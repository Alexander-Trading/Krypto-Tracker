"""
Marktdaten
==========

Holt Kurse und Kontraktdaten von den oeffentlichen OKX-Endpunkten. Die
brauchen keinen API-Key - das ist dieselbe Information, die jeder auf der
Webseite sieht. Es werden ausschliesslich Daten gelesen.

Alles hier ist so gebaut, dass ein Ausfall sichtbar wird. Ein Tracker, der
bei fehlgeschlagenem Abruf stillschweigend alte Zahlen anzeigt, ist
gefaehrlicher als einer, der ehrlich "keine Verbindung" meldet.
"""

import json
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

BASE = "https://www.okx.com"
TIMEOUT = 12

# Pyodide (Browser/WebAssembly) hat keine echten Sockets - urllib kann dort
# gar keine Verbindung aufbauen ("TLS not supported in this environment").
# Der Browser selbst kann aber sehr wohl Netzwerkanfragen stellen; Pyodide
# reicht das über pyfetch() durch (nutzt fetch() im Hintergrund). Deshalb
# ist _get() jetzt async und verzweigt je nach Umgebung - der Rest von
# market.py bleibt inhaltlich unveraendert, nur mit await davor.
IN_PYODIDE = sys.platform == "emscripten"

# Falls die Tier-Tabelle nicht erreichbar ist: Bandbreite statt Einzelwert.
# Bei kleinem Hebel macht die Wartungsmarge kaum einen Unterschied, bei
# grossem sehr wohl - deshalb zeigen wir die Spanne, statt zu raten.
MMR_FALLBACK = (Decimal("0.0035"), Decimal("0.0100"))
TAKER_FEE = Decimal("0.0005")


class MarketError(Exception):
    pass


CERT_HINT = (
    "Python fehlen die Stammzertifikate. Das ist ein bekannter Stolperstein "
    "der python.org-Installation auf dem Mac. Behebung: im Finder unter "
    "Programme den Ordner \"Python 3.x\" öffnen und dort einmal "
    "\"Install Certificates.command\" doppelklicken. Danach das Tool neu starten."
)


