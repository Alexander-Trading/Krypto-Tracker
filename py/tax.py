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
HALTEFRIST_TAGE = 365   # nur noch als grobe Anzeigegroesse; massgeblich ist haltefrist_ende()

# Als EUR-Gegenwert gilt 1:1 - der Euro ist die Bezugswaehrung.
BASE = "EUR"


def freigrenze_23(year):
    if year >= 2024:
        return Decimal(1000)
    return Decimal(600)


def haltefrist_ende(acquired):
    """Letzter Tag der Jahresfrist (§ 23 Abs. 1 Nr. 2 EStG, § 108 AO,
    §§ 187 Abs. 1, 188 Abs. 2 BGB): die Frist beginnt am Tag nach der
    Anschaffung und endet mit Ablauf des Kalendertags im Folgejahr, der das
    Datum der Anschaffung traegt. Ein Verkauf an diesem Tag ist noch
    steuerpflichtig, erst ab dem Folgetag steuerfrei. Anschaffung am 29.2.:
    das Folgejahr hat keinen 29.2., die Frist endet am 28.2. (§ 188 Abs. 3 BGB).

    Fruehere Version: "steuerpflichtig, wenn weniger als 365 Tage" - das hat
    einen Verkauf genau am Jahrestag (und in Schaltjahren schon am Vortag)
    faelschlich als steuerfrei gewertet."""
    from datetime import date
    if isinstance(acquired, str):
        acquired = date.fromisoformat(acquired[:10])
    try:
        return acquired.replace(year=acquired.year + 1)
    except ValueError:            # 29.2. -> 28.2. des Folgejahres
        return acquired.replace(year=acquired.year + 1, day=28)


def innerhalb_haltefrist(acquired, sold):
    """True = Veraeusserung innerhalb der Jahresfrist (steuerpflichtig)."""
    from datetime import date
    if isinstance(sold, str):
        sold = date.fromisoformat(sold[:10])
    return sold <= haltefrist_ende(acquired)


def apply_freigrenze(gain, carry_in, freigrenze):
    """Freigrenze (§ 23 Abs. 3 S. 5 bzw. § 22 Nr. 3 S. 2 EStG) und Verlust-
    vortrag in der richtigen Reihenfolge: Die Freigrenze wird am Jahres-
    ergebnis VOR dem Verlustabzug geprueft (BFH 11.1.2005, IX R 27/04,
    BStBl II 2005, 433). Liegt das Ergebnis darunter, ist es steuerfrei und der
    Vortrag bleibt unverbraucht. Liegt es darueber, ist der GESAMTE Betrag
    steuerpflichtig und wird erst dann um den Vortrag gemindert - auch wenn
    danach weniger als die Freigrenze uebrig bleibt.

    Fruehere Version hat erst den Vortrag abgezogen und dann die Freigrenze
    geprueft: 1.500 Gewinn / 800 Vortrag ergab 0 statt 700 steuerpflichtig,
    und unter der Freigrenze wurde der Vortrag unnoetig verbraucht."""
    exceeded = gain > freigrenze
    used = min(carry_in, gain) if exceeded else Decimal(0)
    return {
        "exceeded": exceeded,
        "carryUsed": used,
        "carryRemaining": carry_in - used,
        "afterCarry": gain - used,
        "taxable": (gain - used) if exceeded else Decimal(0),
    }


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
        "SELECT eur_price FROM price_cache WHERE asset = ? AND day = ? "
        "AND source != 'miss' LIMIT 1", (asset, day)).fetchone()
    return L.D(row["eur_price"]) if row else None


def recent_miss(conn, asset, day, hours=24):
    """Hat OKX fuer dieses Paar vor kurzem keinen Kurs geliefert? Dann nicht
    bei jedem App-Start erneut fragen (kostet auf dem iPhone Sekunden)."""
    row = conn.execute(
        "SELECT fetched_at FROM price_cache WHERE asset = ? AND day = ? AND source = 'miss'",
        (asset, day)).fetchone()
    if not row:
        return False
    try:
        t = datetime.fromisoformat(row["fetched_at"].replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - t).total_seconds() < hours * 3600


def store_miss(conn, asset, day):
    conn.execute(
        """INSERT OR REPLACE INTO price_cache (asset, day, eur_price, source, fetched_at)
           VALUES (?, ?, '', 'miss', ?)""", (asset, day, L.now_utc()))


def store_rate(conn, asset, day, price, source):
    conn.execute(
        """INSERT OR REPLACE INTO price_cache (asset, day, eur_price, source, fetched_at)
           VALUES (?, ?, ?, ?, ?)""",
        (asset, day, L.dstr(price), source, L.now_utc()))


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


# Kurzzeit-Speicher fuer den heutigen Kurs. Der Tag ist noch nicht zu Ende, der
# "Schlusskurs" von heute ist also nur ein Momentwert. Er darf deshalb nicht
# dauerhaft in price_cache landen (sonst bliebe der Portfoliowert den ganzen
# Tag auf dem ersten Abruf stehen), soll aber auch nicht bei jedem Tab-Wechsel
# neu geholt werden.
_TODAY_TTL = 60
_today_cache = {}   # asset -> (monotonic_ts, kurs)


