"""
Steuerberechnung
================

Rechnet aus dem Ledger einen Jahresreport nach deutschem Recht.

  * Paragraph 23 EStG - private Veraeusserungsgeschaefte, FIFO-Lots,
    Jahresfrist, Freigrenze 1.000 EUR (seit 2024, davor 600 EUR)
  * Paragraph 20 EStG - Termingeschaefte, saldiert
  * Paragraph 22 Nr. 3 EStG - sonstige Einkuenfte, Freigrenze 256 EUR

Alle Betraege werden in EUR bewertet. Fehlt ein Kurs, wird das offen
ausgewiesen statt geschaetzt - eine Steuerzahl mit unsichtbarer Luecke ist
schlimmer als eine fehlende.

Kein Steuerrat. Die Zuordnung bildet eine Auffassung ab, keine gesicherte
Rechtslage.
"""

import json
from collections import deque
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import ledger as L

FREIGRENZE_23 = {2023: Decimal(600), 2024: Decimal(1000)}   # ab 2024: 1.000
FREIGRENZE_22 = Decimal(256)
SPARERPAUSCHBETRAG = Decimal(1000)   # § 20 EStG / Anlage KAP, Einzelveranlagung
HALTEFRIST_TAGE = 365

# Als EUR-Gegenwert gilt 1:1 - der Euro ist die Bezugswaehrung.
BASE = "EUR"


def freigrenze_23(year):
    if year >= 2024:
        return Decimal(1000)
    return Decimal(600)


def result_after_carry(raw, carry_in):
    """Verlustvortrag mindert nur einen tatsaechlichen Gewinn, druecht ein
    Ergebnis aber nie unter den echten Jahresverlust. Ein Jahresverlust wird
    unveraendert ausgewiesen (nicht mit einem alten, viel groesseren Vortrag
    zu einer irrefuehrenden Summe verrechnet); ein Gewinn wird um den Vortrag
    gemindert, aber nie unter 0 gedrueckt (ungenutzter Vortrag verfaellt hier
    nicht automatisch, er muesste im Folgejahr manuell neu erfasst werden)."""
    if raw < 0:
        return raw
    return max(raw - carry_in, Decimal(0))


def year_of(ts):
    return int(ts[:4])


def day_of(ts):
    return ts[:10]


# --- EUR-Kurse -------------------------------------------------------------

def cached_rate(conn, asset, day):
    row = conn.execute(
        "SELECT eur_price FROM price_cache WHERE asset = ? AND day = ? LIMIT 1",
        (asset, day)).fetchone()
    return L.D(row["eur_price"]) if row else None


