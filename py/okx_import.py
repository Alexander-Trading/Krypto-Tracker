"""
OKX-CSV-Importer
================

Liest die beiden Exporte aus dem Order Center:

  * Funding History  - Ein-/Auszahlungen und Umbuchungen des Funding-Kontos
  * Trading History  - Handel, Gebuehren, Funding-Raten des Trading-Kontos

Beide Dateien tragen in der ersten Zeile ihre Zeitzone. Die wird ausgelesen
und alles nach UTC umgerechnet - nicht darauf verlassen, dass beim Export
UTC eingestellt war.

Buchungslogik
-------------
Margin ist kein Abgang. Wenn du eine Futures-Position eroeffnest, wandert
Guthaben in die Positionsmargin, gehoert dir aber weiter. Gebucht wird
deshalb nur, was den Bestand wirklich veraendert: Gebuehren und Funding.
Wie viel gerade in Margin gebunden ist, merken wir uns getrennt.
"""

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import ledger as L

# Kontraktgroesse als Rueckfallwert, nur falls "Trading Unit" fehlt oder nicht
# lesbar ist. Der echte Wert kommt normalerweise direkt aus der CSV selbst -
# siehe _parse_trading_unit() - oder aus dem instruments-Endpunkt (market.py).
DEFAULT_CT_VAL = Decimal("0.0001")


def _parse_trading_unit(unit_str):
    """Die Spalte 'Trading Unit' im echten OKX-Export sagt direkt, was ein
    Kontrakt wert ist - z.B. '0.001 ETH' bedeutet: 1 Kontrakt = 0.001 ETH.
    Nur nutzbar, wenn dort wirklich eine Zahl vor dem Kuerzel steht. Steht
    dort nur das Kuerzel ohne Zahl (z.B. einfach 'BTC'), ist NICHT gesagt,
    dass Amount schon direkt in der Basiswaehrung ist - das war ein Fehlschluss
    und hat BTC falsch angezeigt. In dem Fall lieber None zurueckgeben und
    auf den bekannten Schaetzwert zurueckfallen, statt zu raten."""
    if not unit_str:
        return None
    m = re.match(r"^\s*([\d.]+)\s*[A-Za-z]", unit_str.strip())
    if m:
        try:
            return Decimal(m.group(1))
        except Exception:
            return None
    return None  # nur ein Kuerzel, keine Zahl davor -> auf Schaetzwert zurueckfallen


class ImportError_(Exception):
    pass


# --- Kopfzeile -------------------------------------------------------------

def parse_meta(first_line):
    """Liest 'UID:XX,Account Type:Main,Time Zone:UTC+8' aus."""
    meta = {}
    for part in first_line.lstrip("\ufeff").split(","):
        if ":" in part:
            k, v = part.split(":", 1)
            meta[k.strip()] = v.strip()

    offset = timedelta(0)
    tz_raw = next((v for k, v in meta.items()
                   if "time zone" in k.lower() or "zeitzone" in k.lower()), "")
    m = re.search(r"(?:UTC|GMT)\s*([+-]?\d+)", tz_raw, re.I)
    if m:
        offset = timedelta(hours=int(m.group(1)))
    return {"raw": meta, "tz_label": tz_raw or "unbekannt",
            "tz_offset": offset, "tz_found": bool(m)}


def to_utc(time_str, offset):
    """'2026-08-18 16:00:02' plus Zeitzonenversatz -> ISO-UTC."""
    naive = datetime.strptime(time_str.strip(), "%Y-%m-%d %H:%M:%S")
    return (naive - offset).replace(tzinfo=timezone.utc) \
        .isoformat(timespec="seconds").replace("+00:00", "Z")


