"""
Ledger-Kern
===========

Buchen, Bestaende rechnen, gegen OKX abgleichen. Kennt keine CSV-Formate und
keine Steuerlogik - das kommt in eigenen Modulen darueber.

Alle Betraege sind Decimal. Niemals float, an keiner Stelle.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, getcontext
from pathlib import Path

# 34 signifikante Stellen. Krypto hat teilweise 18 Nachkommastellen, und wir
# wollen bei Summen ueber zehntausende Zeilen keine Rundungsdrift.
getcontext().prec = 34

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


# --- Vokabular -------------------------------------------------------------

ACCOUNTS = {"funding", "trading", "earn"}

TX_TYPES = {
    "deposit",          # Fiat oder Krypto von aussen rein
    "withdrawal",       # nach aussen raus
    "transfer",         # zwischen eigenen Konten, steuerlich neutral
    "trade_spot",       # z.B. EUR -> USDC
    "trade_derivative", # Futures: Eroeffnen/Schliessen
    "funding_fee",      # Perpetual-Finanzierungsrate, alle 8h
    "fee",              # freistehende Gebuehr ohne eigenen Trade
    "earn_reward",      # Zinsen, Praemien, Staking
    "adjustment",       # manuelle Korrektur, immer mit note
}

# Steuerliche Zuordnung. Bewusst als Feld gespeichert und nicht zur Laufzeit
# abgeleitet: Wenn sich die Rechtsauffassung aendert - etwa zur Frage, ob
# Krypto-Perpetuals wirklich Termingeschaefte nach Paragraph 20 sind -, wollen
# wir umbuchen koennen, ohne die Importe neu einzulesen.
TAX_BUCKETS = {
    "par23",    # Paragraph 23 EStG, privates Veraeusserungsgeschaeft
    "par20",    # Paragraph 20 EStG, Kapitalvermoegen / Termingeschaeft
    "par22",    # Paragraph 22 Nr. 3 EStG, sonstige Einkuenfte
    "neutral",  # kein Steuerereignis (Transfers, Einzahlungen)
    "unknown",  # noch nicht zugeordnet - blockiert den Report
}

ENTRY_KINDS = {"principal", "fee", "pnl", "funding"}


# --- Hilfen ----------------------------------------------------------------

def D(value) -> Decimal:
    """Alles zuverlaessig nach Decimal. Floats werden bewusst abgelehnt."""
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        raise TypeError(
            f"float {value!r} ist als Geldbetrag nicht zulaessig. "
            "Uebergib den Wert als String oder Decimal."
        )
    if value in (None, ""):
        return Decimal(0)
    return Decimal(str(value).strip().replace(",", "."))


def dstr(value: Decimal) -> str:
    """Kanonischer Dezimalstring fuer die DB, ohne Exponentialschreibweise."""
    return format(D(value).normalize(), "f")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def inspect_db_file(path):
    """Prueft eine fremde .db-Datei, bevor sie die eigene ersetzt (Import auf
    einem anderen Geraet). Oeffnet read-only, damit bei einer beschaedigten
    oder falschen Datei nichts angefasst wird."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"transactions", "entries"} <= tables:
            conn.close()
            return {"ok": False,
                    "error": "Das sieht nicht nach einer Krypto-Tracker-Datenbank aus."}
        tx_count = conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]
        span = conn.execute(
            "SELECT MIN(ts_utc), MAX(ts_utc) FROM transactions").fetchone()
        conn.close()
        return {"ok": True, "txCount": tx_count, "from": span[0], "to": span[1]}
    except Exception as exc:
        return {"ok": False, "error": f"Datei lässt sich nicht öffnen: {exc}"}


# --- Datenklassen ----------------------------------------------------------

@dataclass
class Entry:
    asset: str
    amount: Decimal
    kind: str = "principal"

    def __post_init__(self):
        self.amount = D(self.amount)
        self.asset = self.asset.upper().strip()
        if self.kind not in ENTRY_KINDS:
            raise ValueError(f"Unbekannte Entry-Art: {self.kind}")


@dataclass
class Transaction:
    ts_utc: str
    tx_type: str
    account: str
    tax_bucket: str
    source: str
    entries: list = field(default_factory=list)
    external_id: str = None
    note: str = None
    raw: dict = None

    def validate(self):
        if self.tx_type not in TX_TYPES:
            raise ValueError(f"Unbekannter Transaktionstyp: {self.tx_type}")
        if self.account not in ACCOUNTS:
            raise ValueError(f"Unbekanntes Konto: {self.account}")
        if self.tax_bucket not in TAX_BUCKETS:
            raise ValueError(f"Unbekannter Steuertopf: {self.tax_bucket}")
        if not self.entries:
            raise ValueError("Transaktion ohne Entries ist sinnlos")
        if not self.ts_utc.endswith("Z"):
            raise ValueError(f"Zeitstempel muss UTC sein (Z am Ende): {self.ts_utc}")


# --- Datenbank -------------------------------------------------------------

def connect(db_path="tracker.db") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    _migrate(conn)


