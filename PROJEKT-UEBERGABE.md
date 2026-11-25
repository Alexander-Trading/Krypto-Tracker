# Krypto-Tracker – Projektübergabe

Stand: 21.11.2026 (ursprünglich 27.10.2026). Zweck dieser Datei: In einem neuen Chat weiterarbeiten können, ohne
den alten Verlauf zu kennen. Im neuen Chat diese ZIP hochladen und schreiben:
„Lies PROJEKT-UEBERGABE.md und entpacke das Projekt, ich möchte etwas ändern."

## Was das Projekt ist
Persönlicher Portfolio- und Steuer-Tracker für Krypto (OKX-Handel + Trezor-Bestand),
Deutschland (§ 23, § 20, § 22 EStG). Läuft **komplett im Browser** (iPhone-Safari,
Homescreen-Symbol), kein Server. Gehostet als GitHub-Pages-Seite
`https://alexander-trading.github.io/Krypto-Tracker/` (Repo ist **öffentlich**).

**Datenschutz-Grundsatz:** Im Repo liegt nur Code. Die Daten (Datenbank) liegen im Browser
des Geräts (IndexedDB). Die Sicherungsdatei `.db` und die Bestands-CSV mit Kaufpreisen
**nie ins Repo hochladen**.

## Aufbau
```
index.html        Oberfläche + gesamtes JavaScript (eine Datei, ~1800 Zeilen)
py/               Python-Module, laufen im Browser über Pyodide
  ledger.py       Ledger-Kern: Buchen, Bestände, Konten, Decimal-Rechnung
  schema.sql      SQLite-Schema (Beträge als TEXT, immer Decimal, nie float)
  okx_import.py   OKX-CSV-Import + Wallet-Bestands-CSV-Import (import_wallet_lots)
  tax.py          FIFO-Lots, §23/§20/§22, Verlustvortrag, Portfolio, Report/CSV
  market.py       OKX-Public-API: Kurse, Kontrakte, Liquidation, EUR-Kurse
  webapi.py       dispatch(): Routen, die JS per api("/api/...") aufruft
  pdf_report.py   PDF-Report (reportlab, wird per micropip nachgeladen)
  checks.py       Datenprüfung, Saldo-Abgleich, Zuordnung Ein-/Auszahlungen (Prüfen-Tab)
pyodide/          Python-Laufzeit (WebAssembly), lokal ausgeliefert
pyodide-pkgs/     micropip
tests/            test_backend.py (ohne Netz), e2e_playwright.py (Browser, iPhone-Format)
extras/           oe-kaufkurs-karte.patch (entfernte Funktion, siehe unten)
```
`index.html` lädt beim Start diese Dateien aus `py/` (Liste `files` in der Pyodide-Startroutine)
und schreibt sie ins virtuelle Dateisystem. **Neue .py-Datei = in dieser Liste eintragen.**
Datenbank: `/persist/tracker.db` (IDBFS). Alle `api()`-Aufrufe laufen seriell über einen Lock.

## Datenmodell
- `transactions` (Ereignis: Zeit UTC, Typ, **Konto** `funding|trading|earn|wallet`,
  **Steuertopf** `par23|par20|par22|neutral|unknown`, Quelle, Notiz) und
  `entries` (Guthabenbewegungen: Asset, Betrag, Art, optional `eur_value`).
- `entries.eur_value`: bekannter Einstandswert (tatsächlich gezahlt). Hat in der FIFO-Rechnung
  Vorrang vor Tageskursen. Wird für den Wallet-Bestand genutzt.
- Quellen: `okx_csv`, `wallet_csv`, `manual`, `demo`.

## Trezor-/Wallet-Bestand (wichtigste Funktion dieser Runde)
- Import-Tab → „Bestand außerhalb von OKX". Format der Bestands-CSV (Komma oder Semikolon):
  `Datum,Asset,Menge,EUR-Kosten,Herkunft` – **eine Zeile je Einzelkauf** mit dem echten
  ursprünglichen Kaufdatum und Kaufpreis (nicht dem Transferdatum auf die Wallet).
  Datum JJJJ-MM-TT oder TT.MM.JJJJ, Zahlen mit Punkt oder Komma.
- Jeder Import **ersetzt** den bisherigen Wallet-Bestand komplett (Quelle `wallet_csv`).
  Fehlerhafte Zeilen werden mit Zeilennummer gemeldet und übersprungen.
