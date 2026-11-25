"""Backend-Test ohne Netz und ohne echte Daten.

Aufruf:  python3 tests/test_backend.py
Prueft: Wallet-Bestands-CSV-Import, Portfoliowert, Einstandswerte, Haltefristen,
FIFO-Verkauf, Ersetzen statt Duplizieren, fehlerhafte Zeilen. Kurse sind
erfundene Testwerte, die Daten sind synthetisch.
"""
import asyncio, sqlite3, sys
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "py"))
import ledger as L, okx_import as IMP, tax as TAX, webapi as W, checks as CHK, statements as ST

CSV = """Datum,Asset,Menge,EUR-Kosten,Herkunft
2021-01-10,BTC,0.5,10000.00,Testboerse A
2023-03-01,BTC,0.25,6000.00,Testboerse B
2026-09-01,BTC,0.1,6500.00,Testboerse C
2022-05-05,ETH,2,3000.00,Testboerse A
"""
PRICES = {"BTC": "50000", "ETH": "2000"}


async def fetcher(asset, day):
    return Decimal(PRICES[asset]) if asset in PRICES else None


def fresh():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    L.init_db(c)
    return c


async def main():
    conn = fresh()
    r = IMP.import_wallet_lots(conn, CSV, "t.csv")
    assert r["imported"] == 4 and not r["problems"], r
    assert "EUR" not in L.balances(conn), "keine kuenstliche EUR-Gegenbuchung"

    pf = await TAX.portfolio(conn, [], fetcher)
    expected_total = Decimal("0.85") * 50000 + 2 * 2000
    assert Decimal(pf["totalEur"]) == expected_total, pf["totalEur"]
    assert Decimal(pf["depositedEur"]) == Decimal("25500.00"), pf["depositedEur"]
    assert Decimal(pf["wallet"]["resultEur"]) == expected_total - Decimal("25500"), pf["wallet"]

    rep = await TAX.build_report(conn, fetcher)
    lots = {(l["asset"], l["acquired"]): l for l in rep["openLots"]}
    assert lots[("BTC", "2021-01-10")]["tax_free"] is True
    assert lots[("BTC", "2026-09-01")]["tax_free"] is False
    assert Decimal(lots[("BTC", "2021-01-10")]["cost_eur"]) == Decimal("10000.00")

    # FIFO: Verkauf von 0.6 BTC verbraucht 0.5 (2021) + 0.1 (2023)
    L.add_transaction(conn, L.Transaction(
        ts_utc="2026-10-01T10:00:00Z", tx_type="trade_spot", account="wallet",
        tax_bucket="par23", source="manual", external_id="t-sell", note="Test",
        entries=[L.Entry("BTC", "-0.6"), L.Entry("EUR", "30000")]))
    conn.commit()
    rep = await TAX.build_report(conn, fetcher)
    disp = [d for y in rep["years"] for d in y["par23"]["disposals"]]
    assert [d["acquired"] for d in disp] == ["2021-01-10", "2023-03-01"], disp
    assert all(d["taxable"] is False for d in disp)

    # Erneuter Import ersetzt statt zu verdoppeln
    r2 = IMP.import_wallet_lots(conn, CSV, "t2.csv")
    n = conn.execute("SELECT COUNT(*) FROM transactions WHERE source='wallet_csv'").fetchone()[0]
    assert n == 4 and r2["replaced"] == 4

    # Fehlerhafte Zeilen und deutsches Zahlenformat
    c2 = fresh()
    bad = ("Datum;Asset;Menge;EUR-Kosten;Herkunft\n"
           "28.10.2021;btc;0,5;\"18.933,72\";Kraken\n"
           "2099-01-01;BTC;1;1;x\n2022-01-01;BTC;abc;1;x\n2022-01-01;EUR;1;1;x\n")
    r3 = IMP.import_wallet_lots(c2, bad, "bad.csv")
    assert r3["imported"] == 1 and len(r3["problems"]) == 3
    row = c2.execute("SELECT amount, eur_value FROM entries").fetchone()
    assert (row["amount"], row["eur_value"]) == ("0.5", "18933.72")

    # Demo-Daten + Wallet: Portfoliowert wird nur um den Wallet-Wert groesser
    c3 = fresh(); W.seed_demo(c3)
    before = Decimal((await TAX.portfolio(c3, [], fetcher))["totalEur"])
    IMP.import_wallet_lots(c3, CSV)
    after = Decimal((await TAX.portfolio(c3, [], fetcher))["totalEur"])
    assert after - before == expected_total

    # Position History: nur Journal, keine Buchung; Wiederimport aktualisiert
    import tempfile
    HEAD = ("UID:1,Account Type:Main,Time Zone:UTC+1\n"
            "Position Create Time,Position Update Time,Business Line,Index,Instrument Name,"
            "Margin Mode,Direction,Leverage,Max Position Quantity,Total Close Quantity,"
            "Average Open Price,Average Close Price,Margin Currency,Pnl,Pnl Ratio,Fee,"
            "Funding Fee,Liquidation Clearance Fee,Type,Contract Face Value,Contract Multiplier\n")
    WIN = ("2026-08-20 07:40:36,2026-10-24 16:16:14,FUTURES,-,ETH-USD_UM_XPERP-310404,"
           "isolated,long,4,5979,5979,2257.908826,2300.48,-,254.53305,0.01405,-13.6273,"
           "-193.4661,0,Close all,0.001,1\n")
    LOSS = ("2025-03-01 10:00:00,2025-03-02 10:00:00,FUTURES,-,BTC-USDT-SWAP,cross,short,"
            "10,100,100,80000,81000,USDT,-100,-0.12,-5,-1,0,Close all,0.01,1\n")
    c4 = fresh()
    async def usdfx(asset, day):
        return Decimal("0.9") if asset in ("USDC", "USDT") else None
    with tempfile.TemporaryDirectory() as tmp:
        f1 = Path(tmp) / "pos.csv"; f1.write_text(HEAD + WIN + LOSS)
        r = IMP.import_file(c4, f1)
        assert r["kind"] == "positions" and r["imported"] == 2 and r["journalOnly"], r
        assert c4.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 0
        # Spaeterer Export mit geaenderter Zeile: ersetzt statt zu verdoppeln
        f2 = Path(tmp) / "pos2.csv"; f2.write_text(HEAD + WIN.replace("Close all", "Liquidation") + LOSS)
        r = IMP.import_file(c4, f2)
        assert r["imported"] == 0 and r["updated"] == 1 and r["skipped"] == 1, r
    j = await TAX.journal(c4, usdfx)
    assert len(j["items"]) == 2 and j["years"] == [2026, 2025]
    eth = next(i for i in j["items"] if i["base"] == "ETH")
    assert eth["ccy"] == "USD" and eth["rateAsset"] == "USDC" and eth["size"] == Decimal("5.979")
    assert eth["net"] == Decimal("254.53305") - Decimal("13.6273") - Decimal("193.4661")
    btc = next(i for i in j["items"] if i["base"] == "BTC")
    assert btc["net"] == Decimal("-106") and btc["netEur"] == Decimal("-95.40")
    a = j["all"]
    assert a["count"] == 2 and a["wins"] == 1 and a["losses"] == 1 and a["winRate"] == 50
    assert a["best"] == eth["key"] and a["worst"] == btc["key"]
    assert j["byYear"]["2025"]["count"] == 1
    W._dump(j)  # muss serialisierbar sein

    # --- Steuerkorrekturen 21.11.2026 -----------------------------------
    await steuer_korrekturen()
    await neue_funktionen()
    print("ALLE TESTS OK")