def _ssl_context():
    """certifi mitnehmen, falls vorhanden - sonst der Systemspeicher."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


async def _get(path, **params):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None})

    if IN_PYODIDE:
        payload = await _get_pyodide(url)
    else:
        payload = _get_urllib(url)

    if str(payload.get("code")) != "0":
        raise MarketError(f"OKX meldet: {payload.get('msg') or payload.get('code')}")
    return payload.get("data") or []


def _get_urllib(url):
    req = urllib.request.Request(url, headers={"User-Agent": "tracker/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise MarketError(f"HTTP {exc.code} bei {url}") from None
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "CERTIFICATE_VERIFY_FAILED" in reason or "SSL" in reason.upper():
            raise MarketError(CERT_HINT) from None
        raise MarketError(f"Keine Verbindung zu OKX: {reason}") from None
    except TimeoutError:
        raise MarketError("Zeitüberschreitung beim Abruf") from None


async def _get_pyodide(url):
    import pyodide.http
    try:
        resp = await pyodide.http.pyfetch(
            url, headers={"User-Agent": "tracker/0.1"})
    except Exception as exc:
        # Kommt hierher, wenn schon die Anfrage selbst scheitert - typischerweise
        # ein CORS-Block durch OKX, nicht durch uns.
        raise MarketError(
            f"Keine Verbindung zu OKX aus dem Browser heraus: {exc}. "
            "Möglich, dass OKX Anfragen direkt aus dem Browser nicht zulässt "
            "(CORS) - dann bräuchte es einen Server-Umweg.") from None
    if not resp.ok:
        raise MarketError(f"HTTP {resp.status} bei {url}")
    try:
        return await resp.json()
    except Exception as exc:
        raise MarketError(f"Antwort von OKX nicht lesbar: {exc}") from None


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


# --- Einzelabrufe ----------------------------------------------------------

async def instrument(inst_id, inst_type="FUTURES"):
    """Kontraktdaten: Kontraktgroesse, Verfallsdatum, Abwicklungswaehrung."""
    data = await _get("/api/v5/public/instruments", instType=inst_type, instId=inst_id)
    if not data:
        raise MarketError(f"Kontrakt {inst_id} nicht gefunden")
    d = data[0]
    return {
        "instId": d.get("instId"),
        "instFamily": d.get("instFamily") or d.get("uly"),
        "ctVal": D(d.get("ctVal")),
        "ctValCcy": d.get("ctValCcy"),
        "settleCcy": d.get("settleCcy"),
        "expTime": d.get("expTime") or None,
        "lever": d.get("lever"),
    }


async def mark_price(inst_id, inst_type="FUTURES"):
    """Der Mark-Preis ist die Referenz fuer Liquidationen - nicht der
    zuletzt gehandelte Kurs."""
    data = await _get("/api/v5/public/mark-price", instType=inst_type, instId=inst_id)
    if not data:
        raise MarketError(f"Kein Mark-Preis für {inst_id}")
    return D(data[0].get("markPx"))


async def last_price(inst_id):
    data = await _get("/api/v5/market/ticker", instId=inst_id)
    if not data:
        raise MarketError(f"Kein Kurs für {inst_id}")
    return D(data[0].get("last"))


async def maintenance_margin_rate(inst_family, notional, inst_type="FUTURES"):
    """Waehlt aus der gestaffelten Tier-Tabelle die Stufe, in die die
    Positionsgroesse faellt, und gibt deren Wartungsmargenquote zurueck."""
    data = await _get("/api/v5/public/position-tiers", instType=inst_type,
                tdMode="isolated", instFamily=inst_family)
    if not data:
        raise MarketError(f"Keine Tier-Tabelle für {inst_family}")

    tiers = sorted(data, key=lambda t: int(t.get("tier", 0)))
    for t in tiers:
        max_sz = D(t.get("maxSz"))
        if max_sz == 0 or notional <= max_sz:
            return D(t.get("mmr")), int(t.get("tier", 0))
    last = tiers[-1]
    return D(last.get("mmr")), int(last.get("tier", 0))


# --- Instrument auflösen ------------------------------------------------

INST_TYPES = ("FUTURES", "SWAP")


async def list_instruments(inst_type, inst_family=None):
    """Alle handelbaren Kontrakte einer Gattung."""
    return await _get("/api/v5/public/instruments", instType=inst_type,
                instFamily=inst_family)


def _score(cand_id, symbol):
    """Wie gut passt ein Kontraktname zu dem Namen aus der CSV?"""
    cand, sym = cand_id.upper(), symbol.upper()
    if cand == sym:
        return 1000

    parts = [p for p in sym.split("-") if p]
    base = parts[0] if parts else sym
    digits = [p for p in parts if p.isdigit()]

    score = 0
    if cand.startswith(base + "-"):
        score += 40
    # Die Ziffernfolge im Namen ist der Verfallstermin und damit das
    # trennschaerfste Merkmal.
    if digits and any(d in cand for d in digits):
        score += 50
    if "XPERP" in sym and "XPERP" in cand:
        score += 15
    for token in ("UM", "USD", "USDC"):
        if token in parts and token in cand.split("-"):
            score += 5
    return score


async def resolve_instrument(symbol):
    """Sucht den API-Bezeichner zum Namen aus der CSV.

    OKX verwendet in Exportdateien nicht zwingend dieselbe Schreibweise wie in
    der API. Statt zu raten wird die Kontraktliste durchsucht; bei zu geringer
    Sicherheit werden Kandidaten zurueckgegeben, damit von Hand gewaehlt
    werden kann.
    """
    errors, candidates = [], []
    for inst_type in INST_TYPES:
        try:
            for d in await list_instruments(inst_type):
                iid = d.get("instId") or ""
                sc = _score(iid, symbol)
                if sc > 0:
                    candidates.append({"instId": iid, "instType": inst_type,
                                       "score": sc, "expTime": d.get("expTime"),
                                       "settleCcy": d.get("settleCcy"),
                                       "ctVal": d.get("ctVal")})
        except MarketError as exc:
            errors.append(f"{inst_type}: {exc}")

    candidates.sort(key=lambda c: -c["score"])
    if candidates and candidates[0]["score"] >= 85:
        return candidates[0], candidates[:8]
    return None, candidates[:8]


async def search_instruments(query, limit=25):
    """Freitextsuche über alle Kontrakte, fuer die Auswahl von Hand."""
    q = (query or "").upper()
    out = []
    for inst_type in INST_TYPES:
        try:
            for d in await list_instruments(inst_type):
                iid = (d.get("instId") or "").upper()
                if q in iid:
                    out.append({"instId": d.get("instId"), "instType": inst_type,
                                "settleCcy": d.get("settleCcy"),
                                "ctVal": d.get("ctVal"), "expTime": d.get("expTime")})
        except MarketError:
            continue
    return sorted(out, key=lambda c: c["instId"])[:limit]


# --- Liquidation -----------------------------------------------------------

def liquidation_price(size, avg_entry, margin_balance, mmr,
                      taker=TAKER_FEE, side="long"):
    """Isolated Margin, lineare Kontrakte.

    Liquidiert wird, wenn die Margin unter Wartungsmarge plus Gebuehren
    faellt. Fuer eine Long-Position:

        Margin + (P - Einstieg) * Menge  =  P * Menge * (mmr + Gebuehr)

    nach P aufgeloest. Bei Short dreht sich das Vorzeichen.

    Das ist eine Naeherung. Massgeblich ist immer die Zahl, die OKX in der
    Positionsansicht anzeigt.
    """
    size = abs(D(size))
    if size == 0:
        return None
    factor = D(1) - D(mmr) - D(taker) if side == "long" \
        else D(1) + D(mmr) + D(taker)
    numerator = D(avg_entry) * size - D(margin_balance) if side == "long" \
        else D(avg_entry) * size + D(margin_balance)
    price = numerator / (size * factor)
    return price if price > 0 else Decimal(0)


# --- Historische EUR-Kurse ------------------------------------------------

async def eur_rate_on(asset, day):
    """Tagesschlusskurs asset/EUR von OKX. None, wenn es das Paar nicht gibt
    oder der Tag nicht abgedeckt ist."""
    if asset == "EUR":
        return Decimal(1)

    from datetime import datetime, timezone
    start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
    after = int((start.timestamp() + 86400) * 1000)

    for inst, invert in ((f"{asset}-EUR", False), (f"EUR-{asset}", True)):
        try:
            data = await _get("/api/v5/market/history-candles", instId=inst,
                              bar="1D", after=str(after), limit="1")
        except MarketError:
            continue
        if not data:
            continue
        close = D(data[0][4])
        if close == 0:
            continue
        return (Decimal(1) / close) if invert else close
    return None


# --- Gesamtbild ------------------------------------------------------------

async def snapshot(position, inst_id=None, inst_type=None):
    """Reichert eine importierte Position mit Live-Daten an. Sammelt Fehler,
    statt beim ersten Problem abzubrechen - eine halbe Auskunft ist besser
    als gar keine, solange klar ist, welche Haelfte fehlt."""
    csv_symbol = position["instrument"]
    out = {"instrument": csv_symbol, "errors": [], "source": "okx-public"}

    if not inst_id:
        try:
            match, cands = await resolve_instrument(csv_symbol)
            if match:
                inst_id, inst_type = match["instId"], match["instType"]
                out["resolvedTo"] = inst_id
            else:
                out["candidates"] = cands
                out["errors"].append(
                    f"Der Name {csv_symbol} aus der CSV ist bei OKX nicht als "
                    "Kontrakt bekannt. Wähle den passenden Kontrakt von Hand aus.")
                return out
        except MarketError as exc:
            out["errors"].append(str(exc))
            return out

    inst_type = inst_type or "FUTURES"
    out["instType"] = inst_type
    size = abs(D(position["size"]))
    avg = D(position["avg_entry"])
    margin = D(position["margin_balance"])
    side = position.get("side", "long")

    info = None
    try:
        info = await instrument(inst_id, inst_type)
        out["ctVal"] = str(info["ctVal"])
        out["settleCcy"] = info["settleCcy"]
        out["expTime"] = info["expTime"]
        out["instFamily"] = info["instFamily"]
        if info["ctVal"] and info["ctVal"] != D(position["ct_val"]):
            # Der beim Import geschaetzte Kontraktwert war falsch (Standardwert,
            # bis hierher konnte niemand den echten kennen). avg_entry ist davon
            # unabhaengig (kuerzt sich raus), aber size - und alles was davon
            # abhaengt (Notional, uPnL, Liquidation) - muss neu gerechnet werden,
            # sonst rechnet der Rest dieser Funktion mit einer falschen Zahl weiter.
            contracts = D(position.get("contracts", 0))
            corrected_size = abs(contracts * info["ctVal"])
            out["ctValCorrected"] = True
            out["size"] = str(corrected_size if side == "long" else -corrected_size)
            size = corrected_size
            out["errors"].append(
                f"Kontraktgröße war beim Import falsch geschätzt ({position['ct_val']}) - "
                f"jetzt mit dem echten Wert ({info['ctVal']}) korrigiert und gespeichert.")
    except MarketError as exc:
        out["errors"].append(f"Kontraktdaten: {exc}")

    mark = None
    try:
        mark = await mark_price(inst_id, inst_type)
        out["markPrice"] = str(mark)
    except MarketError as exc:
        out["errors"].append(f"Mark-Preis: {exc}")

    try:
        out["lastPrice"] = str(await last_price(inst_id))
    except MarketError as exc:
        out["errors"].append(f"Kurs: {exc}")

    # Bewertung
    if mark:
        notional = size * mark
        direction = D(1) if side == "long" else D(-1)
        upnl = (mark - avg) * size * direction
        out["notional"] = str(notional)
        out["unrealizedPnl"] = str(upnl)
        out["equity"] = str(margin + upnl)
        if margin:
            out["returnOnMargin"] = str(upnl / margin * 100)

    # Wartungsmarge und Liquidation
    mmr, tier = None, None
    if info and info.get("instFamily") and mark:
        try:
            mmr, tier = await maintenance_margin_rate(
                info["instFamily"], size * mark, inst_type)
            out["mmr"] = str(mmr)
            out["mmrTier"] = tier
        except MarketError as exc:
            out["errors"].append(f"Wartungsmarge: {exc}")

    if mmr is not None:
        liq = liquidation_price(size, avg, margin, mmr, side=side)
        out["liquidationPrice"] = str(liq)
        out["liquidationBasis"] = (
            f"Berechnet mit Tier {tier}, Wartungsmarge {mmr * 100:.2f} %.")
    else:
        lo = liquidation_price(size, avg, margin, MMR_FALLBACK[1], side=side)
        hi = liquidation_price(size, avg, margin, MMR_FALLBACK[0], side=side)
        if lo is not None:
            out["liquidationRange"] = [str(min(lo, hi)), str(max(lo, hi))]
            out["liquidationBasis"] = (
                "Als Spanne geschätzt, weil die Tier-Tabelle nicht "
                "abrufbar war.")

    # Gleiche Ursache, drei Abrufe: einmal melden reicht.
    grouped = {}
    for err in out["errors"]:
        label, _, msg = err.partition(": ")
        grouped.setdefault(msg or label, []).append(label if msg else "")
    out["errors"] = [
        (", ".join(x for x in labels if x) + ": " + msg) if any(labels) else msg
        for msg, labels in grouped.items()]

    liq_val = out.get("liquidationPrice") or (
        out.get("liquidationRange") or [None])[0]
    if mark and liq_val:
        liq_d = D(liq_val)
        gap = (mark - liq_d) if side == "long" else (liq_d - mark)
        out["distanceAbs"] = str(gap)
        out["distancePct"] = str(gap / mark * 100)

    return out