- Gebucht als `deposit`, Konto `wallet`, Topf `neutral`, mit `eur_value`.
  **Keine künstliche EUR-Gegenbuchung** – die hat beim ersten Versuch den Portfoliowert
  verfälscht (Grund für den ersten Rückbau).
- Verkäufe von der Trezor: per „+ Buchung" mit Konto „Trezor / Wallet" (FIFO nimmt die ältesten Käufe).

## Journal (Position History) – neu am 27.10.2026
- Menüpunkt „Journal“ (`data-tab="journal"`). Import über das normale OKX-Feld: `detect_kind` erkennt
  die Position History an `Position Create Time`/`Position Update Time`/`Pnl`.
- Gespeichert in eigener Tabelle `closed_positions` (schema.sql), **nie** als Transaktion: Gewinn,
  Gebühren und Funding kommen schon über die Trading History, sonst Doppelzählung.
- Schlüssel: Instrument + Eröffnung UTC + Richtung + Margin-Modus. Neuer Export ersetzt den alten Stand.
- Auswertung `tax.journal()`: Netto = Pnl + Fee + Funding + Liquidationsgebühr; EUR zum Tageskurs
  des Schließtags (USD-Abrechnung über USDC). Kennzahlen je Jahr und gesamt.
- Routen: `GET /api/journal`, `POST /api/journal-clear`. „Alles löschen“ leert auch das Journal.
- Tests: `test_backend.py` (Abschnitt Position History), Browser: `tests/e2e_journal.py`.

## Wichtige Entscheidungen und Fallen
- **Portfolio:** `Eingezahlt` = Einzahlungen inkl. Wallet-Einstandswerte. Das Ergebnis zeigt so den echten Gewinn.
- **Unrealisiert-Kachel** enthält den Kursgewinn des Wallet-Bestands (Wert − Kaufpreis) plus offene Futures.
  Vorher fiel dieser Gewinn in den „Wechselkurseffekt" (Fehler, behoben). Der Hinweis unter den Kacheln
  zeigt nur noch den echten Rest.
- **Kurse:** `build_rate_lookup` bewertet nur benötigte (Asset, Tag)-Paare. Kurse von **heute** werden nicht
  dauerhaft gecacht (60 s Speicher), vergangene Tage schon (`price_cache`). Kein direktes EUR-Paar → Umweg über USDT/USDC.
- **Haltefrist-Anzeige:** Balken und Datum richten sich nach dem nächsten Kauf, der steuerfrei wird, nicht nach dem ältesten.
- **Navigation:** Keine Tab-Leiste mehr. 9-Punkte-Symbol neben Hell/Dunkel öffnet das Menü-Overlay
  (`#menuOverlay`) mit Icon-Kacheln (Klasse `tab`, `data-tab`) und den Aktionen Datenbank sichern/laden,
  Alles löschen, Programm beenden (die IDs `btnDbExport`, `btnDbImport`, `btnReset`, `btnQuit` sitzen jetzt dort;
  `#dbFile` bleibt als verstecktes Eingabefeld). Seitenname steht unter dem Titel (`#pageName`).
- **CSS-Falle:** Eine Mobile-`@media`-Regel muss **nach** der Grundregel stehen, sonst wird sie überschrieben.
  Gilt z. B. für `.tile-v`. Kacheln: `white-space:nowrap` + Schrift nach Bildschirmbreite.
- **Bewusst entfernt:** Karte „Ø-Kaufkurs je Coin" (Kosten/Menge, jetzt, Abstand, Ergebnis). Der Code liegt als
  `extras/oe-kaufkurs-karte.patch` (Commit `bee82e4`); mit `git apply` wieder einspielbar.
- Offene Punkte aus früheren Runden: Verlustvortrag nur manuell; OKX-API-Anbindung bewusst nicht gebaut
  (nur CSV); PDF-Export braucht Netz (PyPI/jsDelivr) beim Laden; Live-Kurse laufen über `pyfetch` (CORS-abhängig).

## Tests
```
python3 tests/test_backend.py          # ohne Netz, synthetische Daten, soll "ALLE TESTS OK" melden
python3 -m http.server 8765 &          # im Projektordner
python3 tests/e2e_playwright.py bestand.csv out.png 390   # Browser-Test, OKX gemockt
```
Playwright/Chromium muss installiert sein. Browser-Tests immer auch mit 360 px Breite prüfen
(seitliches Scrollen und umbrechende €-Zeichen).

