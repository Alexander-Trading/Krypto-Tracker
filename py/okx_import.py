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
        earliest_open = min(opens, key=lambda r: r["Time"])
        base = inst.split("-")[0] if "-" in inst else inst

        # Netto-Kontrakte je Tag, end-of-day - fuer die Kapitalkurve. Ohne
        # das wuerde ein rueckwirkend berechnetes unrealisiertes Ergebnis
        # immer mit der HEUTIGEN (finalen) Positionsgroesse rechnen, auch
        # fuer Tage, an denen die Position noch viel kleiner war (z.B. beim
        # schrittweisen Aufbau) - das uebertreibt vergangene Kursausschlaege
        # kuenstlich, teils bis ins Negative.
        running = Decimal(0)
        by_day = {}
        for r in sorted(opens, key=lambda r: r["Time"]):
            s = Decimal(1) if r["Action"].strip() == "Buy" else Decimal(-1)
            running += L.D(r["Amount"]) * s
            by_day[to_utc(r["Time"], meta["tz_offset"])[:10]] = running
        size_history = [{"day": d, "contracts": str(c)}
                         for d, c in sorted(by_day.items())]

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
            # Fruehester Buy/Sell-Zeitpunkt fuer dieses Instrument in der
            # Datei - Naeherung fuer "seit wann offen" (kennt keine
            # zwischenzeitliche vollstaendige Glattstellung).
            "opened_at": to_utc(earliest_open["Time"], meta["tz_offset"]),
            "size_history": size_history,
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
    elif kind in ("deposit", "leer"):
        added = skipped = 0
        unknown = set()
    else:
        raise ImportError_(
            "Dateiformat nicht erkannt. Erwartet wird ein Export aus dem "
            "OKX Order Center (Funding History oder Trading History).")

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

    return {
        "kind": kind,
        "timezone": meta["tz_label"],
        "rowsRead": len(rows),
        "imported": added,
        "skipped": skipped,
        "unknown": sorted(unknown),
        "warnings": warnings,
        "positions": positions,
    }
