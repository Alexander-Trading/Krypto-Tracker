"""
Web-API-Bruecke fuer den Pyodide-Betrieb (kein Server, kein Terminal)
======================================================================

Macht inhaltlich dasselbe wie app.py's Handler-Klasse (do_GET/do_POST),
nur ohne HTTP-Drumherum: eine einzige dispatch()-Funktion, die JS direkt
aufruft. Datenbank liegt unter /persist/tracker.db - das ist ein Pfad im
virtuellen Dateisystem von Pyodide, den die JS-Seite per IndexedDB (IDBFS)
gemountet hat, bevor dieses Modul zum ersten Mal eine Verbindung oeffnet.

Rueckgabewert von dispatch() ist immer ein JSON-String. Bei Fehlern:
{"__error__": "Text"} statt einer Exception - Pyodide-Exceptions ueber die
JS-Bruecke zu reichen ist unnoetig fehleranfaellig, ein Fehlertext im JSON
reicht und wird von der JS-Seite genauso behandelt wie ein HTTP-Fehler.
"""

import json
import urllib.parse
import asyncio
import uuid
from decimal import Decimal

import ledger as L
import market as MK
import tax as TAX
import okx_import as IMP
import okx_api as OKX

try:
    import pdf_report as PDF
    PDF_AVAILABLE = True
except ImportError:
    PDF = None
    PDF_AVAILABLE = False

DB_PATH = "/persist/tracker.db"

_initialized = False


def _connect():
    global _initialized
    # Bewusst kein L.connect(): das setzt PRAGMA journal_mode=WAL, was
    # Shared-Memory-Dateien (-wal/-shm) braucht. Das virtuelle Dateisystem
    # von Pyodide (IndexedDB-basiert, IDBFS) unterstuetzt das nicht
    # zuverlaessig - Schreibvorgaenge landeten im WAL-File, ohne dass die
    # Hauptdatei je den neuen Stand sah. Ein Browser-Tab ist ohnehin
    # Single-User/Single-Connection, WAL bringt hier keinen Vorteil.
    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    if not _initialized:
        L.init_db(conn)
        _initialized = True
    return conn


def _dump(obj):
    def default(o):
        if isinstance(o, Decimal):
            return format(o, "f")
        raise TypeError(f"Nicht serialisierbar: {type(o)}")
    return json.dumps(obj, ensure_ascii=False, default=default)


def jsonable(value):
    return format(value, "f") if isinstance(value, Decimal) else value


# --- App-State (identisch zu app.py) ----------------------------------------

def set_state(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(value, ensure_ascii=False), L.now_utc()))
    conn.commit()


def get_state(conn, key):
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else None


def _mask_key(api_key):
    """Nie den vollen Key zurueck an die UI geben, nur genug zum
    Wiedererkennen ('...ab12')."""
    if not api_key or len(api_key) < 4:
        return "••••"
    return "••••" + api_key[-4:]


def build_state(conn):
    tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
    bal = [{"asset": a, "amount": jsonable(v)} for a, v in L.balances(conn).items()]

    buckets = L.tax_bucket_overview(conn)
    totals = {}
    for row in buckets:
        totals[row["tax_bucket"]] = totals.get(row["tax_bucket"], 0) + row["n"]

    span = conn.execute("SELECT MIN(ts_utc), MAX(ts_utc) FROM transactions").fetchone()

    return {
        "app": "krypto-tracker",
        "hasData": tx_count > 0,
        "txCount": tx_count,
        "balances": bal,
        "buckets": buckets,
        "bucketTotals": totals,
        "unknown": L.unknown_count(conn),
        "positions": get_state(conn, "positions") or [],
        "imports": [dict(r) for r in conn.execute(
            "SELECT filename, rows_read, rows_imported, started_at, note "
            "FROM import_runs ORDER BY id DESC LIMIT 8")],
        "from": span[0], "to": span[1],
    }


def analyse_csv(text):
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"error": "Die Datei enthaelt keine lesbaren Zeilen."}

    header = lines[0].lstrip("\ufeff")
    counts = {d: header.count(d) for d in [",", ";", "\t", "|"]}
    delim = max(counts, key=counts.get)
    if counts[delim] == 0:
        return {"error": "Kein Trennzeichen erkennbar. Ist das wirklich eine CSV-Datei?"}

    def split(line):
        out, cur, quoted = [], "", False
        for ch in line:
            if ch == '"':
                quoted = not quoted
            elif ch == delim and not quoted:
                out.append(cur.strip().strip('"'))
                cur = ""
            else:
                cur += ch
        out.append(cur.strip().strip('"'))
        return out

    columns = split(header)
    rows = [split(ln) for ln in lines[1:6]]
    ragged = [i + 2 for i, r in enumerate(rows) if len(r) != len(columns)]

    return {
        "delimiter": {",": "Komma", ";": "Semikolon", "\t": "Tabulator",
                      "|": "Pipe"}[delim],
        "columnCount": len(columns), "columns": columns, "rows": rows,
        "lineCount": len(lines) - 1, "ragged": ragged,
    }