def read_okx_csv(path):
    """Gibt (meta, rows) zurueck. Zeile 1 ist die Kopfzeile mit den Metadaten,
    Zeile 2 sind die Spaltennamen."""
    text = Path(path).read_text(encoding="utf-8-sig")
    lines = text.splitlines()
    if len(lines) < 2:
        raise ImportError_("Die Datei hat weniger als zwei Zeilen.")

    meta = parse_meta(lines[0])

    # Manche Exporte schieben eine Leerzeile zwischen Kopfzeile und
    # Spaltennamen. Die wuerde sonst als Spaltenzeile gelesen.
    rest = lines[1:]
    while rest and not rest[0].strip():
        rest = rest[1:]
    if not rest:
        return meta, []

    rows = list(csv.DictReader(io.StringIO("\n".join(rest))))

    def has_content(row):
        for v in row.values():
            if isinstance(v, list):
                if any((x or "").strip() for x in v):
                    return True
            elif (v or "").strip():
                return True
        return False

    return meta, [r for r in rows if has_content(r)]


def detect_kind(rows):
    if not rows:
        return "leer"
    cols = set(rows[0].keys())
    if {"Position Create Time", "Position Update Time", "Pnl"} <= cols:
        return "positions"
    if {"Trade Type", "Position Balance"} <= cols:
        return "trading"
    if {"Type", "Before Balance", "After Balance"} <= cols:
        return "funding"
    if {"Einzahlungsadresse"} <= cols or {"Deposit address"} <= cols:
        return "deposit"
    return "unbekannt"


# --- Funding History -------------------------------------------------------

FUNDING_MAP = {
    # OKX-Bezeichnung -> (tx_type, tax_bucket)
    "deposit":                    ("deposit", "neutral"),
    "withdrawal":                 ("withdrawal", "neutral"),
    "to unified trading account": ("transfer", "neutral"),
    "from unified trading account": ("transfer", "neutral"),
    "transfer in":                ("transfer", "neutral"),
    "transfer out":               ("transfer", "neutral"),
    # "Received" ist OKX' Sammelbegriff fuer Gutschriften ohne eigene
    # Gegenleistung: Cashback, Prämien, Referral-Boni, Airdrops. Best-guess-
    # Einordnung als sonstige Einkuenfte (§22 Nr. 3) - wie bei allen
    # Steuertopf-Zuordnungen in diesem Tool keine gesicherte Rechtslage,
    # siehe Hinweis auf dem Report-Deckblatt. Bei Bedarf im "Prüfen"-Tab auf
    # einen anderen Topf umbuchen.
    "received":                   ("earn_reward", "par22"),
}


def import_funding(conn, rows, meta, source):
    added, skipped, unknown_types = 0, 0, set()

    for r in rows:
        raw_type = (r.get("Type") or "").strip()
        key = raw_type.lower()
        tx_type, bucket = FUNDING_MAP.get(key, ("adjustment", "unknown"))
        if key not in FUNDING_MAP:
            unknown_types.add(raw_type)

        tx = L.Transaction(
            ts_utc=to_utc(r["Time"], meta["tz_offset"]),
            tx_type=tx_type, account="funding", tax_bucket=bucket,
            source=source, external_id=f"fund-{r['id']}",
            note=raw_type, raw=r,
            entries=[L.Entry(r["Symbol"], L.D(r["Amount"]))],
        )
        if L.add_transaction(conn, tx) is not None:
            added += 1
        else:
            skipped += 1
    return added, skipped, unknown_types


# --- Trading History -------------------------------------------------------