def store_rate(conn, asset, day, price, source):
    conn.execute(
        """INSERT OR REPLACE INTO price_cache (asset, day, eur_price, source, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        (asset, day, L.dstr(price), source, L.now_utc()))


# Eigene "Quelle" im selben price_cache-Table (PRIMARY KEY hat source mit
# drin) fuer native Kontrakt-Kurse - keine Schema-Aenderung noetig, aber
# eigene Abfragen, damit das nie mit einem gleichnamigen Asset kollidiert.
_NATIVE_SOURCE = "okx-native"


def cached_native_price(conn, inst_id, day):
    row = conn.execute(
        "SELECT eur_price FROM price_cache WHERE asset = ? AND day = ? AND source = ?",
        (inst_id, day, _NATIVE_SOURCE)).fetchone()
    return L.D(row["eur_price"]) if row else None


def store_native_price(conn, inst_id, day, price):
    conn.execute(
        """INSERT OR REPLACE INTO price_cache (asset, day, eur_price, source, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        (inst_id, day, L.dstr(price), _NATIVE_SOURCE, L.now_utc()))


def rates_from_own_trades(conn):
    """Notloesung ohne Netz: Die eigenen Umtauschvorgaenge enthalten echte
    EUR-Kurse. Das deckt nur die Tage ab, an denen getauscht wurde - besser
    als nichts, aber ausdruecklich als solches gekennzeichnet."""
    found = {}
    rows = conn.execute(
        """SELECT t.id, t.ts_utc FROM transactions t
           WHERE t.tx_type = 'trade_spot' ORDER BY t.ts_utc""").fetchall()
    for r in rows:
        entries = conn.execute(
            "SELECT asset, amount FROM entries WHERE transaction_id = ? "
            "AND kind = 'principal'", (r["id"],)).fetchall()
        eur = [e for e in entries if e["asset"] == BASE]
        other = [e for e in entries if e["asset"] != BASE]
        if len(eur) == 1 and len(other) == 1:
            eur_amt = abs(L.D(eur[0]["amount"]))
            oth_amt = abs(L.D(other[0]["amount"]))
            if oth_amt:
                found.setdefault(other[0]["asset"], {})[day_of(r["ts_utc"])] = \
                    eur_amt / oth_amt
    return found


async def build_rate_lookup(conn, assets, days, fetcher=None):
    """Baut eine Kurstabelle. fetcher(asset, day) darf None liefern."""
    own = rates_from_own_trades(conn)
    table, missing, sources = {}, set(), {}

    for asset in assets:
        if asset == BASE:
            continue
        for day in days:
            key = (asset, day)
            # Exakter eigener Kurs an GENAU diesem Tag geht vor jeder
            # externen Schaetzung - das ist der tatsaechlich gezahlte
            # Kurs, kein Naeherungswert. Frueher stand ein zwischengespei-
            # cherter/externer Tageskurs davor, was bei einem EUR<->Asset-
            # Umtausch am selben Tag ein kuenstliches, aber falsches
            # Gewinn/Verlust-Rauschen erzeugt hat (der externe Tageskurs
            # weicht vom tatsaechlichen Ausfuehrungskurs immer leicht ab).
            own_today = (own.get(asset) or {}).get(day)
            if own_today is not None:
                table[key] = own_today
                sources[asset] = "aus eigenem Umtausch an diesem Tag (exakt)"
                continue

            r = cached_rate(conn, asset, day)
            if r is not None:
                table[key] = r
                sources[asset] = sources.get(asset) or "zwischengespeichert"
                continue
            if fetcher:
                try:
                    r = await fetcher(asset, day)
                except Exception:
                    r = None
                if r is not None:
                    table[key] = L.D(r)
                    store_rate(conn, asset, day, L.D(r), "okx")
                    sources[asset] = "OKX-Tageskurs"
                    continue
            # Naechstgelegener eigener Umtauschkurs
            cand = own.get(asset) or {}
            if cand:
                near = min(cand, key=lambda d: abs(
                    (datetime.fromisoformat(d) - datetime.fromisoformat(day)).days))
                table[key] = cand[near]
                sources[asset] = "aus eigenen Umtauschvorgängen abgeleitet"
                continue
            missing.add(f"{asset} am {day}")

    conn.commit()
    return table, sorted(missing), sources


def to_eur(table, asset, day, amount):
    if asset == BASE:
        return L.D(amount)
    r = table.get((asset, day))
    return None if r is None else L.D(amount) * r


# --- FIFO ------------------------------------------------------------------

def build_lots(conn, table):
    """Laeuft chronologisch durch alle Zugaenge und Abgaenge und fuehrt je
    Asset eine FIFO-Schlange. Gibt Veraeusserungen und offene Lots zurueck.

    Wichtig: das umfasst ALLE Buchungen, nicht nur die mit tax_bucket
    'par23'/'par22'/'neutral'. Nach BMF-Schreiben vom 6.3.2025 gilt jede
    Verwendung von Kryptowerten als Zahlungsmittel - auch fuer Trading-
    Gebuehren oder Funding-Zahlungen bei Futures (§20-Buchungen) - selbst
    als privates Veraeusserungsgeschaeft nach §23 EStG, zusaetzlich zum
    eigentlichen §20-Ergebnis der Position. Fruehere Version hat §20-
    Buchungen hier komplett ausgeklammert und dadurch USDC-Ausgaben fuer
    Gebuehren/Funding gar nicht als Veraeusserung erfasst."""
    lots = {}          # asset -> deque von {menge, kosten_eur, datum}
    disposals = []
    warnings = []

    rows = conn.execute(
        """SELECT t.id, t.ts_utc, t.tx_type, t.tax_bucket, t.note
           FROM transactions t
           ORDER BY t.ts_utc, t.id""").fetchall()

    for t in rows:
        entries = conn.execute(
            "SELECT asset, amount, kind FROM entries WHERE transaction_id = ?",
            (t["id"],)).fetchall()
        day = day_of(t["ts_utc"])

        for e in entries:
            asset, amt = e["asset"], L.D(e["amount"])
            if asset == BASE or amt == 0:
                continue
            eur = to_eur(table, asset, day, abs(amt))

            if amt > 0:
                # Zugang: neues Lot. Kosten sind der EUR-Gegenwert.
                if eur is None:
                    warnings.append(
                        f"Kein EUR-Kurs für {asset} am {day} – Anschaffung "
                        "ohne Kostenbasis erfasst.")
                lots.setdefault(asset, deque()).append({
                    "qty": amt, "cost_eur": eur, "day": day,
                    "ts": t["ts_utc"], "note": t["note"]})
            else:
                # Abgang: Lots in FIFO-Reihenfolge verbrauchen
                need = -amt
                q = lots.setdefault(asset, deque())
                while need > 0 and q:
                    lot = q[0]
                    take = min(need, lot["qty"])
                    share = (take / lot["qty"]) if lot["qty"] else Decimal(0)
                    cost = (lot["cost_eur"] * share) if lot["cost_eur"] is not None else None
                    proceeds = (eur * (take / -amt)) if eur is not None else None
                    held = (datetime.fromisoformat(day)
                            - datetime.fromisoformat(lot["day"])).days

                    disposals.append({
                        "asset": asset, "qty": take,
                        "acquired": lot["day"], "sold": day,
                        "held_days": held,
                        "taxable": held < HALTEFRIST_TAGE,
                        "cost_eur": cost, "proceeds_eur": proceeds,
                        "gain_eur": (proceeds - cost)
                            if (proceeds is not None and cost is not None) else None,
                        "year": year_of(t["ts_utc"]),
                        "note": t["note"],
                    })

                    lot["qty"] -= take
                    if lot["cost_eur"] is not None:
                        lot["cost_eur"] -= cost
                    need -= take
                    if lot["qty"] <= 0:
                        q.popleft()

                if need > 0:
                    warnings.append(
                        f"Abgang von {need} {asset} am {day} ohne passenden "
                        "Zugang. Es fehlen ältere Daten.")

    open_lots = []
    today = datetime.now(timezone.utc).date()
    for asset, q in lots.items():
        for lot in q:
            if lot["qty"] <= 0:
                continue
            acquired = datetime.fromisoformat(lot["day"]).date()
            free_on = acquired + timedelta(days=HALTEFRIST_TAGE)
            open_lots.append({
                "asset": asset, "qty": lot["qty"], "acquired": lot["day"],
                "cost_eur": lot["cost_eur"],
                "held_days": (today - acquired).days,
                "free_on": free_on.isoformat(),
                "days_to_free": max(0, (free_on - today).days),
                "tax_free": today >= free_on,
            })

    return disposals, sorted(open_lots, key=lambda x: x["acquired"]), warnings


# --- Toepfe 20 und 22 ------------------------------------------------------

def flat_bucket(conn, bucket, table):
    """Summiert einen Topf, in dem nicht nach Lots gerechnet wird."""
    rows = conn.execute(
        """SELECT t.ts_utc, t.tx_type, e.asset, e.amount, e.kind
           FROM transactions t JOIN entries e ON e.transaction_id = t.id
           WHERE t.tax_bucket = ? ORDER BY t.ts_utc""", (bucket,)).fetchall()

    by_year = {}
    missing = 0
    for r in rows:
        y = year_of(r["ts_utc"])
        eur = to_eur(table, r["asset"], day_of(r["ts_utc"]), L.D(r["amount"]))
        slot = by_year.setdefault(y, {
            "year": y, "eur": Decimal(0), "native": {}, "rows": 0,
            "by_kind": {}, "unpriced": 0})
        slot["rows"] += 1
        slot["native"][r["asset"]] = slot["native"].get(r["asset"], Decimal(0)) \
            + L.D(r["amount"])
        slot["by_kind"][r["kind"]] = slot["by_kind"].get(r["kind"], Decimal(0)) \
            + L.D(r["amount"])
        if eur is None:
            slot["unpriced"] += 1
            missing += 1
        else:
            slot["eur"] += eur
    return list(by_year.values()), missing


# --- Verlustvortrag ---------------------------------------------------------

def get_loss_carryforward(conn):
    """{(year, bucket): {'amount': Decimal, 'note': str}}"""
    out = {}
    for r in conn.execute(
            "SELECT year, bucket, amount_eur, note FROM loss_carryforward"):
        out[(r["year"], r["bucket"])] = {
            "amount": L.D(r["amount_eur"]), "note": r["note"] or ""}
    return out


def set_loss_carryforward(conn, year, bucket, amount, note=""):
    if bucket not in ("par23", "par20", "par22"):
        raise ValueError(f"Verlustvortrag ist für {bucket} nicht vorgesehen.")
    amount = L.D(amount)
    if amount < 0:
        raise ValueError("Verlustvortrag als positive Zahl eintragen (Höhe des Verlusts).")
    if amount == 0:
        conn.execute("DELETE FROM loss_carryforward WHERE year = ? AND bucket = ?",
                    (year, bucket))
    else:
        conn.execute(
            """INSERT INTO loss_carryforward (year, bucket, amount_eur, note, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT (year, bucket) DO UPDATE SET
                 amount_eur = excluded.amount_eur, note = excluded.note,
                 updated_at = excluded.updated_at""",
            (year, bucket, L.dstr(amount), note, L.now_utc()))
    conn.commit()


# --- Buchungsliste je Topf (fuer Report: Deckblatt + gruppierte Liste) ------

def bucket_rows(conn, bucket, year, table):
    """Einzelne Buchungen eines Topfes in einem Jahr, aufbereitet fuer die
    Detailliste im Report - nicht nur die Summe wie flat_bucket()."""
    rows = conn.execute(
        """SELECT t.id, t.ts_utc, t.tx_type, t.note FROM transactions t
           WHERE t.tax_bucket = ? AND substr(t.ts_utc,1,4) = ?
           ORDER BY t.ts_utc""", (bucket, str(year))).fetchall()

    out = []
    for t in rows:
        entries = conn.execute(
            "SELECT asset, amount, kind FROM entries WHERE transaction_id = ? ORDER BY id",
            (t["id"],)).fetchall()
        day = day_of(t["ts_utc"])
        eur_total, any_priced = Decimal(0), False
        native = []
        for e in entries:
            amt = L.D(e["amount"])
            native.append({"asset": e["asset"], "amount": amt, "kind": e["kind"]})
            v = to_eur(table, e["asset"], day, amt)
            if v is not None:
                eur_total += v
                any_priced = True
        out.append({
            "id": t["id"], "date": day, "txType": t["tx_type"], "note": t["note"],
            "native": native, "eur": eur_total if any_priced else None,
        })
    return out


# --- Report ----------------------------------------------------------------

def dec2str(o):
    if isinstance(o, Decimal):
        return format(o, "f")
    if isinstance(o, dict):
        return {k: dec2str(v) for k, v in o.items()}
    if isinstance(o, list):
        return [dec2str(v) for v in o]
    return o


ALL_BUCKETS = ["par23", "par20", "par22", "neutral", "unknown"]
BUCKET_LABELS = {
    "par23": "§ 23 — Private Veräußerungsgeschäfte",
    "par20": "§ 20 — Termingeschäfte (Futures)",
    "par22": "§ 22 — Sonstige Einkünfte",
    "neutral": "Neutral (kein Steuerereignis)",
    "unknown": "Unklar (noch nicht zugeordnet)",
}


async def build_report(conn, fetcher=None):
    assets = [r[0] for r in conn.execute("SELECT DISTINCT asset FROM entries")]
    days = [r[0] for r in conn.execute(
        "SELECT DISTINCT substr(ts_utc,1,10) FROM transactions")]
    table, missing_rates, sources = await build_rate_lookup(conn, assets, days, fetcher)

    disposals, open_lots, warn = build_lots(conn, table)
    p20, m20 = flat_bucket(conn, "par20", table)
    p22, m22 = flat_bucket(conn, "par22", table)
    carry = get_loss_carryforward(conn)

    years = sorted({d["year"] for d in disposals}
                   | {y["year"] for y in p20} | {y["year"] for y in p22}
                   | {int(r[0]) for r in conn.execute(
                       "SELECT DISTINCT substr(ts_utc,1,4) FROM transactions")})

    out_years = []
    for y in years:
        d_y = [d for d in disposals if d["year"] == y and d["taxable"]]
        d_free = [d for d in disposals if d["year"] == y and not d["taxable"]]
        gain = sum((d["gain_eur"] for d in d_y if d["gain_eur"] is not None),
                   Decimal(0))
        fg = freigrenze_23(y)
        e20 = next((x for x in p20 if x["year"] == y), None)
        e22 = next((x for x in p22 if x["year"] == y), None)
        sum22 = e22["eur"] if e22 else Decimal(0)
        r20 = e20["eur"] if e20 else Decimal(0)
        rows20 = bucket_rows(conn, "par20", y, table)
        rows22 = bucket_rows(conn, "par22", y, table)
        all_by_bucket = {b: bucket_rows(conn, b, y, table) for b in ALL_BUCKETS}

        # Manueller Verlustvortrag: mindert den Gewinn/das Ergebnis des Jahres,
        # bevor Freigrenze bzw. Steuerpflicht geprueft wird. Keine automatische
        # Fortschreibung - was nicht verbraucht wird, muss im Folgejahr erneut
        # eingetragen werden.
        c23 = carry.get((y, "par23"), {}).get("amount", Decimal(0))
        c20 = carry.get((y, "par20"), {}).get("amount", Decimal(0))
        c22 = carry.get((y, "par22"), {}).get("amount", Decimal(0))
        # result_after_carry statt einfacher Subtraktion: ein echter Verlust
        # bleibt unveraendert sichtbar, statt mit einem u.U. viel groesseren
        # alten Verlustvortrag zu einer irrefuehrenden Zahl verrechnet zu
        # werden (nur ein Gewinn wird durch den Vortrag gemindert).
        gain_after = result_after_carry(gain, c23)
        r20_after = result_after_carry(r20, c20)
        r20_taxable = (max(r20_after - SPARERPAUSCHBETRAG, Decimal(0))
                      if r20 >= 0 else r20_after)
        sum22_after = result_after_carry(sum22, c22)

        out_years.append({
            "year": y,
            "par23": {
                "gain": gain,
                "lossCarryIn": c23,
                "gainAfterCarry": gain_after,
                "count": len(d_y),
                "countTaxFree": len(d_free),
                "freigrenze": fg,
                "exceeded": gain_after > fg,
                "taxable": gain_after if gain_after > fg else Decimal(0),
                "disposals": [d for d in disposals if d["year"] == y],
            },
            "par20": {
                "result": r20,
                "lossCarryIn": c20,
                "resultAfterCarry": r20_after,
                "sparerpauschbetrag": SPARERPAUSCHBETRAG,
                "exceeded": r20 >= 0 and r20_after > SPARERPAUSCHBETRAG,
                "taxable": r20_taxable,
                "native": e20["native"] if e20 else {},
                "byKind": e20["by_kind"] if e20 else {},
                "count": len(rows20),
                "unpriced": e20["unpriced"] if e20 else 0,
                "allUnpriced": bool(e20) and e20["unpriced"] > 0
                              and e20["unpriced"] == e20["rows"],
                "rows": rows20,
            },
            "par22": {
                "result": sum22,
                "lossCarryIn": c22,
                "resultAfterCarry": sum22_after,
                "native": e22["native"] if e22 else {},
                "count": len(rows22),
                "unpriced": e22["unpriced"] if e22 else 0,
                "allUnpriced": bool(e22) and e22["unpriced"] > 0
                              and e22["unpriced"] == e22["rows"],
                "freigrenze": FREIGRENZE_22,
                "exceeded": sum22_after > FREIGRENZE_22,
                "taxable": sum22_after if sum22_after > FREIGRENZE_22 else Decimal(0),
                "rows": rows22,
            },
            "allByBucket": all_by_bucket,
        })

    unknown = conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE tax_bucket = 'unknown'").fetchone()[0]

    return dec2str({
        "years": out_years,
        "openLots": open_lots,
        "warnings": warn,
        "missingRates": missing_rates[:20],
        "missingRatesCount": len(missing_rates),
        "rateSources": sources,
        "unknownCount": unknown,
        "blocked": unknown > 0 or bool(missing_rates),
        "generatedAt": L.now_utc(),
    })


# --- Portfolio fuer das Dashboard ------------------------------------------

async def portfolio(conn, positions=None, fetcher=None):
    """Bewertet die Bestaende in EUR und stellt sie dem Eingezahlten gegenueber."""
    today = datetime.now(timezone.utc).date().isoformat()
    balances = L.balances(conn)
    assets = list(balances)
    table, missing, sources = await build_rate_lookup(conn, assets, [today], fetcher)

    # In Margin gebunden vs. frei verfuegbar
    in_margin = {}
    for p in (positions or []):
        ccy = p.get("settle_ccy") or "USDC"
        in_margin[ccy] = in_margin.get(ccy, Decimal(0)) + L.D(p.get("margin_balance", 0))

    holdings, total = [], Decimal(0)
    for asset, amount in balances.items():
        if abs(amount) < Decimal("0.000000001"):
            continue
        eur = to_eur(table, asset, today, amount)
        if eur is not None:
            total += eur
        locked = in_margin.get(asset, Decimal(0))
        holdings.append({
            "asset": asset, "amount": amount, "eur": eur,
            "rate": table.get((asset, today)),
            "locked": min(locked, amount) if locked else Decimal(0),
            "free": amount - min(locked, amount) if locked else amount,
        })
    holdings.sort(key=lambda h: -(h["eur"] or Decimal(0)))

    deposits = Decimal(0)
    for r in conn.execute(
            """SELECT e.asset, e.amount, t.ts_utc FROM transactions t
               JOIN entries e ON e.transaction_id = t.id
               WHERE t.tx_type = 'deposit'"""):
        v = to_eur(table, r["asset"], day_of(r["ts_utc"]), L.D(r["amount"]))
        if v is not None:
            deposits += v

    withdrawn = Decimal(0)
    for r in conn.execute(
            """SELECT e.asset, e.amount, t.ts_utc FROM transactions t
               JOIN entries e ON e.transaction_id = t.id
               WHERE t.tx_type = 'withdrawal'"""):
        v = to_eur(table, r["asset"], day_of(r["ts_utc"]), L.D(r["amount"]))
        if v is not None:
            withdrawn += v

    net_in = deposits + withdrawn   # Auszahlungen sind negativ gebucht
    result = total - net_in
    return dec2str({
        "holdings": holdings,
        "totalEur": total,
        "depositedEur": net_in,
        "resultEur": result,
        "resultPct": (result / net_in * 100) if net_in else None,
        "rateSources": sources,
        "missingRates": missing[:8],
        "asOf": today,
    })


# --- Gebuehren & Funding, summiert -----------------------------------------

def fees_summary(conn):
    """Gezahlte Gebuehren und Funding getrennt nach Richtung, je Asset in
    nativer Waehrung summiert (keine Kursabhaengigkeit, immer verfuegbar)."""
    fees_paid, funding_recv, funding_paid = {}, {}, {}
    for r in conn.execute("SELECT asset, amount, kind FROM entries WHERE kind IN ('fee','funding')"):
        amt = L.D(r["amount"])
        if amt == 0:
            continue
        if r["kind"] == "fee":
            fees_paid[r["asset"]] = fees_paid.get(r["asset"], Decimal(0)) + abs(amt)
        elif amt > 0:
            funding_recv[r["asset"]] = funding_recv.get(r["asset"], Decimal(0)) + amt
        else:
            funding_paid[r["asset"]] = funding_paid.get(r["asset"], Decimal(0)) + abs(amt)
    return dec2str({
        "feesPaid": fees_paid, "fundingReceived": funding_recv, "fundingPaid": funding_paid,
    })


# --- Kapitalkurve ------------------------------------------------------------

# Zeitraum-Filter fuer die Kapitalkurve, analog zu OKX' Chart-Buttons.
# "all"/unbekannter Key -> komplette Historie (kein Eintrag hier noetig).
RANGE_DAYS = {"7d": 7, "30d": 30, "90d": 90, "365d": 365}


async def capital_curve(conn, fetcher=None, points=150, positions=None,
                         instmap=None, price_fetcher=None, range_key=None):
    """Portfoliowert an bis zu `points` gleichmaessig verteilten Tagen
    zwischen Start des Zeitraums und heute.

    Enthaelt jetzt auch das unrealisierte Ergebnis aktuell offener
    Positionen: fuer jeden Tag ab Eroeffnung wird der historische
    Kontrakt-Kurs von OKX geholt (oeffentlicher Endpunkt, wie bei den
    EUR-Tageskursen) und die Differenz zum Einstiegspreis wie im Dashboard
    oben in EUR umgerechnet und aufaddiert.

    Naeherung: die HEUTIGE Positionsgroesse/der heutige Einstiegspreis wird
    rueckwirkend auf den Kursverlauf angewendet - die tatsaechliche Historie
    von Nachschuessen oder Teil-Glattstellungen ist unbekannt. Fehlt
    `opened_at` (alte Imports vor dieser Aenderung), wird das unrealisierte
    Ergebnis nur fuer den heutigen Punkt angesetzt, nicht rueckwirkend."""
    span = conn.execute("SELECT MIN(ts_utc), MAX(ts_utc) FROM transactions").fetchone()
    if not span[0]:
        return {"points": []}

    start = datetime.fromisoformat(day_of(span[0])).replace(tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)
    range_days = RANGE_DAYS.get(range_key)
    start_bound = max(start, end - timedelta(days=range_days)) if range_days else start

    total_days = max(1, (end.date() - start_bound.date()).days)
    step = max(1, total_days // max(1, points - 1))
    days = []
    d = start_bound
    while d.date() <= end.date():
        days.append(d.date().isoformat())
        d += timedelta(days=step)
    if days[-1] != end.date().isoformat():
        days.append(end.date().isoformat())

    assets = [r[0] for r in conn.execute("SELECT DISTINCT asset FROM entries")]
    table, missing, sources = await build_rate_lookup(conn, assets, days, fetcher)

    # Unrealisiertes Ergebnis offener Positionen, je Tag in EUR.
    pos_by_day = {day: Decimal(0) for day in days}
    missing_mark = 0
    last_day = days[-1]
    for p in (positions or []):
        inst = p.get("instrument")
        size = abs(L.D(p.get("size") or 0))
        if not inst or size == 0 or not price_fetcher:
            continue
        avg = L.D(p.get("avg_entry") or 0)
        ccy = p.get("settle_ccy") or "USDC"
        direction = Decimal(1) if p.get("side") == "long" else Decimal(-1)
        m = (instmap or {}).get(inst) or {}
        inst_id = m.get("instId") or inst
        opened_day = day_of(p["opened_at"]) if p.get("opened_at") else None

        for day in days:
            if opened_day:
                if day < opened_day:
                    continue
            elif day != last_day:
                # Eroeffnungsdatum unbekannt (Import von vor dieser Aenderung) -
                # nur "heute" ansetzen, statt eine unbekannte Vergangenheit zu erfinden.
                continue

            mark = cached_native_price(conn, inst_id, day)
            if mark is None:
                try:
                    fetched = await price_fetcher(inst_id, day)
                except Exception:
                    fetched = None
                mark = L.D(fetched) if fetched is not None else None
                if mark is not None:
                    store_native_price(conn, inst_id, day, mark)
            rate = table.get((ccy, day))
            if mark is None or rate is None:
                missing_mark += 1
                continue
            upnl_native = (mark - avg) * size * direction
            pos_by_day[day] += upnl_native * rate

    out = []
    for day in days:
        until = day + "T23:59:59Z"
        bal = L.balances(conn, until=until)
        total, complete = Decimal(0), True
        for asset, amount in bal.items():
            v = to_eur(table, asset, day, amount)
            if v is None:
                complete = False
            else:
                total += v
        total += pos_by_day.get(day, Decimal(0))
        out.append({"day": day, "eur": total, "complete": complete})
    return dec2str({"points": out, "missingRatesCount": len(missing) + missing_mark})


# --- CSV -------------------------------------------------------------------

def money(v):
    """Zwei Nachkommastellen, deutsches Dezimalkomma. Fuer PDF/Anzeige."""
    if v in (None, ""):
        return ""
    return format(Decimal(str(v)).quantize(Decimal("0.01")), "f").replace(".", ",")


def num(v, places="0.01"):
    """Zwei Nachkommastellen, Punkt als Dezimaltrennzeichen - echte, von
    Tabellenkalkulationen als Zahl erkennbare Werte statt lokalisiertem Text."""
    if v in (None, ""):
        return ""
    return format(Decimal(str(v)).quantize(Decimal(places)), "f")


def _csv_row(cols):
    """Escaped fuer ';'-getrennte CSV: Felder mit ';', '"' oder Zeilenumbruch
    werden in Anfuehrungszeichen gesetzt, enthaltene '"' verdoppelt."""
    out = []
    for c in cols:
        c = "" if c is None else str(c)
        if any(ch in c for ch in (";", '"', "\n")):
            c = '"' + c.replace('"', '""') + '"'
        out.append(c)
    return ";".join(out)


TX_TYPE_LABELS = {
    "deposit": "Einzahlung", "withdrawal": "Auszahlung", "transfer": "Umbuchung",
    "trade_spot": "Tausch", "trade_derivative": "Futures",
    "funding_fee": "Finanzierungsrate", "fee": "Gebühr",
    "earn_reward": "Earn-Gutschrift", "adjustment": "Korrektur",
}


def report_csv(report):
    """Vier klar getrennte Tabellen: Kennzahlen je Jahr/Topf, eine
    Buchungszeile pro Vorgang (feste Spalten, alle Steuertoepfe inkl.
    neutraler Buchungen - jede Buchung genau einmal), die daraus per FIFO
    erkannten Veraeusserungen nach §23 (das ist die einzige Stelle mit einer
    zweiten, ueberschneidenden Sicht - weil sie echten Mehrwert bringt: den
    berechneten Gewinn/Verlust je Abgang statt nur die Rohdaten), und die
    offenen (noch nicht verkauften) Bestaende. Zahlen stehen als echte
    Dezimalzahlen mit Punkt, nicht als lokalisierter Text - damit Excel/
    Numbers/Sheets sie direkt als Zahl erkennen.
    """
    lines = []

    lines.append("# KENNZAHLEN")
    lines.append(_csv_row(["Jahr", "Steuertopf", "Kennzahl", "Wert_EUR"]))
    for y in report["years"]:
        yr = y["year"]
        kz = [
            ("§23", "Gewinn steuerpflichtige Veraeusserungen", y["par23"]["gain"]),
            ("§23", "Verlustvortrag", y["par23"]["lossCarryIn"]),
            ("§23", "Gewinn nach Verlustvortrag", y["par23"]["gainAfterCarry"]),
            ("§23", "Freigrenze", y["par23"]["freigrenze"]),
            ("§23", "Steuerpflichtig nach Freigrenze", y["par23"]["taxable"]),
            ("§20", "Ergebnis Termingeschaefte", y["par20"]["result"]),
            ("§20", "Verlustvortrag", y["par20"]["lossCarryIn"]),
            ("§20", "Ergebnis nach Verlustvortrag", y["par20"]["resultAfterCarry"]),
            ("§20", "Sparerpauschbetrag", y["par20"]["sparerpauschbetrag"]),
            ("§20", "Steuerpflichtig nach Sparerpauschbetrag", y["par20"]["taxable"]),
            ("§22", "Sonstige Einkuenfte", y["par22"]["result"]),
            ("§22", "Verlustvortrag", y["par22"]["lossCarryIn"]),
            ("§22", "Steuerpflichtig nach Freigrenze", y["par22"]["taxable"]),
        ]
        for topf, feld, wert in kz:
            lines.append(_csv_row([yr, topf, feld, num(wert)]))

    lines.append("")
    lines.append("# BUCHUNGEN (jede Transaktion genau einmal, nach Steuertopf - auch neutrale)")
    lines.append(_csv_row([
        "Jahr", "Steuertopf", "Datum", "Typ", "Beschreibung",
        "Native_Buchungszeilen", "Betrag_EUR",
    ]))
    for y in report["years"]:
        yr = y["year"]
        for bucket in ALL_BUCKETS:
            for r in y["allByBucket"].get(bucket, []):
                native = "; ".join(
                    f"{num(n['amount'], '0.00000001')} {n['asset']}"
                    + (" (Gebühr)" if n["kind"] == "fee" else "")
                    for n in r["native"])
                lines.append(_csv_row([
                    yr, BUCKET_LABELS[bucket], r["date"],
                    TX_TYPE_LABELS.get(r["txType"], r["txType"]),
                    r["note"] or "", native, num(r["eur"]),
                ]))

    lines.append("")
    lines.append("# VERAEUSSERUNGEN_FIFO (§23 - berechneter Gewinn/Verlust je Abgang, "
                 "FIFO ueber ALLE Buchungen inkl. z.B. Trading-Gebuehren/Funding in USDC)")
    lines.append(_csv_row([
        "Jahr", "Datum", "Asset", "Menge", "Erworben_am", "Haltedauer_Tage",
        "Steuerpflichtig", "Beschreibung", "Gewinn_Verlust_EUR",
    ]))
    for y in report["years"]:
        yr = y["year"]
        for d in y["par23"]["disposals"]:
            lines.append(_csv_row([
                yr, d["sold"], d["asset"], num(d["qty"], "0.00000001"),
                d["acquired"], d["held_days"], "Ja" if d["taxable"] else "Nein",
                d["note"] or "", num(d["gain_eur"]),
            ]))

    lines.append("")
    lines.append("# OFFENE_BESTAENDE")
    lines.append(_csv_row(["Asset", "Menge", "Erworben_am", "Steuerfrei_ab", "Tage_bis_steuerfrei"]))
    for lot in report["openLots"]:
        lines.append(_csv_row([
            lot["asset"], num(lot["qty"], "0.00000001"), lot["acquired"],
            lot["free_on"], lot["days_to_free"],
        ]))

    return "\n".join(lines)
