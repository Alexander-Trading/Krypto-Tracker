-- ---------------------------------------------------------------------------
-- Ledger-Schema
--
-- Grundprinzip: Eine Transaktion ist ein Ereignis, Entries sind die einzelnen
-- Guthabenbewegungen dazu. Ein Spot-Kauf hat drei Entries (+Coin, -Zahlmittel,
-- -Gebuehr), ein Airdrop nur einen. Der Bestand eines Assets ist immer die
-- Summe aller Entries. Weicht er vom OKX-Kontostand ab, fehlen Daten.
--
-- Betraege stehen als TEXT in der DB, nicht als REAL. SQLite REAL ist ein
-- Float, und Floats verlieren bei Geldbetraegen Nachkommastellen. Wir speichern
-- exakte Dezimalstrings und rechnen in Python mit Decimal.
-- ---------------------------------------------------------------------------

PRAGMA foreign_keys = ON;


CREATE TABLE IF NOT EXISTS transactions (
    id            INTEGER PRIMARY KEY,

    ts_utc        TEXT    NOT NULL,   -- ISO8601 in UTC, z.B. 2025-03-14T09:12:44Z
    tx_type       TEXT    NOT NULL,   -- siehe TX_TYPES in ledger.py
    account       TEXT    NOT NULL,   -- funding | trading | earn
    tax_bucket    TEXT    NOT NULL,   -- par23 | par20 | par22 | neutral | unknown

    source        TEXT    NOT NULL,   -- okx_csv_bills | okx_api | manual
    external_id   TEXT,               -- billId / tradeId von OKX
    note          TEXT,
    raw           TEXT,               -- Originalzeile als JSON, unveraendert

    imported_at   TEXT    NOT NULL,

    -- Verhindert Duplikate beim erneuten Import derselben Datei.
    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_tx_ts     ON transactions (ts_utc);
CREATE INDEX IF NOT EXISTS idx_tx_bucket ON transactions (tax_bucket, ts_utc);


CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY,
    transaction_id  INTEGER NOT NULL REFERENCES transactions (id) ON DELETE CASCADE,

    asset           TEXT    NOT NULL,   -- EUR, USDC, BTC ...
    amount          TEXT    NOT NULL,   -- vorzeichenbehafteter Dezimalstring
    kind            TEXT    NOT NULL,   -- principal | fee | pnl | funding

    -- Wird spaeter vom Preis-Service gefuellt, nicht beim Import.
    eur_value       TEXT,
    price_source    TEXT
);

CREATE INDEX IF NOT EXISTS idx_entries_tx    ON entries (transaction_id);
CREATE INDEX IF NOT EXISTS idx_entries_asset ON entries (asset);


-- Kontostaende, wie OKX sie meldet. Dient nur dem Abgleich gegen unser
-- errechnetes Ledger, geht nie in die Steuerberechnung ein.
CREATE TABLE IF NOT EXISTS balance_snapshots (
    id        INTEGER PRIMARY KEY,
    ts_utc    TEXT NOT NULL,
    account   TEXT NOT NULL,
    asset     TEXT NOT NULL,
    amount    TEXT NOT NULL,
    UNIQUE (ts_utc, account, asset)
);


-- EUR-Kurse zum Transaktionszeitpunkt. Einmal geholt, fuer immer behalten:
-- ein Steuerreport muss in zwei Jahren dieselben Zahlen liefern wie heute.
CREATE TABLE IF NOT EXISTS price_cache (
    asset       TEXT NOT NULL,
    day         TEXT NOT NULL,   -- YYYY-MM-DD (UTC)
    eur_price   TEXT NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (asset, day, source)
);


-- Protokoll aller Importe. Bei einem Steuerreport muss nachvollziehbar sein,
-- woher jede Zahl stammt.
CREATE TABLE IF NOT EXISTS import_runs (
    id            INTEGER PRIMARY KEY,
    started_at    TEXT NOT NULL,
    source        TEXT NOT NULL,
    filename      TEXT,
    file_sha256   TEXT,
    rows_read     INTEGER DEFAULT 0,
    rows_imported INTEGER DEFAULT 0,
    rows_skipped  INTEGER DEFAULT 0,
    note          TEXT
);


-- Manuell eingetragener Verlustvortrag aus Vorjahren, je Steuertopf und Jahr,
-- fuer das er zur Verrechnung antritt. Wird nicht automatisch fortgeschrieben -
-- die Person traegt jedes Jahr ein, was laut eigener/vorheriger Erklaerung an
-- Verlust noch offen ist.
CREATE TABLE IF NOT EXISTS loss_carryforward (
    year        INTEGER NOT NULL,
    bucket      TEXT    NOT NULL,   -- par23 | par20 | par22
    amount_eur  TEXT    NOT NULL,   -- positive Zahl = Hoehe des Verlustvortrags
    note        TEXT,
    updated_at  TEXT    NOT NULL,
    PRIMARY KEY (year, bucket)
);


-- Kleiner Schluessel-Wert-Speicher fuer Dinge, die nicht ins Ledger gehoeren:
-- zuletzt abgeleitete Position, Einstellungen, zuletzt gesehener Kurs.
CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