DEMO = [
    ("2025-01-10T08:00:00Z", "deposit", "funding", "neutral", "dep-1",
     [("EUR", "5000", "principal")], "SEPA-Einzahlung"),
    ("2025-01-11T10:30:00Z", "trade_spot", "trading", "par23", "trade-1",
     [("USDC", "5150.00", "principal"), ("EUR", "-5000.00", "principal"),
      ("USDC", "-5.15", "fee")], "EUR/USDC bei Kurs 1.03"),
    ("2025-02-01T00:00:00Z", "earn_reward", "earn", "par22", "earn-1",
     [("USDC", "12.40", "principal")], "Praemie EUR-Einzahlung"),
    ("2025-02-14T15:22:00Z", "trade_derivative", "trading", "par20", "fut-1",
     [("USDC", "310.00", "pnl"), ("USDC", "-4.20", "fee")],
     "BTC-USDC-SWAP long geschlossen"),
    ("2025-02-14T16:00:00Z", "funding_fee", "trading", "par20", "fund-1",
     [("USDC", "-1.85", "funding")], "Finanzierungsrate"),
    ("2025-02-20T12:00:00Z", "adjustment", "trading", "unknown", "mystery-1",
     [("USDC", "3.00", "principal")], "Gutschrift unklarer Herkunft"),
]


def seed_demo(conn):
    for ts, tx_type, account, bucket, ext, entries, note in DEMO:
        L.add_transaction(conn, L.Transaction(
            ts_utc=ts, tx_type=tx_type, account=account, tax_bucket=bucket,
            source="demo", external_id=ext, note=note,
            entries=[L.Entry(a, amt, kind) for a, amt, kind in entries],
        ))
    conn.commit()


# --- Routen (Gegenstueck zu app.py's do_GET/do_POST) ------------------------

async def _route_get(path, query, conn):
    q = urllib.parse.parse_qs(query)

    if path == "/api/market":
        positions = get_state(conn, "positions") or []
        if not positions:
            return {"positions": [], "error": "Keine offene Position gefunden."}
        instmap = get_state(conn, "instmap") or {}
        out = []
        positions_changed = False
        for pos in positions:
            m = instmap.get(pos["instrument"]) or {}
            try:
                snap = await MK.snapshot(pos, m.get("instId"), m.get("instType"))
            except Exception as exc:
                snap = {"instrument": pos.get("instrument"), "errors": [str(exc)]}
            if snap.get("resolvedTo") and pos["instrument"] not in instmap:
                instmap[pos["instrument"]] = {
                    "instId": snap["resolvedTo"], "instType": snap.get("instType")}
                set_state(conn, "instmap", instmap)
            if snap.get("ctValCorrected"):
                # Dauerhaft uebernehmen, damit der Fehler nicht bei jedem
                # Laden erneut auftaucht, auch offline/ohne Live-Kurs.
                pos["ct_val"] = snap["ctVal"]
                pos["size"] = snap["size"]
                positions_changed = True
            out.append(snap)
        if positions_changed:
            set_state(conn, "positions", positions)
        return {"positions": out}

    if path == "/api/portfolio":
        return await TAX.portfolio(conn, get_state(conn, "positions") or [], MK.eur_rate_on)

    if path == "/api/tax":
        rep = await TAX.build_report(conn, fetcher=MK.eur_rate_on)
        fmt = (q.get("format") or [""])[0]
        if fmt == "csv":
            return {"__csv__": TAX.report_csv(rep)}
        if fmt == "pdf":
            if not PDF_AVAILABLE:
                return {"__error__":
                    "PDF-Export ist gerade nicht verfügbar - vermutlich konnte beim "
                    "Laden der Seite kein Netzwerkzugriff auf PyPI/jsDelivr hergestellt "
                    "werden (z.B. wegen einer Firewall oder eines Werbeblockers). Seite "
                    "neu laden und nochmal versuchen, oder CSV exportieren."}
            years = [y["year"] for y in rep["years"]]
            if not years:
                return {"__error__": "Keine Vorgänge für einen Report."}
            year = int((q.get("year") or [years[-1]])[0])
            span = conn.execute(
                "SELECT MIN(ts_utc), MAX(ts_utc) FROM transactions").fetchone()
            meta_text = (
                f"Krypto-Tracker · OKX · Zeitraum {span[0][:10]} bis {span[1][:10]} · "
                f"erstellt {rep['generatedAt'][:16].replace('T',' ')}")
            import base64
            body = PDF.build_pdf(rep, year, meta_text)
            return {"__pdf_base64__": base64.b64encode(body).decode("ascii"),
                    "__filename__": f"steuerreport-{year}.pdf"}
        return rep

    if path == "/api/capital-curve":
        return await TAX.capital_curve(conn, fetcher=MK.eur_rate_on)

    if path == "/api/fees":
        return TAX.fees_summary(conn)

    if path == "/api/losscarry":
        return {"items": [dict(r) for r in conn.execute(
            "SELECT year, bucket, amount_eur, note FROM loss_carryforward "
            "ORDER BY year DESC, bucket")]}

    if path == "/api/instruments":
        try:
            return {"items": await MK.search_instruments((q.get("q") or [""])[0])}
        except MK.MarketError as exc:
            return {"items": [], "error": str(exc)}

    if path == "/api/transactions":
        where, args = [], []
        if q.get("bucket"):
            where.append("t.tax_bucket = ?"); args.append(q["bucket"][0])
        if q.get("type"):
            where.append("t.tx_type = ?"); args.append(q["type"][0])
        if q.get("q"):
            where.append("(t.note LIKE ? OR t.external_id LIKE ?)")
            args += [f"%{q['q'][0]}%"] * 2
        clause = (" WHERE " + " AND ".join(where)) if where else ""

        total = conn.execute(f"SELECT COUNT(*) FROM transactions t{clause}", args).fetchone()[0]
        limit = min(int((q.get("limit") or [100])[0]), 500)
        offset = int((q.get("offset") or [0])[0])

        rows = conn.execute(
            f"""SELECT t.id, t.ts_utc, t.tx_type, t.account, t.tax_bucket,
                       t.note, t.external_id, t.source
                FROM transactions t{clause}
                ORDER BY t.ts_utc DESC, t.id DESC LIMIT ? OFFSET ?""",
            args + [limit, offset]).fetchall()

        items = []
        for r in rows:
            entries = [dict(e) for e in conn.execute(
                "SELECT asset, amount, kind FROM entries "
                "WHERE transaction_id = ? ORDER BY id", (r["id"],))]
            items.append({**dict(r), "entries": entries})
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    if path == "/api/state":
        return build_state(conn)

    if path == "/api/okx-status":
        creds = get_state(conn, "okx_credentials")
        return {"connected": bool(creds and creds.get("apiKey")),
                "apiKeyMasked": _mask_key(creds.get("apiKey")) if creds else None}

    return {"__error__": "Nicht gefunden", "__status__": 404}


