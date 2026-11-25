"""
Nachweise: Bestand zum Stichtag und Herkunftsnachweis
=====================================================

holdings_at()   Bestand je Kryptowert und Verwahrort (OKX / Trezor) zum Ende
                eines Tages, bewertet zum EUR-Tageskurs, mit den FIFO-
                Anschaffungskosten der dann noch offenen Lots. Typisch: 31.12.
                fuer die eigene Dokumentation.

origin_report() Herkunftsnachweis (Mittelherkunft), wie ihn Banken z. B. vor
                einer Finanzierung oder bei groesseren Auszahlungen verlangen:
                Woher kam das Geld (EUR-Einzahlungen, mitgebrachte Coins),
                was ist daraus geworden (realisierte Ergebnisse je Jahr), was
                ist heute da, und geht die Rechnung auf.

Beides liefert nur Daten; das PDF baut pdf_report.py.
"""

from datetime import datetime, timezone
from decimal import Decimal

import ledger as L
import tax as TAX

D0 = Decimal(0)
OKX_ACCOUNTS = ("funding", "trading", "earn")
PLACE = {"funding": "OKX", "trading": "OKX", "earn": "OKX", "wallet": "Trezor"}


def _balances_until(conn, until):
    """{(ort, asset): menge} fuer alle Buchungen bis Tagesende (UTC)."""
    out = {}
    for r in conn.execute(
            """SELECT t.account, e.asset, e.amount FROM entries e
               JOIN transactions t ON t.id = e.transaction_id
               WHERE t.ts_utc <= ?""", (until + "T23:59:59Z",)):
        key = (PLACE.get(r["account"], r["account"]), r["asset"])
        out[key] = out.get(key, D0) + L.D(r["amount"])
    return {k: v for k, v in out.items() if abs(v) > Decimal("0.00000001")}