def import_trading(conn, rows, meta, source):
    added, skipped, unknown = 0, 0, set()
    off = meta["tz_offset"]

    # Convert-Buchungen kommen als zwei Zeilen mit derselben Order id und
    # gehoeren zu einer Transaktion zusammen.
    spot_groups = {}
    for r in rows:
        if (r.get("Trade Type") or "").strip() == "Spot":
            spot_groups.setdefault(r["Order id"], []).append(r)

    for order_id, legs in spot_groups.items():
        entries = [L.Entry(leg["Balance Unit"], L.D(leg["Balance Change"]))
                   for leg in legs if L.D(leg["Balance Change"]) != 0]
        for leg in legs:
            if L.D(leg["Fee"]) != 0:
                entries.append(L.Entry(leg["Fee Unit"], L.D(leg["Fee"]), "fee"))
        if not entries:
            continue

        first = legs[0]
        tx = L.Transaction(
            ts_utc=to_utc(first["Time"], off),
            tx_type="trade_spot", account="trading", tax_bucket="par23",
            source=source, external_id=f"spot-{order_id}",
            note=f"{first['Symbol']} @ {first['Filled Price']}",
            raw={"legs": legs},
            entries=entries,
        )
        if L.add_transaction(conn, tx) is not None:
            added += 1
        else:
            skipped += 1

    for r in rows:
        trade_type = (r.get("Trade Type") or "").strip()
        action = (r.get("Action") or "").strip()
        if trade_type == "Spot":
            continue  # oben schon verarbeitet

        ts = to_utc(r["Time"], off)
        ext = f"trade-{r['id']}"
        entries = []

        if trade_type == "Transfer":
            tx_type, bucket = "transfer", "neutral"
            chg = L.D(r["Balance Change"])
            if chg != 0:
                entries.append(L.Entry(r["Balance Unit"], chg))

        elif "Funding fee" in action:
            # Funding schlaegt sich in der Positionsmargin nieder, nicht im
            # freien Guthaben. In der Spalte Balance Change steht deshalb 0 -
            # der Betrag steht in PnL.
            tx_type, bucket = "funding_fee", "par20"
            amount = L.D(r["PnL"])
            if amount != 0:
                entries.append(L.Entry(r["Fee Unit"] or "USDC", amount, "funding"))

        elif trade_type == "Futures":
            # Der Margin-Umzug ist kein Abgang. Bestandswirksam sind nur
            # Gebuehr und realisierter Gewinn.
            tx_type, bucket = "trade_derivative", "par20"
            pnl = L.D(r["PnL"])
            fee = L.D(r["Fee"])
            if pnl != 0:
                entries.append(L.Entry(r["Fee Unit"] or "USDC", pnl, "pnl"))
            if fee != 0:
                entries.append(L.Entry(r["Fee Unit"] or "USDC", fee, "fee"))

        else:
            tx_type, bucket = "adjustment", "unknown"
            unknown.add(f"{trade_type} / {action}")
            chg = L.D(r["Balance Change"])
            if chg != 0:
                entries.append(L.Entry(r["Balance Unit"], chg))

        if not entries:
            continue  # z.B. Futures-Kauf ohne Gebuehr: nichts zu buchen

        tx = L.Transaction(
            ts_utc=ts, tx_type=tx_type, account="trading", tax_bucket=bucket,
            source=source, external_id=ext,
            note=f"{r['Symbol']} {action}".strip(), raw=r, entries=entries,
        )
        if L.add_transaction(conn, tx) is not None:
            added += 1
        else:
            skipped += 1

    return added, skipped, unknown


# --- Position History (nur Journal, keine Buchung) -------------------------

def _pos_key(inst, opened_utc, direction, margin_mode):
    return "|".join([inst, opened_utc, direction or "", margin_mode or ""])


