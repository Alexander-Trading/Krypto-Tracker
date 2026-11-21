"""Browser-Test fuer den Reiter Journal (Position History), ohne echtes Netz.

Aufruf (aus dem Projektordner):
    python3 -m http.server 8765 &
    python3 tests/e2e_journal.py POSITION_HISTORY.csv ausgabe.png [breite]

USDC/USDT-EUR wird mit 0.86 gemockt. Prueft: Import erkennt die Datei, es
entsteht keine Buchung, Journal zeigt Positionen, Zeile klappt auf, kein
seitliches Scrollen.
"""
import asyncio, json, sys
from playwright.async_api import async_playwright

CSV, OUT = sys.argv[1], sys.argv[2]
WIDTH = int(sys.argv[3]) if len(sys.argv) > 3 else 390


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": WIDTH, "height": 844},
                                  device_scale_factor=2, is_mobile=True, has_touch=True)
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.on("dialog", lambda d: asyncio.ensure_future(d.dismiss()))

        async def okx(route):
            u = route.request.url
            inst = u.split("instId=")[1].split("&")[0] if "instId=" in u else ""
            if "candles" in u and inst in ("USDC-EUR", "USDT-EUR"):
                body = {"code": "0", "data": [["0", "0", "0", "0", "0.86", "0"]]}
            else:
                body = {"code": "51001", "msg": "x", "data": []}
            await route.fulfill(status=200, content_type="application/json",
                                headers={"access-control-allow-origin": "*"},
                                body=json.dumps(body))

        await pg.route("**/www.okx.com/**", okx)
        for h in ("pypi.org", "files.pythonhosted.org", "cdn.jsdelivr.net",
                  "fonts.googleapis.com", "fonts.gstatic.com"):
            await pg.route(f"**/{h}/**", lambda r: r.abort())

        await pg.goto("http://localhost:8765/index.html")
        await pg.wait_for_selector("#menuBtn", timeout=120000)
        await pg.wait_for_timeout(6000)
        await pg.evaluate("go('import')")
        await pg.wait_for_selector("#drop", timeout=30000)
        await pg.set_input_files("#file", CSV)
        await pg.wait_for_selector("#impResult .kv", timeout=60000)
        await pg.wait_for_timeout(2500)
        print("Import:", (await pg.inner_text("#impResult"))[:300].replace("\n", " | "))
        if OUT:
            await pg.screenshot(path=OUT.replace(".png", "-import.png"), full_page=True)
        tx = await pg.evaluate("STATE.txCount")
        print("Buchungen nach Import:", tx)

        # Ueber das Menue oeffnen, wie auf dem iPhone
        await pg.click("#menuBtn")
        await pg.wait_for_timeout(400)
        if OUT:
            await pg.screenshot(path=OUT.replace(".png", "-menu.png"))
        await pg.click('.tab[data-tab="journal"]')
        await pg.wait_for_selector(".jrow", timeout=60000)
        await pg.wait_for_timeout(1500)
        await pg.click(".jrow")
        await pg.wait_for_selector(".jdetail", timeout=10000)
        await pg.wait_for_timeout(500)
        print("Seite:", await pg.inner_text("#pageName"))
        print("scrollWidth/innerWidth:",
              await pg.evaluate("[document.documentElement.scrollWidth, innerWidth]"))
        print("Fehler:", errs)
        await pg.screenshot(path=OUT, full_page=True)
        await b.close()

asyncio.run(main())
