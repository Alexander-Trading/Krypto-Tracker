"""Browser-Test im iPhone-Format (Playwright, Chromium), ohne echtes Netz.

Aufruf (aus dem Projektordner):
    python3 -m http.server 8765 &
    python3 tests/e2e_playwright.py BESTAND.csv ausgabe.png [breite]

- OKX-Kurse werden gemockt (BTC 85000, ETH 3000, ADA 0.40, XRP 2.00),
  PyPI/jsDelivr/Fonts werden blockiert (PDF-Export ist im Test nicht verfuegbar).
- Ladet die Bestands-CSV ueber den Import, oeffnet Uebersicht, prueft auf
  seitliches Scrollen und speichert einen Screenshot.
Navigation per go('tab'), weil die Reiter im Menue-Overlay liegen.
"""
import asyncio, json, sys
from playwright.async_api import async_playwright

CSV = sys.argv[1]
OUT = sys.argv[2]
WIDTH = int(sys.argv[3]) if len(sys.argv) > 3 else 390
PR = {"BTC": "85000", "ETH": "3000", "ADA": "0.40", "XRP": "2.00"}


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch()
        ctx = await b.new_context(viewport={"width": WIDTH, "height": 844},
                                  device_scale_factor=2, is_mobile=True, has_touch=True)
        pg = await ctx.new_page()
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        async def okx(route):
            u = route.request.url
            inst = u.split("instId=")[1].split("&")[0] if "instId=" in u else ""
            a = inst.split("-")[0]
            if "history-candles" in u and a in PR and inst.endswith("-EUR"):
                body = {"code": "0", "data": [["0", "0", "0", "0", PR[a], "0"]]}
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
        await pg.wait_for_timeout(8000)
        await pg.evaluate("go('import')")
        await pg.wait_for_selector("#dropLots", timeout=30000)
        await pg.set_input_files("#fileLots", CSV)
        await pg.wait_for_selector("#btnSaveLots", timeout=60000)
        await pg.wait_for_timeout(3000)
        await pg.evaluate("go('dash')")
        await pg.wait_for_timeout(6000)
        print("scrollWidth/innerWidth:",
              await pg.evaluate("[document.documentElement.scrollWidth, innerWidth]"))
        print("Fehler:", errs)
        await pg.screenshot(path=OUT, full_page=True)
        await b.close()

asyncio.run(main())