async def holdings_at(conn, until, fetcher=None):
    bal = _balances_until(conn, until)
    assets = sorted({a for _, a in bal})
    # Kurse fuer den Stichtag (Bewertung) UND fuer alle Buchungstage bis dahin
    # (Anschaffungskosten der offenen FIFO-Lots) - wie in build_report.
    pairs = [(a, until) for a in assets if a != TAX.BASE] + [
        (r[0], r[1]) for r in conn.execute(
            """SELECT DISTINCT e.asset, substr(t.ts_utc, 1, 10) FROM entries e
               JOIN transactions t ON t.id = e.transaction_id
               WHERE e.asset != 'EUR' AND t.ts_utc <= ?""", (until + "T23:59:59Z",))]
    table, missing, _ = await TAX.build_rate_lookup(conn, set(), set(),
                                                    fetcher=fetcher, pairs=pairs)
    missing = [m for m in missing if m.endswith(until[8:10] + "." + until[5:7] + "." + until[:4])
               or until in m]
    _, open_lots, _, _ = TAX.build_lots(conn, table, until=until)
    cost = {}
    for lot in open_lots:
        if lot["cost_eur"] is not None:
            cost[lot["asset"]] = cost.get(lot["asset"], D0) + lot["cost_eur"]

    rows, total_value, total_cost = [], D0, D0
    for (place, asset), qty in sorted(bal.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        value = TAX.to_eur(table, asset, until, qty)
        rows.append({"place": place, "asset": asset, "qty": qty,
                     "rate": (value / qty) if (value is not None and qty) else None,
                     "value": value})
        if value is not None:
            total_value += value
    per_asset = {}
    for r in rows:
        a = per_asset.setdefault(r["asset"], {"asset": r["asset"], "qty": D0, "value": D0,
                                              "cost": cost.get(r["asset"]),
                                              "priced": True})
        a["qty"] += r["qty"]
        if r["value"] is None:
            a["priced"] = False
        else:
            a["value"] += r["value"]
    for a in per_asset.values():
        if a["cost"] is not None and a["asset"] != TAX.BASE:
            total_cost += a["cost"]
    lots_at = [l for l in open_lots]
    return TAX.dec2str({
        "date": until, "rows": rows, "assets": list(per_asset.values()),
        "totalValue": total_value, "totalCostCrypto": total_cost,
        "openLots": lots_at, "missingRates": missing,
        "generatedAt": L.now_utc(),
    })


async def origin_report(conn, fetcher=None, until=None):
    until = until or datetime.now(timezone.utc).date().isoformat()

    def flows(kind_sql, args=()):
        return [dict(r) for r in conn.execute(
            f"""SELECT t.id, t.ts_utc, t.tx_type, t.account, t.source, t.note,
                       e.asset, e.amount, e.eur_value
                FROM transactions t JOIN entries e ON e.transaction_id = t.id
                WHERE {kind_sql} AND t.ts_utc <= ?
                ORDER BY t.ts_utc""", (*args, until + "T23:59:59Z"))]

    eur_in = flows("t.tx_type = 'deposit' AND e.asset = 'EUR' AND CAST(e.amount AS REAL) > 0 "
                   "AND t.source != 'wallet_csv'")
    eur_out = flows("t.tx_type = 'withdrawal' AND e.asset = 'EUR' AND CAST(e.amount AS REAL) < 0")
    # Nur echte Ein-/Auszahlungen von Coins bei OKX. Die Zeilen der Trezor-
    # Bestands-CSV sind intern ebenfalls "deposit" (Konto wallet) - die stehen
    # schon als "Trezor-Bestand (Kauf)" und duerfen hier nicht nochmal auftauchen.
    okx = "t.account IN ('funding', 'trading', 'earn') AND t.source != 'wallet_csv'"
    crypto_in = flows(f"t.tx_type = 'deposit' AND {okx} AND e.asset != 'EUR' "
                      "AND CAST(e.amount AS REAL) > 0")
    crypto_out = flows(f"t.tx_type = 'withdrawal' AND {okx} AND e.asset != 'EUR' "
                       "AND CAST(e.amount AS REAL) < 0")
    wallet_lots = flows("t.source = 'wallet_csv' AND CAST(e.amount AS REAL) > 0")

    rep = await TAX.build_report(conn, fetcher=fetcher)
    years = []
    for y in rep["years"]:
        p23 = y["par23"]
        all23 = sum((L.D(d["gain_eur"]) for d in p23.get("disposals", [])
                     if d.get("gain_eur") not in (None, "")), D0)
        free23 = sum((L.D(d["gain_eur"]) for d in p23.get("disposalsTaxFree", [])
                      if d.get("gain_eur") not in (None, "")), D0) \
            if "disposalsTaxFree" in p23 else None
        years.append({
            "year": y["year"],
            "par23": L.D(p23["gain"]),
            "par23Free": free23,
            "par20": L.D(y["par20"]["result"]),
            "par22": L.D(y["par22"]["result"]),
        })

    hold = await holdings_at(conn, until, fetcher=fetcher)
    sum_in = sum((L.D(r["amount"]) for r in eur_in), D0)
    sum_out = sum((-L.D(r["amount"]) for r in eur_out), D0)
    sum_wallet = sum((L.D(r["eur_value"]) for r in wallet_lots
                      if r["eur_value"] not in (None, "")), D0)

    # Offene Futures: Die Marge steckt im USDC-Bestand, der nicht realisierte
    # Gewinn/Verlust nicht. Schaetzung zum Stichtag aus Positionsgroesse,
    # Einstiegspreis und Tageskurs (nur fuer heute sinnvoll - die
    # gespeicherten Positionen sind der aktuelle Stand).
    upnl_rows, upnl_eur = [], D0
    try:
        import json
        row = conn.execute("SELECT value FROM app_state WHERE key = 'positions'").fetchone()
        positions = json.loads(row["value"]) if row else []
    except Exception:
        positions = []
    if positions and until >= datetime.now(timezone.utc).date().isoformat():
        bases = {(p.get("instrument") or "").split("-")[0] for p in positions}
        tbl, _, _ = await TAX.build_rate_lookup(
            conn, set(), set(), fetcher=fetcher,
            pairs=[(b, until) for b in bases if b] + [("USDC", until)])
        usdc = TAX.to_eur(tbl, "USDC", until, Decimal(1))
        for p in positions:
            base = (p.get("instrument") or "").split("-")[0]
            px = TAX.to_eur(tbl, base, until, Decimal(1))
            if not (px and usdc and p.get("size") and p.get("avg_entry")):
                continue
            mark_usd = px / usdc
            upnl = L.D(p["size"]) * (mark_usd - L.D(p["avg_entry"]))
            upnl_rows.append({"instrument": p.get("instrument"), "size": L.D(p["size"]),
                              "entry": L.D(p["avg_entry"]), "mark": mark_usd,
                              "upnlUsdc": upnl, "upnlEur": upnl * usdc,
                              "estimate": bool(p.get("partial"))})
            upnl_eur += upnl * usdc

    # Ergebnis gesamt = was aus dem eingesetzten Geld geworden ist:
    # heutiger Wert + bereits ausgezahlt - eingezahlt - mitgebrachte Coins
    value_now = L.D(hold["totalValue"]) + upnl_eur
    result_total = value_now + sum_out - sum_in - sum_wallet
    realized = sum((y["par23"] + (y["par23Free"] or D0) + y["par20"] + y["par22"]
                    for y in years), D0)
    return TAX.dec2str({
        "until": until,
        "eurIn": eur_in, "eurOut": eur_out,
        "cryptoIn": crypto_in, "cryptoOut": crypto_out,
        "walletLots": wallet_lots,
        "sumIn": sum_in, "sumOut": sum_out, "sumWallet": sum_wallet,
        "years": years, "realized": realized,
        "holdings": hold, "valueNow": value_now,
        "openPositions": upnl_rows, "openPositionsEur": upnl_eur,
        "resultTotal": result_total,
        "unrealizedAndOther": result_total - realized,
        "firstDate": (eur_in[0]["ts_utc"][:10] if eur_in else None),
        "generatedAt": L.now_utc(),
    })