def import_position_history(conn, rows, meta):
    """Speichert geschlossene Positionen fuer das Journal.

    Bewusst KEINE Transaktion: Gewinn, Gebuehren und Funding derselben
    Positionen kommen schon ueber die Trading History ins Ledger. Hier wird
    nur abgelegt, was OKX je Position zusammenfasst.

    Schluessel ist Instrument + Eroeffnungszeit + Richtung + Margin-Modus.
    Ein spaeterer Export derselben Position (z.B. nachdem sie weiter
    teilgeschlossen wurde) ueberschreibt den alten Stand, statt eine zweite
    Zeile anzulegen. Gibt (neu, aktualisiert, unveraendert) zurueck."""
    off = meta["tz_offset"]
    added = updated = same = 0
    fields = ("instrument", "opened_utc", "closed_utc", "direction", "margin_mode",
              "leverage", "max_qty", "closed_qty", "avg_open", "avg_close",
              "margin_ccy", "pnl", "pnl_ratio", "fee", "funding_fee", "liq_fee",
              "close_type", "face_value", "multiplier")
    for r in rows:
        g = lambda k: (r.get(k) or "").strip()
        inst = g("Instrument Name")
        if not inst or not g("Position Create Time"):
            continue
        num = lambda k: L.dstr(L.D(g(k) or "0")) if g(k) not in ("", "-") else None
        rec = {
            "instrument": inst,
            "opened_utc": to_utc(g("Position Create Time"), off),
            "closed_utc": to_utc(g("Position Update Time") or g("Position Create Time"), off),
            "direction": g("Direction").lower() or None,
            "margin_mode": g("Margin Mode").lower() or None,
            "leverage": num("Leverage"),
            "max_qty": num("Max Position Quantity"),
            "closed_qty": num("Total Close Quantity"),
            "avg_open": num("Average Open Price"),
            "avg_close": num("Average Close Price"),
            "margin_ccy": g("Margin Currency") if g("Margin Currency") not in ("", "-") else None,
            "pnl": num("Pnl") or "0",
            "pnl_ratio": num("Pnl Ratio"),
            "fee": num("Fee") or "0",
            "funding_fee": num("Funding Fee") or "0",
            "liq_fee": num("Liquidation Clearance Fee") or "0",
            "close_type": g("Type") or None,
            "face_value": num("Contract Face Value"),
            "multiplier": num("Contract Multiplier"),
        }
        key = _pos_key(inst, rec["opened_utc"], rec["direction"], rec["margin_mode"])
        old = conn.execute("SELECT * FROM closed_positions WHERE pos_key = ?",
                           (key,)).fetchone()
        if old and all((old[f] or None) == (rec[f] or None) for f in fields):
            same += 1
            continue
        conn.execute(
            f"""INSERT OR REPLACE INTO closed_positions
                (pos_key, {", ".join(fields)}, raw, imported_at)
                VALUES (?, {", ".join("?" for _ in fields)}, ?, ?)""",
            (key, *[rec[f] for f in fields],
             json.dumps(r, ensure_ascii=False), L.now_utc()))
        if old:
            updated += 1
        else:
            added += 1
    conn.commit()
    return added, updated, same


# --- Position aus der Trading History ableiten -----------------------------

def derive_positions(rows, meta, ct_val=DEFAULT_CT_VAL, ct_val_map=None):
    """Rekonstruiert offene Futures-Positionen, getrennt je Instrument.
    Geschlossene Positionen (Saldo null) fallen raus.

    ct_val_map: {instrument: Decimal} - von Hand bestaetigte oder von OKX live
    aufgeloeste Kontraktgroessen aus frueheren Sitzungen. Greift nur, wenn
    die Datei selbst keine 'Trading Unit' angibt (aeltere Exporte) - die
    Spalte in der aktuellen Datei ist immer die zuverlaessigste Quelle."""
    ct_val_map = ct_val_map or {}
    by_inst = {}
    for r in rows:
        if (r.get("Trade Type") or "").strip() != "Futures":
            continue
        by_inst.setdefault(r["Symbol"], []).append(r)

    out = []
    for inst, fut in by_inst.items():
        opens = [r for r in fut
                 if (r.get("Action") or "").strip() in ("Buy", "Sell")]
        if not opens:
            continue

        from_csv = _parse_trading_unit(opens[0].get("Trading Unit"))
        inst_ct_val = from_csv if from_csv is not None else ct_val_map.get(inst, ct_val)
        contracts = notional = margin = Decimal(0)
        for r in opens:
            side = Decimal(1) if r["Action"].strip() == "Buy" else Decimal(-1)
            c = L.D(r["Amount"])
            notional += c * inst_ct_val * L.D(r["Filled Price"])
            contracts += c * side
            margin += L.D(r["Position Change"])

        if contracts == 0:
            continue  # glattgestellt, keine offene Position mehr

        funding = sum((L.D(r["PnL"]) for r in fut
                       if "Funding fee" in r["Action"]), Decimal(0))
        fees = sum((L.D(r["Fee"]) for r in fut), Decimal(0))
        size = contracts * inst_ct_val
        latest = max(fut, key=lambda r: r["Time"])
        base = inst.split("-")[0] if "-" in inst else inst

        out.append({
            "instrument": inst,
            "base": base,
            "side": "long" if contracts > 0 else "short",
            "contracts": contracts,
            "size": size,
            "ct_val": inst_ct_val,
            "avg_entry": (notional / abs(size)) if size else Decimal(0),
            "initial_margin": margin,
            "funding": funding,
            "fees": fees,
            "margin_balance": margin + funding,
            "settle_ccy": latest["Fee Unit"] or "USDC",
            "as_of": to_utc(latest["Time"], meta["tz_offset"]),
        })

    return sorted(out, key=lambda p: p["instrument"])