async def _route_post(path, body, conn):
    if path == "/api/preview":
        return analyse_csv(body.get("text", ""))

    if path == "/api/import":
        name = (body.get("name") or "upload.csv").replace("/", "_")
        # Pyodide hat ein eigenes (temporaeres) In-Memory-FS unter /tmp - reicht,
        # okx_import.py braucht nur einen Pfad zum Lesen, nichts Dauerhaftes.
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            fp = Path(tmp) / name
            fp.write_text(body.get("text") or "", encoding="utf-8")
            result = IMP.import_file(conn, fp, source="okx_csv")
        if result.get("positions"):
            ps = [{k: (str(v) if isinstance(v, Decimal) else v)
                   for k, v in p.items()} for p in result["positions"]]
            set_state(conn, "positions", ps)
            result["positions"] = ps
        result["state"] = build_state(conn)
        return result

    if path == "/api/demo":
        seed_demo(conn)
        return build_state(conn)

    if path == "/api/reset":
        conn.executescript(
            "DELETE FROM entries; DELETE FROM transactions; "
            "DELETE FROM balance_snapshots; DELETE FROM import_runs; "
            "DELETE FROM app_state; DELETE FROM loss_carryforward;")
        conn.commit()
        return build_state(conn)

    if path == "/api/losscarry":
        TAX.set_loss_carryforward(
            conn, int(body["year"]), body["bucket"],
            L.D(body.get("amount") or 0), body.get("note") or "")
        return {"items": [dict(r) for r in conn.execute(
            "SELECT year, bucket, amount_eur, note FROM loss_carryforward "
            "ORDER BY year DESC, bucket")]}

    if path == "/api/tx-update":
        L.update_transaction(
            conn, int(body["id"]),
            tax_bucket=body.get("tax_bucket"), tx_type=body.get("tx_type"),
            note=body.get("note"))
        return {"ok": True, "state": build_state(conn)}

    if path == "/api/tx-add":
        entries_in = body.get("entries") or []
        if not entries_in:
            return {"__error__": "Mindestens ein Eintrag (Asset + Betrag) nötig."}
        entries = [L.Entry(e["asset"], e["amount"], e.get("kind") or "principal")
                  for e in entries_in]
        tx = L.Transaction(
            ts_utc=body["ts_utc"], tx_type=body["tx_type"],
            account=body.get("account") or "trading",
            tax_bucket=body["tax_bucket"], source="manual",
            external_id=f"manual-{uuid.uuid4().hex}",
            note=body.get("note") or None, entries=entries)
        tx_id = L.add_transaction(conn, tx)
        conn.commit()
        return {"ok": True, "id": tx_id, "state": build_state(conn)}

    if path == "/api/tx-delete":
        row = conn.execute("SELECT source FROM transactions WHERE id = ?",
                           (int(body["id"]),)).fetchone()
        if not row:
            return {"__error__": "Vorgang nicht gefunden."}
        if row["source"] != "manual":
            return {"__error__":
                "Nur manuell angelegte Buchungen lassen sich hier löschen. "
                "Importierte Buchungen bitte über „Alles löschen“ + Re-Import korrigieren."}
        L.delete_transaction(conn, int(body["id"]))
        return {"ok": True, "state": build_state(conn)}

    if path == "/api/instmap":
        instmap = get_state(conn, "instmap") or {}
        instmap[body["symbol"]] = {"instId": body["instId"],
                                   "instType": body.get("instType", "FUTURES")}
        set_state(conn, "instmap", instmap)
        return {"ok": True, "instmap": instmap}

    if path == "/api/okx-save":
        api_key = (body.get("apiKey") or "").strip()
        api_secret = (body.get("apiSecret") or "").strip()
        passphrase = (body.get("passphrase") or "").strip()
        if not (api_key and api_secret and passphrase):
            return {"__error__": "API-Key, Secret und Passphrase werden alle drei gebraucht."}
        set_state(conn, "okx_credentials",
                  {"apiKey": api_key, "apiSecret": api_secret, "passphrase": passphrase})
        return {"ok": True, "apiKeyMasked": _mask_key(api_key)}

    if path == "/api/okx-clear":
        conn.execute("DELETE FROM app_state WHERE key = 'okx_credentials'")
        conn.commit()
        return {"ok": True}

    if path == "/api/okx-test":
        creds = get_state(conn, "okx_credentials")
        try:
            info = await OKX.test_connection(creds)
        except OKX.OkxAuthError as exc:
            return {"__error__": str(exc), "errorKind": "auth"}
        except OKX.OkxApiError as exc:
            return {"__error__": str(exc), "errorKind": "network"}
        return {"ok": True, "account": info}

    if path == "/api/okx-positions":
        creds = get_state(conn, "okx_credentials")
        try:
            live = await OKX.positions(creds)
            live_balance = await OKX.balance(creds)
        except OKX.OkxAuthError as exc:
            return {"__error__": str(exc), "errorKind": "auth"}
        except OKX.OkxApiError as exc:
            return {"__error__": str(exc), "errorKind": "network"}
        set_state(conn, "positions", live)
        return {"ok": True, "positions": live, "balance": live_balance,
                "state": build_state(conn)}

    return {"__error__": "Nicht gefunden", "__status__": 404}