def _migrate(conn: sqlite3.Connection):
    """Kleine, idempotente Reparaturen am Datenbestand. Laeuft bei jedem Start.

    Fruehere Versionen haben den Dateinamen in die 'source'-Spalte gemischt
    (z.B. 'okx_csv:export (1).csv'). Damit greift der Duplikatschutz
    (UNIQUE source+external_id) nicht, wenn OKX beim erneuten Export einen
    anderen Dateinamen vergibt - derselbe Vorgang koennte doppelt gebucht
    werden. Wir vereinheitlichen die source auf eine stabile Kennung."""
    rows = conn.execute(
        "SELECT DISTINCT source FROM transactions WHERE source LIKE 'okx_csv:%'"
    ).fetchall()
    if not rows:
        return
    for row in rows:
        old = row["source"]
        try:
            conn.execute(
                "UPDATE transactions SET source = 'okx_csv' WHERE source = ?",
                (old,))
        except sqlite3.IntegrityError:
            # Echter Konflikt: gleiche external_id existiert schon unter
            # 'okx_csv'. Das waere ein tatsaechliches Duplikat - die alte,
            # dateinamen-gebundene Zeile bleibt dann unangetastet stehen,
            # statt Daten zu verlieren.
            pass
    conn.commit()


def add_transaction(conn: sqlite3.Connection, tx: Transaction) -> int | None:
    """Bucht eine Transaktion. Gibt die ID zurueck, oder None wenn die
    external_id schon existiert. Damit ist wiederholtes Importieren derselben
    Datei folgenlos."""
    tx.validate()

    if tx.external_id:
        existing = conn.execute(
            "SELECT id FROM transactions WHERE source = ? AND external_id = ?",
            (tx.source, tx.external_id),
        ).fetchone()
        if existing:
            return None

    cur = conn.execute(
        """INSERT INTO transactions
           (ts_utc, tx_type, account, tax_bucket, source, external_id, note, raw, imported_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (tx.ts_utc, tx.tx_type, tx.account, tx.tax_bucket, tx.source,
         tx.external_id, tx.note,
         json.dumps(tx.raw, ensure_ascii=False) if tx.raw else None,
         now_utc()),
    )
    tx_id = cur.lastrowid

    conn.executemany(
        "INSERT INTO entries (transaction_id, asset, amount, kind) VALUES (?, ?, ?, ?)",
        [(tx_id, e.asset, dstr(e.amount), e.kind) for e in tx.entries],
    )
    return tx_id


# --- Auswertung ------------------------------------------------------------

def balances(conn, account=None, until=None) -> dict:
    """Bestand je Asset als Summe aller Entries. Bewusst in Python summiert:
    SUM() in SQL wuerde die TEXT-Betraege in Floats verwandeln."""
    sql = ("SELECT e.asset, e.amount FROM entries e "
           "JOIN transactions t ON t.id = e.transaction_id WHERE 1=1")
    params = []
    if account:
        sql += " AND t.account = ?"
        params.append(account)
    if until:
        sql += " AND t.ts_utc <= ?"
        params.append(until)

    out = {}
    for row in conn.execute(sql, params):
        out[row["asset"]] = out.get(row["asset"], Decimal(0)) + D(row["amount"])
    return {k: v for k, v in sorted(out.items()) if v != 0}


def reconcile(conn, reported: dict, account=None, tolerance="0.00000001") -> list:
    """Vergleicht unser Ledger mit den Kontostaenden, die OKX meldet.

    Das ist die wichtigste Funktion im ganzen Projekt. Eine Abweichung heisst,
    dass Transaktionen fehlen oder falsch gebucht sind - und ein Steuerreport
    auf luckenhaften Daten ist schlimmer als gar keiner, weil er plausibel
    aussieht."""
    tol = D(tolerance)
    ours = balances(conn, account=account)
    result = []

    for asset in sorted(set(ours) | set(reported)):
        mine = ours.get(asset, Decimal(0))
        theirs = D(reported.get(asset, 0))
        diff = mine - theirs
        result.append({
            "asset": asset,
            "ledger": mine,
            "okx": theirs,
            "diff": diff,
            "ok": abs(diff) <= tol,
        })
    return result


def tax_bucket_overview(conn) -> list:
    """Wie viele Transaktionen liegen in welchem Steuertopf. Alles unter
    'unknown' muss vor dem Report geklaert werden."""
    return [dict(r) for r in conn.execute(
        """SELECT tax_bucket, tx_type, COUNT(*) AS n,
                  MIN(ts_utc) AS von, MAX(ts_utc) AS bis
           FROM transactions
           GROUP BY tax_bucket, tx_type
           ORDER BY tax_bucket, tx_type"""
    )]


def update_transaction(conn, tx_id, tax_bucket=None, tx_type=None, note=None):
    """Ordnet eine Buchung von Hand neu ein - z.B. eine, die beim Import als
    'unknown' gelandet ist. Fasst weder Zeitpunkt noch Entries an, nur die
    Einordnung."""
    if tax_bucket is not None and tax_bucket not in TAX_BUCKETS:
        raise ValueError(f"Unbekannter Steuertopf: {tax_bucket}")
    if tx_type is not None and tx_type not in TX_TYPES:
        raise ValueError(f"Unbekannter Transaktionstyp: {tx_type}")

    row = conn.execute("SELECT id FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not row:
        raise ValueError(f"Vorgang {tx_id} existiert nicht.")

    fields, params = [], []
    if tax_bucket is not None:
        fields.append("tax_bucket = ?"); params.append(tax_bucket)
    if tx_type is not None:
        fields.append("tx_type = ?"); params.append(tx_type)
    if note is not None:
        fields.append("note = ?"); params.append(note)
    if not fields:
        return
    params.append(tx_id)
    conn.execute(f"UPDATE transactions SET {', '.join(fields)} WHERE id = ?", params)
    conn.commit()


def unknown_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM transactions WHERE tax_bucket = 'unknown'"
    ).fetchone()[0]