# --- Einstieg --------------------------------------------------------------

def find_duplicate_run(conn, file_sha256):
    """Wurde exakt diese Datei (byteidentisch) schon einmal importiert?"""
    row = conn.execute(
        """SELECT id, started_at, filename, rows_imported FROM import_runs
           WHERE file_sha256 = ? ORDER BY id LIMIT 1""",
        (file_sha256,)).fetchone()
    return dict(row) if row else None


def import_file(conn, path, source=None):
    path = Path(path)
    # Stabile, dateinamen-unabhaengige Quelle: der Duplikatschutz beruht auf
    # (source, external_id). Wuerde der Dateiname mit einfliessen, wuerde ein
    # erneuter Export unter anderem Namen (z.B. "export (1).csv") dieselben
    # Vorgaenge einfach nochmal buchen.
    source = source or "okx_csv"
    file_sha256 = L.sha256_file(path)

    dupe = find_duplicate_run(conn, file_sha256)
    if dupe:
        conn.execute(
            """INSERT INTO import_runs (started_at, source, filename, file_sha256,
                                        rows_read, rows_imported, rows_skipped, note)
               VALUES (?, ?, ?, ?, 0, 0, 0, ?)""",
            (L.now_utc(), source, path.name, file_sha256,
             json.dumps({"duplicateOf": dupe["id"]}, ensure_ascii=False)))
        conn.commit()
        return {
            "kind": "duplicate", "duplicate": True,
            "duplicateOfRun": dupe["id"],
            "duplicateImportedAt": dupe["started_at"],
            "timezone": None, "rowsRead": 0, "imported": 0, "skipped": 0,
            "unknown": [], "positions": [],
            "warnings": [
                f"Diese Datei wurde bereits am {dupe['started_at'][:16].replace('T',' ')} "
                f"importiert (als „{dupe['filename']}“, {dupe['rows_imported']} Buchungen). "
                "Es wurde nichts erneut eingelesen."],
        }

    meta, rows = read_okx_csv(path)
    kind = detect_kind(rows)

    if kind == "funding":
        added, skipped, unknown = import_funding(conn, rows, meta, source)
    elif kind == "trading":
        added, skipped, unknown = import_trading(conn, rows, meta, source)
    elif kind == "positions":
        added, updated, skipped = import_position_history(conn, rows, meta)
        unknown = set()
    elif kind in ("deposit", "leer"):
        added = skipped = 0
        unknown = set()
    else:
        raise ImportError_(
            "Dateiformat nicht erkannt. Erwartet wird ein Export aus dem "
            "OKX Order Center (Funding History, Trading History oder "
            "Position History).")

    warnings = []
    if rows and not meta["tz_found"]:
        warnings.append(
            "In der Kopfzeile steht keine Zeitzone. Es wurde UTC angenommen. "
            "Falls der Export in einer anderen Zeitzone erstellt wurde, sind "
            "die Zeitstempel verschoben.")
    if kind == "leer":
        warnings.append("Die Datei enthält keine Datenzeilen, nur Spaltennamen.")

    conn.execute(
        """INSERT INTO import_runs (started_at, source, filename, file_sha256, rows_read,
                                    rows_imported, rows_skipped, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (L.now_utc(), source, path.name, file_sha256, len(rows), added, skipped,
         json.dumps({"kind": kind, "tz": meta["tz_label"],
                     "unknown": sorted(unknown)}, ensure_ascii=False)))
    conn.commit()

    ct_val_map = {}
    row = conn.execute("SELECT value FROM app_state WHERE key = 'instmap'").fetchone()
    if row:
        try:
            for inst, m in json.loads(row["value"]).items():
                if m.get("ctVal"):
                    ct_val_map[inst] = L.D(m["ctVal"])
        except (ValueError, TypeError, KeyError):
            pass
    positions = derive_positions(rows, meta, ct_val_map=ct_val_map) if kind == "trading" else []

    extra = {}
    if kind == "positions":
        extra = {"updated": updated, "journalOnly": True}

    return {
        **extra,
        "kind": kind,
        "timezone": meta["tz_label"],
        "rowsRead": len(rows),
        "imported": added,
        "skipped": skipped,
        "unknown": sorted(unknown),
        "warnings": warnings,
        "positions": positions,
    }


# --- Wallet-Bestand (Trezor o.ae.) per Bestands-CSV --------------------------

WALLET_SOURCE = "wallet_csv"

_LOT_COLS = {
    "date":   {"datum", "date", "kaufdatum", "kaufdatum (original)"},
    "asset":  {"asset", "coin", "waehrung", "währung", "symbol", "token"},
    "qty":    {"menge", "amount", "anzahl", "qty", "quantity"},
    "cost":   {"eur-kosten", "eur_kosten", "eurkosten", "kosten", "kosten (eur)",
               "cost", "cost_eur", "einstandspreis", "einstand", "kaufpreis"},
    "origin": {"herkunft", "notiz", "note", "origin", "quelle", "bemerkung"},
}


def _lot_number(raw):
    """Zahl aus CSV-Text. Akzeptiert 1234.56, 1234,56, 1.234,56 und 1,234.56.
    Gibt None zurueck, wenn es keine Zahl ist."""
    t = (raw or "").strip().replace("€", "").replace("EUR", "").replace(" ", "")
    t = t.replace("'", "")
    if not t:
        return None
    if "," in t and "." in t:
        # Das zuletzt vorkommende Zeichen ist das Dezimalzeichen.
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")
        else:
            t = t.replace(",", "")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        return Decimal(t)
    except Exception:
        return None


def _lot_date(raw):
    """Datum als YYYY-MM-DD. Akzeptiert JJJJ-MM-TT, TT.MM.JJJJ, TT.MM.JJ und
    ISO-Zeitstempel."""
    t = (raw or "").strip()
    if not t:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y", "%Y/%m/%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(t[:10] if fmt.startswith("%Y-") else t, fmt) \
                .date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(t.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def parse_wallet_lots(text):
    """Liest die Bestands-CSV. Gibt (lots, problems) zurueck. Jede Zeile ist
    EIN Kauf mit seinem ursprünglichen Kaufdatum und Kaufpreis - nicht das
    Datum des Transfers auf die Wallet. Fehlerhafte Zeilen werden mit
    Zeilennummer gemeldet und uebersprungen, die uebrigen trotzdem gelesen."""
    text = (text or "").lstrip("﻿")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise ImportError_("Die Datei enthält keine Zeilen.")

    header_line = lines[0]
    counts = {d: header_line.count(d) for d in (";", ",", "\t")}
    delim = max(counts, key=counts.get)
    if counts[delim] == 0:
        raise ImportError_(
            "Kein Trennzeichen erkennbar. Erwartet: Datum, Asset, Menge, "
            "EUR-Kosten, Herkunft (getrennt durch Komma oder Semikolon).")

    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    rows = list(reader)
    header = [h.strip().lower() for h in rows[0]]
    idx = {}
    for key, names in _LOT_COLS.items():
        for i, h in enumerate(header):
            if h in names:
                idx[key] = i
                break
    missing = [k for k in ("date", "asset", "qty", "cost") if k not in idx]
    if missing:
        label = {"date": "Datum", "asset": "Asset", "qty": "Menge",
                 "cost": "EUR-Kosten"}
        raise ImportError_(
            "Das ist keine Bestands-CSV. Es fehlen die Spalten: "
            + ", ".join(label[m] for m in missing)
            + ". Erwartet: Datum, Asset, Menge, EUR-Kosten, Herkunft.")

    def cell(r, key):
        i = idx.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    today = datetime.now(timezone.utc).date().isoformat()
    lots, problems = [], []
    for n, r in enumerate(rows[1:], start=2):
        d = _lot_date(cell(r, "date"))
        asset = cell(r, "asset").upper()
        qty = _lot_number(cell(r, "qty"))
        cost = _lot_number(cell(r, "cost"))
        origin = cell(r, "origin")

        err = None
        if d is None:
            err = f"Datum „{cell(r, 'date')}“ nicht lesbar (erwartet JJJJ-MM-TT)"
        elif d > today:
            err = f"Datum {d} liegt in der Zukunft"
        elif not re.fullmatch(r"[A-Z0-9]{2,15}", asset):
            err = f"Asset „{cell(r, 'asset')}“ ungültig"
        elif asset == "EUR":
            err = "EUR ist kein Krypto-Bestand"
        elif qty is None or qty <= 0:
            err = f"Menge „{cell(r, 'qty')}“ ist keine positive Zahl"
        elif cost is None or cost < 0:
            err = f"EUR-Kosten „{cell(r, 'cost')}“ keine gültige Zahl"
        if err:
            problems.append({"line": n, "problem": err})
            continue
        lots.append({"line": n, "date": d, "asset": asset, "qty": qty,
                     "cost": cost, "origin": origin})
    return lots, problems


def import_wallet_lots(conn, text, filename="bestand.csv"):
    """Ersetzt den bisherigen Wallet-Bestand durch den Inhalt der CSV.

    Die Datei gilt als vollstaendiger Stand des Bestands. Fruehere Zeilen aus
    einer Bestands-CSV werden deshalb zuerst entfernt - so entstehen weder
    Doppelbuchungen noch bleiben korrigierte Zeilen in alter Fassung stehen.
    Von Hand angelegte Buchungen und OKX-Importe bleiben unberuehrt."""
    lots, problems = parse_wallet_lots(text)
    if not lots:
        return {
            "kind": "wallet", "timezone": None, "rowsRead": len(problems),
            "imported": 0, "skipped": len(problems), "replaced": 0,
            "problems": problems, "unknown": [], "positions": [],
            "warnings": ["Keine gültige Zeile gefunden. Der bisherige Bestand "
                         "wurde nicht verändert."],
        }

    replaced = L.delete_by_source(conn, WALLET_SOURCE)

    seen = {}
    for lot in lots:
        key = (lot["date"], lot["asset"], L.dstr(lot["qty"]), L.dstr(lot["cost"]),
               lot["origin"])
        seen[key] = seen.get(key, 0) + 1
        ext = "lot-" + L.hashlib.sha1(
            ("|".join(map(str, key)) + f"#{seen[key]}").encode("utf-8")).hexdigest()[:16]
        L.add_transaction(conn, L.Transaction(
            ts_utc=f"{lot['date']}T00:00:00Z", tx_type="deposit",
            account="wallet", tax_bucket="neutral", source=WALLET_SOURCE,
            external_id=ext,
            note=lot["origin"] or "Wallet-Bestand",
            entries=[L.Entry(lot["asset"], lot["qty"], "principal",
                             eur_value=lot["cost"])],
            raw={"line": lot["line"], "file": filename},
        ))
    conn.commit()

    warnings = []
    zero_cost = [l for l in lots if l["cost"] == 0]
    if zero_cost:
        warnings.append(
            f"{len(zero_cost)} Zeile(n) mit 0 € Kosten (z.B. Airdrop/Geschenk). "
            "Ein späterer Verkauf wird dann in voller Höhe als Gewinn gerechnet.")
    if replaced:
        warnings.append(
            f"{replaced} Zeile(n) aus einem früheren Bestandsimport wurden durch "
            "diese Datei ersetzt.")

    conn.execute(
        """INSERT INTO import_runs (started_at, source, filename, file_sha256, rows_read,
                                    rows_imported, rows_skipped, note)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (L.now_utc(), WALLET_SOURCE, filename, None, len(lots) + len(problems),
         len(lots), len(problems),
         json.dumps({"kind": "wallet", "tz": "UTC", "unknown": []},
                    ensure_ascii=False)))
    conn.commit()

    by_asset = {}
    for l in lots:
        a = by_asset.setdefault(l["asset"], {"qty": Decimal(0), "cost": Decimal(0), "n": 0})
        a["qty"] += l["qty"]; a["cost"] += l["cost"]; a["n"] += 1

    return {
        "kind": "wallet", "timezone": "UTC", "rowsRead": len(lots) + len(problems),
        "imported": len(lots), "skipped": len(problems), "replaced": replaced,
        "problems": problems, "unknown": [], "positions": [],
        "warnings": warnings,
        "summary": [{"asset": a, "qty": v["qty"], "cost": v["cost"], "count": v["n"]}
                    for a, v in sorted(by_asset.items())],
    }
