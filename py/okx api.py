"""
OKX API - authentifizierte Anfragen
====================================

Im Unterschied zu market.py (oeffentliche Endpunkte, kein Login noetig)
braucht alles hier einen API-Key/Secret/Passphrase und eine Signatur pro
Anfrage. Das Signierverfahren folgt der offiziellen OKX-v5-Spezifikation:

    prehash   = timestamp + method + requestPath + body
    signature = Base64(HMAC-SHA256(secret, prehash))

Timestamp ist ISO-8601 mit Millisekunden und 'Z', z.B.
"2026-08-30T14:03:12.481Z" - muss der Serverzeit auf wenige Sekunden nahe
sein, sonst lehnt OKX die Anfrage ab (Fehlercode 50102).

WICHTIG - noch nicht verifiziert: Ob OKX CORS-Anfragen direkt aus dem
Browser fuer PRIVATE (signierte) Endpunkte erlaubt, ist unbekannt - das
konnte aus der Sandbox, in der dieses Modul entstanden ist, nie getestet
werden (kein Netzwerkzugriff auf okx.com von dort). test_connection() ist
extra als erster, risikoarmer Schritt gedacht: einmal auf dem echten
iPhone bestaetigen, dass das ueberhaupt geht, BEVOR groessere Funktionen
(Trading-/Funding-Historie) darauf aufgebaut werden.

Rechte des API-Keys: Diese Datei fragt ausschliesslich lesende Endpunkte
ab (Konfiguration, Kontostand, Positionen). Ein Key mit nur Lese-Rechten
reicht fuer alles hier vollstaendig aus - Trade- oder Withdraw-Rechte
werden nirgends gebraucht.
"""

import base64
import hashlib
import hmac
import json
import sys
from datetime import datetime, timezone
from decimal import Decimal

BASE = "https://www.okx.com"
TIMEOUT = 12
IN_PYODIDE = sys.platform == "emscripten"


class OkxApiError(Exception):
    pass


class OkxAuthError(OkxApiError):
    """Key/Secret/Passphrase falsch oder Key hat nicht die noetigen Rechte -
    im Unterschied zu einem Netzwerk-/CORS-Fehler ist das eindeutig ein
    Problem mit den hinterlegten Zugangsdaten."""
    pass


def D(v):
    return Decimal(str(v)) if v not in (None, "") else Decimal(0)


def _timestamp():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def _sign(secret, timestamp, method, request_path, body=""):
    prehash = f"{timestamp}{method}{request_path}{body}"
    mac = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()


async def _request(creds, method, path, params=None, body=None):
    """creds: dict mit apiKey/apiSecret/passphrase (aus app_state gelesen).
    Signiert und schickt eine private Anfrage, liefert das 'data'-Feld der
    Antwort zurueck."""
    if not creds or not creds.get("apiKey") or not creds.get("apiSecret") \
            or not creds.get("passphrase"):
        raise OkxAuthError(
            "Keine OKX-Zugangsdaten hinterlegt. Erst API-Key, Secret und "
            "Passphrase unter Import > OKX-Verbindung eingeben und speichern.")

    query = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            import urllib.parse
            query = "?" + urllib.parse.urlencode(clean)
    request_path = path + query
    body_str = json.dumps(body, separators=(",", ":")) if body else ""

    ts = _timestamp()
    sign = _sign(creds["apiSecret"], ts, method, request_path, body_str)
    headers = {
        "OK-ACCESS-KEY": creds["apiKey"],
        "OK-ACCESS-SIGN": sign,
        "OK-ACCESS-TIMESTAMP": ts,
        "OK-ACCESS-PASSPHRASE": creds["passphrase"],
        "Content-Type": "application/json",
    }
    url = BASE + request_path

    if IN_PYODIDE:
        payload = await _request_pyodide(url, method, headers, body_str)
    else:
        payload = _request_urllib(url, method, headers, body_str)

    code = str(payload.get("code"))
    if code == "50111" or code == "50113" or code == "50114":
        raise OkxAuthError(f"OKX lehnt die Zugangsdaten ab: {payload.get('msg')}")
    if code != "0":
        raise OkxApiError(f"OKX meldet: {payload.get('msg') or code}")
    return payload.get("data") or []