## Ideen-Liste (nicht gebaut)
1. Verkaufs-Simulator (FIFO-Vorschau, Freigrenze, „warten spart Steuer?") · 2. Verkauf/Transfer-Formular für die Trezor ·
3. Kapitalkurve mit Zeiträumen (historische Tageskurse) · 4. Sicherungs-Erinnerung („letzte Sicherung vor X Tagen") ·
5. Fristen-Übersicht (nächste steuerfreie Käufe) · 6. Verschlüsselte Sicherung / PIN-Sperre ·
7. Verlustvortrag automatisch · 8. Werte für Anlage SO/KAP · 9. Staking/Airdrops als §22 ·
10. Trezor-Saldo-Abgleich · 11. Rebalancing.

## Arbeitsweise, die sich bewährt hat
- Nach jeder Änderung: Python-Syntax prüfen, `tests/test_backend.py`, JS-Syntax (`node --check`), Browser-Test auf Handybreite.
- Ausgeliefert werden `index.html` + betroffene `py/*.py`; der Nutzer lädt sie selbst ins GitHub-Repo hoch
  (vom iPhone), danach Homescreen-Symbol neu hinzufügen. Der Code-Stand steht unten auf der Seite.
- Seit 21.11.2026 ist die Claude-GitHub-App verknüpft: Claude kann direkt pushen.
- Kein Steuerrat: Die Zuordnung ist eine Auffassung, keine gesicherte Rechtslage.

## Änderungen 21.11.2026 (direkt auf main gepusht, Claude hat jetzt Schreibzugriff)
- Steuerlogik: Umbuchungen keine Veräußerung; Haltefrist nach §§ 187/188 BGB (`haltefrist_ende`);
  Freigrenze vor Verlustvortrag (`apply_freigrenze`, BFH IX R 27/04); Gebühren bei § 23-Tausch als
  Anschaffungsneben-/Veräußerungskosten.
- Steuer-Tab: Kachel antippen → Detailansicht der Buchungen; Werte für Anlage SO/KAP; Rechenweg;
  Verkaufs-Simulator (`/api/simulate`).
- Prüfen-Tab: Datenprüfung, Saldo-Abgleich (Ist-Bestände OKX/Trezor), Ein-/Auszahlungen zuordnen
  (`flow_modes` in app_state: csv | carry | third bzw. external | carry; carry erzeugt Gegenbuchung
  mit source `auto_transfer`).
- **Selbst-Update:** `const APP_BUILD` in index.html bei JEDER Änderung an index.html hochzählen,
  sonst laden Safari/Homescreen die neue Oberfläche nicht.
- Ladezeit (22.11.): PDF-Bibliotheken erst beim ersten PDF-Export (`pyEnsurePdf`), py-Dateien
  parallel, Service Worker `sw.js` cached nur pyodide/ + pyodide-pkgs/ (bei neuer Pyodide-Version
  `CACHE` in sw.js hochzählen), Kursabrufe parallel (max. 6), fehlgeschlagene Kurse 24 h gemerkt
  (price_cache source='miss').
- 25.11.: Funding-Kosten-Auswertung im Positionen-Tab (`/api/funding`, tax.funding_analysis);
  Nachweise im Steuer-Tab: Bestand zum Stichtag und Herkunftsnachweis als PDF
  (`/api/statement?kind=holdings|origin&date=…&format=pdf`, py/statements.py).
- Offen: FIFO je Wallet, Sparerpauschbetrag einstellbar, echte EUR-Beträge statt Tageskurs,
  Datum nach deutscher Zeit statt UTC, OKX Earn/Staking, Verlustvortrag automatisch.

## Git-Stand alt (lokaler Branch `trezor-spot-bestand`, nicht gepusht)
`5c17fe5` Trezor-/Spot-Bestand · `8e4db05` Kursgewinn zu Unrealisiert · `106b738` Steuer-Layout mobil ·
`1669008` Übersicht lesbar · `a13f3a8` Menü mit 9-Punkte-Symbol · `e92b2f7` Fußleisten-Buttons entfernt ·
`bee82e4` Ø-Kaufkurs-Karte · `c86b1d6` Revert der Karte. Ausgangsstand im Repo: `18876e1`.
Diese ZIP enthält den Dateistand nach `c86b1d6` (ohne `.git`).