def _today_utc():
    return datetime.now(timezone.utc).date().isoformat()


async def build_rate_lookup(conn, assets, days, fetcher=None, pairs=None):
    """Baut eine Kurstabelle. fetcher(asset, day) darf None liefern.

    pairs: optional Liste von (asset, day). Dann werden genau diese Paare
    bewertet statt aller Kombinationen aus assets x days - spart bei
    Altbestaenden mit vielen Kaufterminen dutzende unnoetige Abrufe."""
    import time
    own = rates_from_own_trades(conn)
    table, missing, sources = {}, set(), {}
    today = _today_utc()

    if pairs is None:
        pairs = [(a, d) for a in assets for d in days]
    pairs = sorted({(a, d) for a, d in pairs if a != BASE})

    # Abrufe vorab PARALLEL erledigen (frueher strikt nacheinander - bei ~90
    # neuen Tageskursen auf dem iPhone viele Sekunden). Max. 6 gleichzeitig,
    # um OKX nicht zu ueberlasten. Danach greift die Logik unten wie bisher,
    # nur liefert "fetcher" dann das vorab geholte Ergebnis.
    if fetcher:
        import asyncio
        need = []
        for asset, day in pairs:
            if day == today:
                hit = _today_cache.get(asset)
                if not (hit and time.monotonic() - hit[0] < _TODAY_TTL):
                    need.append((asset, day))
            elif (own.get(asset) or {}).get(day) is None and \
                    cached_rate(conn, asset, day) is None and \
                    not recent_miss(conn, asset, day):
                need.append((asset, day))
        if need:
            sem = asyncio.Semaphore(6)
            raw_fetcher = fetcher

            async def one(a, d):
                async with sem:
                    try:
                        return (a, d), await raw_fetcher(a, d)
                    except Exception:
                        return (a, d), None
            got = dict(await asyncio.gather(*(one(a, d) for a, d in need)))
            for (a, d), v in got.items():
                if v is None and d != today:
                    store_miss(conn, a, d)
            skip = {(a, d) for a, d in pairs
                    if d != today and (a, d) not in got and recent_miss(conn, a, d)}

            async def fetcher(a, d):          # noqa: F811 - bewusst ueberschrieben
                if (a, d) in got:
                    return got[(a, d)]
                if (a, d) in skip:
                    return None
                return await raw_fetcher(a, d)
        else:
            skip = {(a, d) for a, d in pairs if d != today and recent_miss(conn, a, d)}
            if skip:
                raw_fetcher = fetcher

                async def fetcher(a, d):      # noqa: F811
                    if (a, d) in skip:
                        return None
                    return await raw_fetcher(a, d)

    for asset, day in pairs:
        key = (asset, day)
        is_today = (day == today)

        # Exakter eigener Kurs an GENAU diesem Tag geht vor jeder
        # externen Schaetzung - das ist der tatsaechlich gezahlte
        # Kurs, kein Naeherungswert. (Fuer "heute" gilt das nicht: ein
        # Umtauschkurs vom Morgen ist kein aktueller Marktkurs.)
        own_today = (own.get(asset) or {}).get(day)
        if own_today is not None and not is_today:
            table[key] = own_today
            sources[asset] = "aus eigenem Umtausch an diesem Tag (exakt)"
            continue

        if is_today:
            hit = _today_cache.get(asset)
            if hit and time.monotonic() - hit[0] < _TODAY_TTL:
                table[key] = hit[1]
                sources[asset] = "OKX-Kurs (aktuell)"
                continue
            if fetcher:
                try:
                    r = await fetcher(asset, day)
                except Exception:
                    r = None
                if r is not None:
                    table[key] = L.D(r)
                    _today_cache[asset] = (time.monotonic(), L.D(r))
                    sources[asset] = "OKX-Kurs (aktuell)"
                    continue
            # Abruf fehlgeschlagen: lieber der letzte bekannte Wert (als
            # solcher gekennzeichnet) als gar keiner.
            if hit:
                table[key] = hit[1]
                sources[asset] = "letzter bekannter Kurs (Abruf gerade nicht möglich)"
                continue
            r = cached_rate(conn, asset, day)
            if r is not None:
                table[key] = r
                sources[asset] = "zwischengespeichert (Abruf gerade nicht möglich)"
                continue
        else:
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
        """SELECT t.id, t.ts_utc, t.tx_type, t.tax_bucket, t.note, t.account
           FROM transactions t
           ORDER BY t.ts_utc, t.id""").fetchall()

    transferred_out = []   # Auszahlungen auf eigene Wallets (keine Veraeusserung)

    for t in rows:
        # Umbuchung zwischen eigenen Konten (Funding <-> Trading usw.): kein
        # Steuerereignis. Die Coins bleiben dieselben, die Haltefrist laeuft
        # weiter. Beide Seiten werden uebersprungen - frueher wurde die
        # Abbuchung als Verkauf und die Gutschrift als Neukauf zum Tageskurs
        # gerechnet (fiktiver Gewinn, Haltefrist neu gestartet). Da FIFO hier
        # ueber alle Konten gemeinsam laeuft, aendert eine Umbuchung nichts
        # an der Reihenfolge der Lots.
        if t["tx_type"] == "transfer":
            continue

        entries = conn.execute(
            "SELECT asset, amount, kind, eur_value FROM entries WHERE transaction_id = ?",
            (t["id"],)).fetchall()
        day = day_of(t["ts_utc"])

        # Gebuehren eines Tauschs (§ 23): Anschaffungsnebenkosten beim Kauf,
        # Veraeusserungskosten beim Verkauf (BMF 6.3.2025, Rz. 57 ff.).
        # Frueher wurden EUR-Gebuehren komplett ignoriert und Coin-Gebuehren
        # nur als Mini-Veraeusserung gebucht, ohne den Gewinn zu mindern.
        # Eine in Krypto gezahlte Gebuehr bleibt zusaetzlich selbst eine
        # Veraeusserung der Gebuehren-Coins (unten, wie bisher).
        fee_eur = Decimal(0)
        principal_out = [e for e in entries if e["kind"] == "principal"
                         and e["asset"] != BASE and L.D(e["amount"]) < 0]
        principal_in = [e for e in entries if e["kind"] == "principal"
                        and e["asset"] != BASE and L.D(e["amount"]) > 0]
        if t["tax_bucket"] == "par23" and t["tx_type"] == "trade_spot":
            for e in entries:
                if e["kind"] != "fee" or L.D(e["amount"]) == 0:
                    continue
                v = to_eur(table, e["asset"], day, abs(L.D(e["amount"])))
                if v is None:
                    warnings.append(
                        f"Kein EUR-Kurs für die Gebühr in {e['asset']} am {day} – "
                        "Gebühr mindert den Gewinn nicht.")
                else:
                    fee_eur += v
        fee_on_sale = fee_eur if principal_out else Decimal(0)
        fee_on_buy = fee_eur if (fee_eur and not principal_out and principal_in) else Decimal(0)

        # Auszahlung nach aussen: als Uebertrag auf eine eigene Wallet
        # behandelt (Regelfall: OKX -> Trezor), also keine Veraeusserung.
        # Die Lots verlassen die OKX-Rechnung; auf der Wallet stehen sie
        # ueber die Bestands-CSV mit ihren urspruenglichen Kaufdaten.
        # Im Pruefen-Tab als "Zahlung an Dritte" markiert (Topf par23): dann
        # ist die Auszahlung eine Veraeusserung zum Tageskurs.
        is_withdrawal = t["tx_type"] == "withdrawal" and t["tax_bucket"] != "par23"

        for e in entries:
            asset, amt = e["asset"], L.D(e["amount"])
            if asset == BASE or amt == 0:
                continue
            # Bekannter Einstandswert (Wallet-Bestand): tatsaechlich gezahlter
            # Betrag, keine Schaetzung ueber einen Tageskurs.
            if amt > 0 and e["eur_value"] not in (None, ""):
                eur = L.D(e["eur_value"])
            else:
                eur = to_eur(table, asset, day, abs(amt))

            if amt > 0:
                # Zugang: neues Lot. Kosten sind der EUR-Gegenwert.
                if eur is None:
                    warnings.append(
                        f"Kein EUR-Kurs für {asset} am {day} – Anschaffung "
                        "ohne Kostenbasis erfasst.")
                elif fee_on_buy and e["kind"] == "principal":
                    eur += fee_on_buy / len(principal_in)
                lots.setdefault(asset, deque()).append({
                    "qty": amt, "cost_eur": eur, "day": day,
                    "ts": t["ts_utc"], "note": t["note"],
                    "account": t["account"]})
            else:
                # Abgang: Lots in FIFO-Reihenfolge verbrauchen
                need = -amt
                q = lots.setdefault(asset, deque())
                if (eur is not None and fee_on_sale
                        and e["kind"] == "principal"):
                    eur -= fee_on_sale / len(principal_out)
                while need > 0 and q:
                    lot = q[0]
                    take = min(need, lot["qty"])
                    share = (take / lot["qty"]) if lot["qty"] else Decimal(0)
                    cost = (lot["cost_eur"] * share) if lot["cost_eur"] is not None else None
                    proceeds = (eur * (take / -amt)) if eur is not None else None
                    held = (datetime.fromisoformat(day)
                            - datetime.fromisoformat(lot["day"])).days

                    if is_withdrawal:
                        transferred_out.append({
                            "asset": asset, "qty": take,
                            "acquired": lot["day"], "date": day})
                    else:
                        disposals.append({
                            "asset": asset, "qty": take,
                            "acquired": lot["day"], "sold": day,
                            "held_days": held,
                            "taxable": innerhalb_haltefrist(lot["day"], day),
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

                if need > 0 and not is_withdrawal:
                    warnings.append(
                        f"Abgang von {need} {asset} am {day} ohne passenden "
                        "Zugang. Es fehlen ältere Daten.")

    notes = []
    if transferred_out:
        n = len({(x["asset"], x["date"]) for x in transferred_out})
        notes.append(
            f"{n} Auszahlung(en) von OKX gelten als Übertrag auf deine Trezor, deren "
            "Coins mit Kaufdatum in der Bestands-CSV stehen (keine Veräußerung). "
            "Im Tab „Prüfen“ unter „Ein- und Auszahlungen“ lässt sich das je Vorgang "
            "ändern: Umbuchung mit Kaufdaten oder Zahlung an Dritte.")

    open_lots = []
    today = datetime.now(timezone.utc).date()
    for asset, q in lots.items():
        for lot in q:
            if lot["qty"] <= 0:
                continue
            acquired = datetime.fromisoformat(lot["day"]).date()
            free_on = haltefrist_ende(acquired) + timedelta(days=1)
            open_lots.append({
                "asset": asset, "qty": lot["qty"], "acquired": lot["day"],
                "cost_eur": lot["cost_eur"],
                "origin": lot.get("note"), "account": lot.get("account"),
                "held_days": (today - acquired).days,
                "free_on": free_on.isoformat(),
                "days_to_free": max(0, (free_on - today).days),
                "tax_free": today >= free_on,
            })

    return disposals, sorted(open_lots, key=lambda x: x["acquired"]), warnings, notes


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


def native_label(n):
    """Beschriftung einer Buchungszeile im Report: was ist dieser Teil?
    Frueher stand nur bei Gebuehren ein Zusatz - eine Positionsschliessung
    ("254,53 USDC · -6,88 USDC (Gebühr)") las sich dann wie eine Gebuehr."""
    amt = L.D(n["amount"])
    kind = n.get("kind")
    if kind == "pnl":
        return "realisierter Gewinn" if amt >= 0 else "realisierter Verlust"
    if kind == "fee":
        return "Gebühr" if amt <= 0 else "Gebührenerstattung"
    if kind == "funding":
        return "Funding erhalten" if amt >= 0 else "Funding gezahlt"
    return ""


def native_text(n, places=8):
    """'254,53305 USDC realisierter Gewinn' - deutsches Zahlenformat,
    ueberfluessige Nullen weg."""
    amt = L.D(n["amount"])
    q = amt.quantize(Decimal(1).scaleb(-places)).normalize()
    s = f"{q:f}"
    if "." in s:
        whole, frac = s.split(".")
    else:
        whole, frac = s, ""
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    grouped = ""
    while len(whole) > 3:
        grouped = "." + whole[-3:] + grouped
        whole = whole[:-3]
    txt = ("-" if neg else "") + whole + grouped + ("," + frac if frac else "")
    lab = native_label(n)
    return f"{txt} {n['asset']}" + (f" {lab}" if lab else "")


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
    # Nur die Paare bewerten, die wirklich vorkommen - und nur dort, wo kein
    # fester Einstandswert (entries.eur_value) vorliegt. Bei einem Altbestand
    # mit Dutzenden Kaufterminen waeren sonst Hunderte Kursabrufe faellig.
    pairs = [(r[0], r[1]) for r in conn.execute(
        """SELECT DISTINCT e.asset, substr(t.ts_utc,1,10)
           FROM entries e JOIN transactions t ON t.id = e.transaction_id
           WHERE e.eur_value IS NULL OR e.amount < 0""")]
    table, missing_rates, sources = await build_rate_lookup(
        conn, [], [], fetcher, pairs=pairs)

    disposals, open_lots, warn, notes = build_lots(conn, table)

    # Offene Lots mit aktuellem Wert: zeigt je Kauf, was er heute wert ist.
    today = _today_utc()
    now_table, now_missing, now_sources = await build_rate_lookup(
        conn, [], [], fetcher,
        pairs=[(l["asset"], today) for l in open_lots])
    for lot in open_lots:
        rate = now_table.get((lot["asset"], today))
        lot["rate_now"] = rate
        lot["value_eur"] = (lot["qty"] * rate) if rate is not None else None
        lot["unrealized_eur"] = (
            lot["value_eur"] - lot["cost_eur"]
            if lot["value_eur"] is not None and lot["cost_eur"] is not None
            else None)
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

        # Manueller Verlustvortrag (keine automatische Fortschreibung). Bei
        # § 23/§ 22 erst Freigrenze pruefen, dann Vortrag - siehe
        # apply_freigrenze().
        c23 = carry.get((y, "par23"), {}).get("amount", Decimal(0))
        c20 = carry.get((y, "par20"), {}).get("amount", Decimal(0))
        c22 = carry.get((y, "par22"), {}).get("amount", Decimal(0))
        fg23 = apply_freigrenze(gain, c23, fg)
        fg22 = apply_freigrenze(sum22, c22, FREIGRENZE_22)
        gain_after = fg23["afterCarry"]
        sum22_after = fg22["afterCarry"]
        # § 20: Sparerpauschbetrag ist ein Freibetrag, keine Freigrenze -
        # hier ist "erst Verlustvortrag, dann Pauschbetrag" richtig.
        used20 = min(c20, r20) if r20 > 0 else Decimal(0)
        r20_after = r20 - used20
        r20_taxable = (max(r20_after - SPARERPAUSCHBETRAG, Decimal(0))
                      if r20 >= 0 else r20_after)

        decl = declaration_values(d_y, rows20, rows22, {
            "par23": (gain, fg23), "par22": (sum22, fg22),
            "par20": (r20, used20, r20_taxable)})

        out_years.append({
            "year": y,
            "declaration": decl,
            "par23": {
                "gain": gain,
                "lossCarryIn": c23,
                "gainAfterCarry": gain_after,
                "carryUsed": fg23["carryUsed"],
                "carryRemaining": fg23["carryRemaining"],
                "count": len(d_y),
                "countTaxFree": len(d_free),
                "freigrenze": fg,
                "exceeded": fg23["exceeded"],
                "taxable": fg23["taxable"],
                "disposals": [d for d in disposals if d["year"] == y],
            },
            "par20": {
                "result": r20,
                "lossCarryIn": c20,
                "resultAfterCarry": r20_after,
                "carryUsed": used20,
                "carryRemaining": c20 - used20,
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
                "carryUsed": fg22["carryUsed"],
                "carryRemaining": fg22["carryRemaining"],
                "native": e22["native"] if e22 else {},
                "count": len(rows22),
                "unpriced": e22["unpriced"] if e22 else 0,
                "allUnpriced": bool(e22) and e22["unpriced"] > 0
                              and e22["unpriced"] == e22["rows"],
                "freigrenze": FREIGRENZE_22,
                "exceeded": fg22["exceeded"],
                "taxable": fg22["taxable"],
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
        "notes": notes,
        "methodology": methodology(),
        "missingRates": missing_rates[:20],
        "missingRatesCount": len(missing_rates),
        "rateSources": {**sources, **now_sources},
        "unknownCount": unknown,
        "blocked": unknown > 0 or bool(missing_rates),
        "generatedAt": L.now_utc(),
    })


# --- Werte fuer die Steuererklaerung ----------------------------------------

def declaration_values(d_taxable, rows20, rows22, ctx):
    """Bereitet die Jahreswerte so auf, wie sie in die Anlagen gehoeren.
    Bewusst ohne Zeilennummern - die aendern sich je Formularjahr."""
    D0 = Decimal(0)
    priced = [d for d in d_taxable if d["gain_eur"] is not None]
    proceeds = sum((d["proceeds_eur"] for d in priced), D0)
    cost = sum((d["cost_eur"] for d in priced), D0)
    gain23, fg23 = ctx["par23"]

    def split(rows):
        pos = sum((r["eur"] for r in rows if r["eur"] is not None and r["eur"] > 0), D0)
        neg = sum((-r["eur"] for r in rows if r["eur"] is not None and r["eur"] < 0), D0)
        return pos, neg

    g20, l20 = split(rows20)
    r20, used20, tax20 = ctx["par20"]
    inc22, wk22 = split(rows22)
    sum22, fg22 = ctx["par22"]
    return {
        "so23": {
            "label": "Anlage SO – Private Veräußerungsgeschäfte, andere Wirtschaftsgüter (Kryptowerte)",
            "count": len(d_taxable), "unpriced": len(d_taxable) - len(priced),
            "proceeds": proceeds, "cost": cost, "werbungskosten": D0,
            "gain": gain23, "freigrenzeExceeded": fg23["exceeded"],
            "carryUsed": fg23["carryUsed"], "taxable": fg23["taxable"],
            "hint": "Gebühren sind in Veräußerungspreis bzw. Anschaffungskosten "
                    "eingerechnet, deshalb Werbungskosten 0. Einzelaufstellung: "
                    "CSV- oder PDF-Export.",
        },
        "so22": {
            "label": "Anlage SO – Leistungen (§ 22 Nr. 3 EStG)",
            "income": inc22, "werbungskosten": wk22, "result": sum22,
            "freigrenzeExceeded": fg22["exceeded"], "taxable": fg22["taxable"],
        },
        "kap20": {
            "label": "Anlage KAP – Kapitalerträge ohne inländischen Steuerabzug (ausländischer Anbieter OKX)",
            "result": r20, "gains": g20, "losses": l20,
            "carryUsed": used20, "taxableAfterPauschbetrag": tax20,
            "hint": "Ergebnis der Termingeschäfte; Gewinne und Verluste je Buchung "
                    "(Futures-Ergebnis, Gebühr, Funding). Sparerpauschbetrag wird im "
                    "Bescheid über alle Kapitalerträge berücksichtigt.",
        },
    }


def methodology():
    """Rechenweg in Worten - fuer Report, PDF und eine eventuelle
    Rueckfrage des Finanzamts (BMF 6.3.2025: Verwendungsreihenfolge,
    Kurse und Zeitpunkte muessen nachvollziehbar sein)."""
    return [
        "Verwendungsreihenfolge: FIFO je Kryptowert über alle Konten gemeinsam "
        "(OKX Funding/Trading/Earn und Trezor).",
        "Haltefrist: Die Jahresfrist beginnt am Tag nach der Anschaffung und endet mit "
        "Ablauf des gleichnamigen Kalendertags im Folgejahr (§ 108 AO, §§ 187, 188 BGB); "
        "ein Verkauf an diesem Tag ist noch steuerpflichtig.",
        "Zeitpunkte: Zeitstempel der Börse, umgerechnet nach UTC; Datum nach UTC.",
        "Kurse: EUR-Tageskurs der OKX-Public-API; wo am selben Tag ein eigener "
        "Umtausch gegen EUR stattfand, dessen tatsächlicher Kurs. Wallet-Bestände mit "
        "dem tatsächlich gezahlten Einstandswert.",
        "Tausch Krypto gegen Krypto ist eine Veräußerung; in Krypto gezahlte Gebühren "
        "und Funding sind Veräußerungen der hingegebenen Kryptowerte (§ 23 EStG).",
        "Gebühren bei Tauschgeschäften: beim Kauf Anschaffungsnebenkosten, beim Verkauf "
        "Veräußerungskosten.",
        "Futures (Perpetuals): Termingeschäfte nach § 20 Abs. 2 S. 1 Nr. 3 EStG; "
        "realisierter Gewinn/Verlust, Gebühren und Funding fließen in das Ergebnis. "
        "Margin-Bewegungen sind keine Abgänge.",
        "Umbuchungen zwischen eigenen Konten und mit der eigenen Trezor sind keine "
        "Veräußerung; das ursprüngliche Anschaffungsdatum bleibt erhalten.",
        "Freigrenze (§ 23: 1.000 € ab 2024, davor 600 €; § 22 Nr. 3: 256 €) wird vor "
        "einem Verlustvortrag geprüft (BFH IX R 27/04).",
        "Erhaltene Prämien/Airdrops („Received“) sind vorläufig § 22 Nr. 3 zugeordnet; "
        "Zuordnung je Buchung änderbar.",
    ]


# --- Verkaufs-Simulator ------------------------------------------------------

async def simulate_sale(conn, asset, qty=None, price_eur=None, sale_date=None,
                        fetcher=None, tax_rate=None):
    """Was passiert steuerlich, wenn ich jetzt (oder an einem Datum) verkaufe?
    Rechnet auf den offenen FIFO-Lots, ohne etwas zu buchen."""
    from datetime import date as _date
    asset = (asset or "").upper().strip()
    sale_date = _date.fromisoformat(sale_date) if sale_date else \
        datetime.now(timezone.utc).date()
    rep = await build_report(conn, fetcher=fetcher)
    lots = [l for l in rep["openLots"] if l["asset"] == asset]
    if not lots:
        return {"__error__": f"Kein offener Bestand in {asset}."}
    held = sum((L.D(l["qty"]) for l in lots), Decimal(0))
    qty = held if qty in (None, "") else L.D(qty)
    if qty <= 0:
        return {"__error__": "Menge muss größer als 0 sein."}
    if price_eur in (None, ""):
        rate = next((l.get("rate_now") for l in lots if l.get("rate_now")), None)
        if rate is None:
            return {"__error__": "Kein aktueller Kurs verfügbar – bitte Preis eingeben."}
        price = L.D(rate)
    else:
        price = L.D(price_eur)

    need, parts = qty, []
    for l in lots:                       # schon nach Kaufdatum sortiert (FIFO)
        if need <= 0:
            break
        lq = L.D(l["qty"])
        take = min(need, lq)
        cost = (L.D(l["cost_eur"]) * take / lq) if l["cost_eur"] not in (None, "") else None
        proceeds = price * take
        taxable = innerhalb_haltefrist(l["acquired"], sale_date)
        parts.append({
            "acquired": l["acquired"], "qty": take, "cost": cost, "proceeds": proceeds,
            "gain": (proceeds - cost) if cost is not None else None,
            "taxable": taxable,
            "freeOn": (haltefrist_ende(l["acquired"]) + timedelta(days=1)).isoformat(),
            "account": l.get("account"),
        })
        need -= take

    D0 = Decimal(0)
    tax_gain = sum((p["gain"] for p in parts if p["taxable"] and p["gain"] is not None), D0)
    free_gain = sum((p["gain"] for p in parts if not p["taxable"] and p["gain"] is not None), D0)
    year = sale_date.year
    ycur = next((y for y in rep["years"] if y["year"] == year), None)
    gain_so_far = L.D(ycur["par23"]["gain"]) if ycur else D0
    carry = L.D(ycur["par23"]["lossCarryIn"]) if ycur else D0
    fg = freigrenze_23(year)
    before = apply_freigrenze(gain_so_far, carry, fg)
    after = apply_freigrenze(gain_so_far + tax_gain, carry, fg)
    extra_taxable = after["taxable"] - before["taxable"]

    # Wartevorteil: wann wird welcher steuerpflichtige Teil frei?
    waits, remaining = [], tax_gain
    for d in sorted({p["freeOn"] for p in parts if p["taxable"]}):
        freed = sum((p["gain"] for p in parts
                     if p["taxable"] and p["freeOn"] == d and p["gain"] is not None), D0)
        remaining -= freed
        waits.append({"date": d, "freedGain": freed, "remainingTaxableGain": remaining})

    rate = L.D(tax_rate) / 100 if tax_rate not in (None, "") else None
    return dec2str({
        "asset": asset, "qty": qty, "held": held, "price": price,
        "date": sale_date.isoformat(), "short": max(qty - held, D0),
        "parts": parts, "proceeds": sum((p["proceeds"] for p in parts), D0),
        "taxableGain": tax_gain, "taxFreeGain": free_gain,
        "year": year, "gainSoFar": gain_so_far, "freigrenze": fg,
        "exceededBefore": before["exceeded"], "exceededAfter": after["exceeded"],
        "extraTaxable": extra_taxable,
        "taxEstimate": (extra_taxable * rate) if rate is not None else None,
        "waits": waits[:6],
    })


# --- Portfolio fuer das Dashboard ------------------------------------------

async def portfolio(conn, positions=None, fetcher=None):
    """Bewertet die Bestaende in EUR und stellt sie dem Eingezahlten gegenueber.

    Bestaende ausserhalb von OKX (Konto 'wallet', z.B. Trezor) gehen mit in den
    Portfoliowert ein; ihr Einstandswert zaehlt als Eingezahlt, damit das
    Ergebnis den echten Gewinn/Verlust zeigt und nicht den ganzen Bestand als
    Gewinn."""
    today = _today_utc()
    balances = L.balances(conn)
    wallet_bal = L.balances(conn, account="wallet")
    assets = list(balances)
    table, missing, sources = await build_rate_lookup(conn, assets, [today], fetcher)

    # In Margin gebunden vs. frei verfuegbar
    in_margin = {}
    for p in (positions or []):
        ccy = p.get("settle_ccy") or "USDC"
        in_margin[ccy] = in_margin.get(ccy, Decimal(0)) + L.D(p.get("margin_balance", 0))

    holdings, total, wallet_total = [], Decimal(0), Decimal(0)
    for asset, amount in balances.items():
        if abs(amount) < Decimal("0.000000001"):
            continue
        eur = to_eur(table, asset, today, amount)
        if eur is not None:
            total += eur
        w_amt = wallet_bal.get(asset, Decimal(0))
        w_eur = to_eur(table, asset, today, w_amt) if w_amt else None
        if w_eur is not None:
            wallet_total += w_eur
        locked = in_margin.get(asset, Decimal(0))
        holdings.append({
            "asset": asset, "amount": amount, "eur": eur,
            "rate": table.get((asset, today)),
            "locked": min(locked, amount) if locked else Decimal(0),
            "free": amount - min(locked, amount) if locked else amount,
            "wallet": w_amt if w_amt else None,
            "walletEur": w_eur,
        })
    holdings.sort(key=lambda h: -(h["eur"] or Decimal(0)))

    # Einstandswert der Wallet-Bestaende (fester Einstandswert je Zugang).
    wallet_cost = Decimal(0)
    for r in conn.execute(
            """SELECT e.amount, e.eur_value FROM entries e
               JOIN transactions t ON t.id = e.transaction_id
               WHERE t.account = 'wallet' AND e.eur_value IS NOT NULL"""):
        wallet_cost += L.D(r["eur_value"])

    deposits = Decimal(0)
    for r in conn.execute(
            """SELECT e.asset, e.amount, e.eur_value, t.ts_utc FROM transactions t
               JOIN entries e ON e.transaction_id = t.id
               WHERE t.tx_type = 'deposit'"""):
        if r["eur_value"] not in (None, ""):
            deposits += L.D(r["eur_value"])
            continue
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
        "wallet": {
            "valueEur": wallet_total, "costEur": wallet_cost,
            "resultEur": (wallet_total - wallet_cost) if wallet_cost else None,
        } if wallet_bal else None,
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
                    + (f" ({native_label(n)})" if native_label(n) else "")
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


# --- Positions-Journal (aus der Position History) ---------------------------
#
# Reine Auswertung. Die Zahlen stammen aus OKX' eigener Zusammenfassung je
# Position und gehen nicht in Bestand oder Steuer ein - dort zaehlt allein die
# Trading History. EUR-Werte sind zum Tageskurs des Schliesstags umgerechnet,
# als Orientierung, nicht als steuerlicher Wert.

def _journal_ccy(rec):
    """Waehrung, in der OKX Pnl/Gebuehr/Funding dieser Position ausweist.
    Rueckgabe (anzeige, kurs_asset)."""
    m = (rec["margin_ccy"] or "").upper()
    if m:
        return m, m
    parts = rec["instrument"].upper().replace("_", "-").split("-")
    for c in ("USDT", "USDC"):
        if c in parts:
            return c, c
    # z.B. ETH-USD_UM_XPERP: in USD abgerechnet (Unified Margin). Fuer den
    # EUR-Wert wird der USDC-Kurs als Dollar-Naeherung genommen.
    return "USD", "USDC"


def _journal_item(rec):
    D = L.D
    inst = rec["instrument"]
    base = inst.split("-")[0].upper()
    ccy, rate_asset = _journal_ccy(rec)
    inverse = ccy == base  # Coin-margined: Kontrakt in USD, Ergebnis in Coin

    qty = D(rec["max_qty"] or 0)
    closed = D(rec["closed_qty"] or 0)
    unit = D(rec["face_value"] or 1) * D(rec["multiplier"] or 1)
    size = qty * unit
    lev = D(rec["leverage"] or 0)
    p_open = D(rec["avg_open"] or 0)

    pnl, fee = D(rec["pnl"]), D(rec["fee"])
    funding, liq = D(rec["funding_fee"]), D(rec["liq_fee"])
    net = pnl + fee + funding + liq

    margin = None
    if lev > 0 and p_open > 0 and size > 0:
        margin = (size / p_open / lev) if inverse else (size * p_open / lev)

    ratio = D(rec["pnl_ratio"]) if rec["pnl_ratio"] is not None else (
        net / margin if margin else None)

    t0 = datetime.fromisoformat(rec["opened_utc"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(rec["closed_utc"].replace("Z", "+00:00"))
    hold_days = Decimal(str(round((t1 - t0).total_seconds() / 86400, 2)))

    return {
        "key": rec["pos_key"], "instrument": inst, "base": base,
        "openedUtc": rec["opened_utc"], "closedUtc": rec["closed_utc"],
        "year": int(rec["closed_utc"][:4]),
        "direction": rec["direction"], "marginMode": rec["margin_mode"],
        "leverage": lev, "contracts": qty, "closedContracts": closed,
        "partial": closed < qty,
        "size": size, "sizeUnit": "USD" if inverse else base,
        "avgOpen": p_open, "avgClose": D(rec["avg_close"] or 0),
        "ccy": ccy, "rateAsset": rate_asset,
        "pnl": pnl, "fee": fee, "funding": funding, "liqFee": liq, "net": net,
        "margin": margin, "ratio": ratio, "holdDays": hold_days,
        "closeType": rec["close_type"],
    }


def _journal_summary(items):
    D0 = Decimal(0)
    n = len(items)
    wins = [i for i in items if i["net"] > 0]
    losses = [i for i in items if i["net"] < 0]
    by_ccy = {}
    for i in items:
        c = by_ccy.setdefault(i["ccy"], {"pnl": D0, "fee": D0, "funding": D0,
                                         "liqFee": D0, "net": D0, "n": 0})
        for k in ("pnl", "fee", "funding", "liqFee", "net"):
            c[k] += i[k]
        c["n"] += 1

    eur_ok = all(i.get("netEur") is not None for i in items)
    def esum(k):
        return sum((i[k] for i in items), D0) if eur_ok else None

    def rank(i):
        return i["netEur"] if eur_ok else i["net"]

    gross_eur = esum("pnlEur")
    fund_eur = esum("fundingEur")
    funding_share = None
    if gross_eur is not None and gross_eur > 0 and fund_eur is not None:
        funding_share = -fund_eur / gross_eur * 100

    sum_win = sum((rank(i) for i in wins), D0)
    sum_loss = sum((rank(i) for i in losses), D0)
    return {
        "count": n, "wins": len(wins), "losses": len(losses),
        "winRate": (Decimal(len(wins)) / n * 100) if n else None,
        "byCcy": [{"ccy": k, **v} for k, v in sorted(by_ccy.items())],
        "eurComplete": eur_ok,
        "netEur": esum("netEur"), "pnlEur": gross_eur, "feeEur": esum("feeEur"),
        "fundingEur": fund_eur, "liqFeeEur": esum("liqFeeEur"),
        "avgNetEur": (esum("netEur") / n) if (n and eur_ok) else None,
        "avgWin": (sum_win / len(wins)) if wins else None,
        "avgLoss": (sum_loss / len(losses)) if losses else None,
        "profitFactor": (sum_win / -sum_loss) if sum_loss < 0 else None,
        "fundingShare": funding_share,
        "avgHoldDays": (sum((i["holdDays"] for i in items), D0) / n) if n else None,
        "best": max(items, key=rank)["key"] if items else None,
        "worst": min(items, key=rank)["key"] if items else None,
        "rankIn": "EUR" if eur_ok else "Abrechnungswährung",
    }


async def journal(conn, fetcher=None):
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM closed_positions ORDER BY closed_utc DESC, instrument")]
    items = [_journal_item(r) for r in rows]

    pairs = [(i["rateAsset"], i["closedUtc"][:10]) for i in items]
    table, missing, _src = await build_rate_lookup(
        conn, None, None, fetcher=fetcher, pairs=pairs) if pairs else ({}, [], {})
    for i in items:
        rate = table.get((i["rateAsset"], i["closedUtc"][:10]))
        if i["rateAsset"] == BASE:
            rate = Decimal(1)
        i["eurRate"] = rate
        for k in ("pnl", "fee", "funding", "liqFee", "net"):
            i[k + "Eur"] = (i[k] * rate).quantize(Decimal("0.01")) if rate is not None else None

    years = sorted({i["year"] for i in items}, reverse=True)
    return {
        "items": items,
        "years": years,
        "all": _journal_summary(items),
        "byYear": {str(y): _journal_summary([i for i in items if i["year"] == y])
                   for y in years},
        "missingRates": missing,
        "lastImport": conn.execute(
            "SELECT MAX(imported_at) FROM closed_positions").fetchone()[0],
    }