async def _request_pyodide(url, method, headers, body_str):
    import pyodide.http
    try:
        kwargs = {"method": method, "headers": headers}
        if body_str:
            kwargs["body"] = body_str
        resp = await pyodide.http.pyfetch(url, **kwargs)
    except Exception as exc:
        raise OkxApiError(
            f"Keine Verbindung zu OKX aus dem Browser heraus: {exc}. Möglich, "
            "dass OKX signierte Anfragen direkt aus dem Browser nicht "
            "zulässt (CORS) - dann bräuchte es einen Server-Umweg, und die "
            "direkte API-Anbindung wäre so nicht nutzbar.") from None
    if resp.status == 401:
        raise OkxAuthError("OKX lehnt die Zugangsdaten ab (HTTP 401).")
    if not resp.ok:
        raise OkxApiError(f"HTTP {resp.status} bei {url}")
    try:
        return await resp.json()
    except Exception as exc:
        raise OkxApiError(f"Antwort von OKX nicht lesbar: {exc}") from None


def _request_urllib(url, method, headers, body_str):
    import ssl
    import urllib.error
    import urllib.request
    data = body_str.encode() if body_str else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT,
                                    context=ssl.create_default_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode())
        except Exception:
            raise OkxApiError(f"HTTP {exc.code} bei {url}") from None
    except urllib.error.URLError as exc:
        raise OkxApiError(f"Keine Verbindung zu OKX: {exc.reason}") from None


# --- Endpunkte ---------------------------------------------------------

async def test_connection(creds):
    """Kleinstmoeglicher authentifizierter Aufruf, nur um zu pruefen: sind
    die Zugangsdaten gueltig, und laesst der Browser die Anfrage ueberhaupt
    durch (CORS)? Gibt bei Erfolg ein paar harmlose Kontoeckdaten zurueck."""
    data = await _request(creds, "GET", "/api/v5/account/config")
    if not data:
        raise OkxApiError("OKX hat eine leere Antwort geschickt.")
    cfg = data[0]
    return {
        "ok": True,
        "uid": cfg.get("uid"),
        "level": cfg.get("level"),
        "mode": cfg.get("acctLv"),
    }


async def balance(creds, ccy=None):
    """Kontostand pro Waehrung (verfuegbar + in Positionen gebunden)."""
    data = await _request(creds, "GET", "/api/v5/account/balance",
                          params={"ccy": ccy})
    if not data:
        return []
    out = []
    for d in data[0].get("details", []):
        out.append({
            "ccy": d.get("ccy"),
            "eq": str(D(d.get("eq"))),
            "availBal": str(D(d.get("availBal"))),
            "frozenBal": str(D(d.get("frozenBal"))),
            "eqUsd": str(D(d.get("eqUsd"))),
        })
    return out


async def positions(creds, inst_type=None):
    """Offene Positionen, direkt von OKX - im selben Feldnamen-Stil wie
    derive_positions() in okx_import.py, damit sich der Rest der App (z.B.
    market.snapshot()) nicht darum kuemmern muss, ob eine Position aus der
    CSV rekonstruiert oder live von der API kam."""
    data = await _request(creds, "GET", "/api/v5/account/positions",
                          params={"instType": inst_type})
    out = []
    for p in data:
        sz = D(p.get("pos"))
        if sz == 0:
            continue
        side = "long" if (p.get("posSide") == "long" or
                          (p.get("posSide") in (None, "net") and sz > 0)) else "short"
        ct_val = D(p.get("ctVal"))
        contracts = sz
        size = abs(contracts) * ct_val if ct_val else abs(sz)
        out.append({
            "instrument": p.get("instId"),
            "base": (p.get("instId") or "").split("-")[0],
            "side": side,
            "contracts": contracts,
            "size": str(size),
            "ct_val": str(ct_val),
            "avg_entry": str(D(p.get("avgPx"))),
            "initial_margin": str(D(p.get("margin"))),
            "margin_balance": str(D(p.get("margin")) + D(p.get("upl"))),
            "settle_ccy": p.get("settleCcy") or p.get("ccy") or "USDC",
            "as_of": None,
            "source": "okx-api-live",
            # direkt von OKX mitgeliefert, kein eigener Kurs-Abruf noetig
            "liveUpl": str(D(p.get("upl"))),
            "liveUplRatio": str(D(p.get("uplRatio"))),
            "liveMarkPx": str(D(p.get("markPx"))) if p.get("markPx") else None,
        })
    return out