def validate_import(path):
    """Direkt von JS aufgerufen (nicht ueber dispatch): prueft eine
    hochgeladene .db-Datei, bevor sie /persist/tracker.db ersetzt."""
    return _dump(L.inspect_db_file(path))


# Ein Browser-Tab hat nur einen einzigen Nutzer, aber JS kann trotzdem zwei
# api()-Aufrufe uebereinander schicken (z.B. wenn irgendwo async-Code ohne
# await lief, oder bei einem Doppel-Tap). SQLite mit mehreren gleichzeitig
# offenen Schreibverbindungen auf derselben Datei quittiert das mit
# "database is locked" - vor allem im IDBFS-Dateisystem heikel. Dieser Lock
# serialisiert alle dispatch()-Aufrufe strikt nacheinander, unabhaengig
# davon, was JS an Reihenfolge einhaelt.
_db_lock = asyncio.Lock()


async def dispatch(method, path_with_query, body_json):
    """Einziger Einstiegspunkt von JS aus. method: 'GET'|'POST'.
    path_with_query: z.B. '/api/transactions?bucket=unknown&limit=50'.
    body_json: JSON-String oder leer. Async, weil Markt-/Kursabrufe im
    Browser ueber pyfetch laufen muessen (siehe market.py) - der Aufruf
    von JS aus bekommt dafuer automatisch ein Promise zurueck."""
    if "?" in path_with_query:
        path, query = path_with_query.split("?", 1)
    else:
        path, query = path_with_query, ""

    async with _db_lock:
        conn = _connect()
        try:
            if method == "GET":
                result = await _route_get(path, query, conn)
            else:
                body = json.loads(body_json) if body_json else {}
                result = await _route_post(path, body, conn)
            return _dump(result)
        except Exception as exc:
            return _dump({"__error__": str(exc)})
        finally:
            conn.close()