def _tx(c, ts, typ, acc, bucket, ents, ext):
    L.add_transaction(c, L.Transaction(
        ts_utc=ts, tx_type=typ, account=acc, tax_bucket=bucket,
        source="test", external_id=ext, entries=ents))


async def steuer_korrekturen():
    D = Decimal

    # 1) Umbuchung zwischen eigenen Konten ist keine Veraeusserung und
    #    startet die Haltefrist nicht neu.
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02")], "a")
    _tx(c, "2025-06-01T10:00:00Z", "transfer", "trading", "neutral",
        [L.Entry("BTC", "-0.02")], "b")
    _tx(c, "2025-06-01T10:00:00Z", "transfer", "funding", "neutral",
        [L.Entry("BTC", "0.02")], "c")
    d, o, w, n = TAX.build_lots(c, {("BTC", "2025-01-10"): D(50000),
                                   ("BTC", "2025-06-01"): D(60000)})
    assert d == [], d
    assert len(o) == 1 and o[0]["acquired"] == "2025-01-10" and o[0]["cost_eur"] == D(1000), o

    # 1b) Auszahlung (OKX -> eigene Wallet): keine Veraeusserung, Hinweis
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02")], "a")
    _tx(c, "2025-06-01T10:00:00Z", "withdrawal", "funding", "neutral",
        [L.Entry("BTC", "-0.02")], "b")
    d, o, w, n = TAX.build_lots(c, {("BTC", "2025-01-10"): D(50000),
                                   ("BTC", "2025-06-01"): D(60000)})
    assert d == [] and o == [] and len(n) == 1 and not w, (d, o, w, n)

    # 2) Haltefrist nach §§ 187, 188 BGB
    cases = [("2024-03-15", "2025-03-15", True),    # Jahrestag: noch steuerpflichtig
             ("2024-03-15", "2025-03-16", False),   # Folgetag: steuerfrei
             ("2024-01-10", "2025-01-09", True),    # Schaltjahr, 365 Tage
             ("2024-01-10", "2025-01-10", True),    # Schaltjahr, Jahrestag (366 Tage)
             ("2024-01-10", "2025-01-11", False),
             ("2024-02-29", "2025-02-28", True),    # 29.2. -> Fristende 28.2.
             ("2024-02-29", "2025-03-01", False)]
    for acq, sold, taxable in cases:
        assert TAX.innerhalb_haltefrist(acq, sold) is taxable, (acq, sold)
        c = fresh()
        _tx(c, acq + "T10:00:00Z", "trade_spot", "trading", "par23",
            [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02")], "a")
        _tx(c, sold + "T10:00:00Z", "trade_spot", "trading", "par23",
            [L.Entry("EUR", "2000"), L.Entry("BTC", "-0.02")], "b")
        d, o, w, n = TAX.build_lots(c, {(("BTC", acq)): D(50000), ("BTC", sold): D(100000)})
        assert d[0]["taxable"] is taxable, (acq, sold, d[0])
    assert str(TAX.haltefrist_ende("2024-03-15")) == "2025-03-15"

    # 3) Freigrenze vor Verlustvortrag (BFH IX R 27/04)
    r = TAX.apply_freigrenze(D(1500), D(800), D(1000))
    assert r["exceeded"] and r["taxable"] == D(700) and r["carryUsed"] == D(800), r
    r = TAX.apply_freigrenze(D(900), D(500), D(1000))
    assert not r["exceeded"] and r["taxable"] == 0 and r["carryRemaining"] == D(500), r
    r = TAX.apply_freigrenze(D(1200), D(5000), D(1000))
    assert r["taxable"] == 0 and r["carryUsed"] == D(1200) and r["carryRemaining"] == D(3800), r
    r = TAX.apply_freigrenze(D(-300), D(500), D(1000))
    assert r["taxable"] == 0 and r["carryUsed"] == 0, r
    # im Report
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02")], "a")
    _tx(c, "2025-03-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "2500"), L.Entry("BTC", "-0.02")], "b")
    TAX.set_loss_carryforward(c, 2025, "par23", "800")
    rep = await TAX.build_report(c, fetcher=None)
    y = next(v for v in rep["years"] if v["year"] == 2025)["par23"]
    assert D(y["gain"]) == D(1500) and D(y["taxable"]) == D(700) and y["exceeded"], y

    # 4) Gebuehren: EUR-Gebuehr beim Kauf und beim Verkauf mindern den Gewinn
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02"), L.Entry("EUR", "-10", "fee")], "a")
    _tx(c, "2025-03-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "1000"), L.Entry("BTC", "-0.02"), L.Entry("EUR", "-10", "fee")], "b")
    d, o, w, n = TAX.build_lots(c, {("BTC", "2025-01-10"): D(50000),
                                   ("BTC", "2025-03-10"): D(50000)})
    assert len(d) == 1 and d[0]["gain_eur"] == D(-20), d

    # 4b) Gebuehr in der gekauften Coin: Kostenbasis bleibt der volle Kaufpreis
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02"), L.Entry("BTC", "-0.0002", "fee")], "a")
    d, o, w, n = TAX.build_lots(c, {("BTC", "2025-01-10"): D(50000)})
    rest = sum((x["cost_eur"] for x in o), D(0))
    fee_gain = sum((x["gain_eur"] for x in d), D(0))
    # 1.000 bezahlt: Restbestand + Gebuehrenabgang ergeben zusammen 1.000 Kosten
    assert abs(rest - fee_gain - D(1000)) < D("0.01"), (rest, fee_gain)
    assert abs(rest - D("999.9")) < D("0.01"), rest

    # 4c) Futures-Gebuehren (§ 20) werden NICHT zusaetzlich in § 23 abgezogen
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("USDC", "1000")], "a")
    _tx(c, "2025-02-10T10:00:00Z", "trade_derivative", "trading", "par20",
        [L.Entry("USDC", "-5", "fee")], "b")
    d, o, w, n = TAX.build_lots(c, {("USDC", "2025-01-10"): D(1), ("USDC", "2025-02-10"): D(1)})
    assert len(d) == 1 and d[0]["gain_eur"] == 0, d
    print("Steuerkorrekturen OK")


async def neue_funktionen():
    D = Decimal
    RATES = {"BTC": D(50000), "USDC": D("0.9")}
    async def fx(asset, day):
        return RATES.get(asset)

    def base():
        c = fresh()
        _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
            [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02")], "buy")
        _tx(c, "2025-02-01T10:00:00Z", "withdrawal", "funding", "neutral",
            [L.Entry("BTC", "-0.01")], "wd")
        return c

    def wd_id(c):
        return c.execute("SELECT id FROM transactions WHERE external_id='wd'").fetchone()[0]

    # Umbuchung auf Trezor mit Kaufdaten: Lots bleiben, Trezor-Bestand entsteht
    c = base()
    fl = CHK.flows(c)
    assert len(fl) == 1 and fl[0]["mode"] == "csv", fl
    CHK.set_flow_mode(c, wd_id(c), "carry")
    assert L.balances(c, account="wallet") == {"BTC": D("0.01")}
    d, o, w, n = TAX.build_lots(c, {("BTC", "2025-01-10"): D(50000)})
    assert d == [] and sum(l["qty"] for l in o) == D("0.02") and not n, (d, o, n)
    assert all(l["acquired"] == "2025-01-10" for l in o)
    assert CHK.flows(c)[0]["mode"] == "carry"
    # zurueck auf Standard: Gegenbuchung weg, Auszahlung wieder Auszahlung
    CHK.set_flow_mode(c, wd_id(c), "csv")
    assert "BTC" not in L.balances(c, account="wallet")
    assert c.execute("SELECT tx_type FROM transactions WHERE external_id='wd'").fetchone()[0] == "withdrawal"

    # Zahlung an Dritte: Veraeusserung
    c = base()
    CHK.set_flow_mode(c, wd_id(c), "third")
    d, o, w, n = TAX.build_lots(c, {("BTC", "2025-01-10"): D(50000), ("BTC", "2025-02-01"): D(60000)})
    assert len(d) == 1 and d[0]["gain_eur"] == D(100) and d[0]["taxable"], d

    # Einzahlung von der Trezor: Kaufdaten aus der Bestands-CSV bleiben
    c = fresh()
    IMP.import_wallet_lots(c, "Datum,Asset,Menge,EUR-Kosten\n2021-01-10,BTC,0.5,10000\n", "t.csv")
    _tx(c, "2025-03-01T10:00:00Z", "deposit", "funding", "neutral", [L.Entry("BTC", "0.5")], "dep")
    dep = c.execute("SELECT id FROM transactions WHERE external_id='dep'").fetchone()[0]
    CHK.set_flow_mode(c, dep, "carry")
    d, o, w, n = TAX.build_lots(c, {})
    assert len(o) == 1 and o[0]["acquired"] == "2021-01-10" and o[0]["cost_eur"] == D(10000), o
    assert L.balances(c) == {"BTC": D("0.5")} and "BTC" not in L.balances(c, account="wallet")

    # Saldo-Abgleich
    c = base()
    CHK.set_actual_balances(c, okx={"btc": "0.01"}, wallet={"BTC": "0.009"})
    bc = CHK.balance_check(c)
    row = next(r for r in bc["rows"] if r["asset"] == "BTC")
    assert row["okx"]["ok"] is True and row["wallet"]["ok"] is False, row
    assert bc["mismatches"] == 1

    # Pruefungen: negativer Bestand und Doppelbuchung
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "1000"), L.Entry("BTC", "-0.02")], "x1")
    _tx(c, "2025-01-11T10:00:00Z", "fee", "trading", "par23", [L.Entry("BTC", "-0.001", "fee")], "x2")
    L.add_transaction(c, L.Transaction(ts_utc="2025-01-11T10:00:00Z", tx_type="fee",
        account="trading", tax_bucket="par23", source="manual", external_id="x3",
        entries=[L.Entry("BTC", "-0.001", "fee")]))
    kinds = {i["kind"] for i in CHK.data_checks(c)}
    assert {"negative", "duplicate"} <= kinds, kinds
    # zwei OKX-Importzeilen mit eigener ID (Teilausfuehrungen): keine Meldung
    c = fresh()
    for ext in ("trade-1", "trade-2"):
        _tx(c, "2026-10-15T15:02:00Z", "trade_derivative", "trading", "par20",
            [L.Entry("USDC", "-0.00408857", "fee")], ext)
    assert "duplicate" not in {i["kind"] for i in CHK.data_checks(c)}

    # Erklaerungswerte und Rechenweg im Report
    c = fresh()
    _tx(c, "2025-01-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("BTC", "0.02")], "a")
    _tx(c, "2025-03-10T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "1500"), L.Entry("BTC", "-0.02")], "b")
    rep = await TAX.build_report(c, fetcher=None)
    so = next(y for y in rep["years"] if y["year"] == 2025)["declaration"]["so23"]
    assert D(so["proceeds"]) == D(1500) and D(so["cost"]) == D(1000) and D(so["gain"]) == D(500), so
    assert len(rep["methodology"]) >= 5

    # Verkaufs-Simulator
    c = fresh()
    IMP.import_wallet_lots(c, "Datum,Asset,Menge,EUR-Kosten\n2025-01-10,BTC,0.01,500\n"
                           "2025-06-10,BTC,0.01,700\n", "t.csv")
    sim = await TAX.simulate_sale(c, "BTC", qty="0.015", price_eur="100000",
                                  sale_date="2026-03-01", fetcher=None, tax_rate="42")
    assert D(sim["taxFreeGain"]) == D(500) and D(sim["taxableGain"]) == D(150), sim
    assert sim["waits"][0]["date"] == "2026-06-11", sim["waits"]
    assert D(sim["extraTaxable"]) == 0 and not sim["exceededAfter"], sim   # 150 < Freigrenze
    sim = await TAX.simulate_sale(c, "BTC", qty=None, price_eur="200000",
                                  sale_date="2026-03-01", fetcher=None, tax_rate="42")
    assert D(sim["taxableGain"]) == D(1300) and sim["exceededAfter"]
    assert D(sim["taxEstimate"]) == D(1300) * D("0.42"), sim
    # Transfer in einen separaten OKX-Topf (Saldo springt nicht mit): nicht buchen
    rows = [
        {"id": "1", "Time": "2026-11-16 16:00:03", "Trade Type": "Futures", "Action": "Funding fee expense",
         "Symbol": "X", "PnL": "-1", "Fee": "0", "Fee Unit": "USDC", "Balance Change": "0",
         "Balance": "0.4966", "Balance Unit": "USDC"},
        {"id": "2", "Time": "2026-11-16 23:17:14", "Trade Type": "Transfer", "Action": "",
         "Symbol": "", "PnL": "0", "Fee": "0", "Fee Unit": "USDC", "Balance Change": "18.7",
         "Balance": "18.7", "Balance Unit": "USDC"},
        {"id": "3", "Time": "2026-11-17 22:39:52", "Trade Type": "Transfer", "Action": "Transfer in",
         "Symbol": "", "PnL": "0", "Fee": "0", "Fee Unit": "USDC", "Balance Change": "26.8886",
         "Balance": "27.3852", "Balance Unit": "USDC"},
    ]
    assert IMP.foreign_pot_transfers(rows) == {"2"}

    # Funding-Auswertung, Bestand zum Stichtag, Herkunftsnachweis
    from datetime import date as _date
    c = fresh()
    _tx(c, "2026-01-05T10:00:00Z", "deposit", "funding", "neutral", [L.Entry("EUR", "1000")], "d1")
    _tx(c, "2026-01-06T10:00:00Z", "trade_spot", "trading", "par23",
        [L.Entry("EUR", "-1000"), L.Entry("USDC", "1100")], "s1")
    for k, day in enumerate(("2026-01-10", "2026-01-20", "2026-02-02")):
        L.add_transaction(c, L.Transaction(ts_utc=day + "T08:00:00Z", tx_type="funding_fee",
            account="trading", tax_bucket="par20", source="t", external_id=f"f{k}",
            note="BTC-USD-PERP Funding fee expense", entries=[L.Entry("USDC", "-10", "funding")]))
    fa = await TAX.funding_analysis(c, fetcher=None, today=_date(2026, 2, 5))
    assert D(fa["total"]["net"]) == D(-30) and fa["instruments"][0]["instrument"] == "BTC-USD-PERP", fa
    assert D(fa["total"]["net30"]) == D(-30) and D(fa["total"]["net7"]) == D(-10)
    assert len(fa["months"]) == 2
    h = await ST.holdings_at(c, "2026-01-31")
    usdc = next(a for a in h["assets"] if a["asset"] == "USDC")
    assert D(usdc["qty"]) == D(1080), usdc
    assert abs(D(usdc["cost"]) - D(1000) * D(1080) / D(1100)) < D("0.01"), usdc
    o = await ST.origin_report(c, until="2026-02-05")
    assert D(o["sumIn"]) == D(1000) and len(o["eurIn"]) == 1
    # Abstimmung: Wert = Einzahlung + realisiert + nicht realisiert
    assert abs(D(o["valueNow"]) - (D(o["sumIn"]) + D(o["resultTotal"]))) < D("0.000001")
    print("Neue Funktionen OK")


asyncio.run(main())
