"""
Datenpruefung, Saldo-Abgleich und Zuordnung von Ein-/Auszahlungen
=================================================================

Ein Steuerreport auf lueckenhaften Daten sieht plausibel aus und ist trotzdem
falsch. Dieses Modul sucht deshalb aktiv nach Luecken:

  * data_checks()     automatische Pruefungen (negative Bestaende, moegliche
                      Doppelbuchungen, Umbuchungen ohne Gegenstueck, noch nicht
                      zugeordnete Ein-/Auszahlungen)
  * balance_check()   Ledger-Bestand gegen die tatsaechlichen Bestaende bei OKX
                      und auf der Trezor, die der Nutzer eintraegt
  * flows()/set_flow_mode()
                      Krypto-Ein- und Auszahlungen bei OKX einordnen:
                      Umbuchung mit der eigenen Trezor (Kaufdaten wandern mit),
                      schon in der Bestands-CSV enthalten, oder Zahlung an Dritte.
"""

import json
from datetime import datetime, timedelta
from decimal import Decimal

import ledger as L

OKX_ACCOUNTS = ("funding", "trading", "earn")
TOL = Decimal("0.00000001")
MIRROR_SOURCE = "auto_transfer"


def _state(conn, key, default):
    row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except (ValueError, TypeError):
        return default


def _set_state(conn, key, value):
    conn.execute(
        "INSERT OR REPLACE INTO app_state (key, value, updated_at) VALUES (?, ?, ?)",
        (key, json.dumps(value, ensure_ascii=False), L.now_utc()))
    conn.commit()


def _entries(conn, tx_id):
    return [dict(r) for r in conn.execute(
        "SELECT asset, amount, kind, eur_value FROM entries WHERE transaction_id = ? "
        "ORDER BY id", (tx_id,))]


# --- Ein-/Auszahlungen zuordnen ----------------------------------------------
#
# Modi je Vorgang (gespeichert in app_state 'flow_modes', {tx_id: {...}}):
#   Auszahlung von OKX:
#     "csv"    (Standard) eigene Trezor, die Coins stehen mit Kaufdatum in der
#              Bestands-CSV -> die OKX-Lots verlassen die Rechnung
#     "carry"  Umbuchung auf die Trezor, Kaufdaten wandern mit -> die Lots
#              bleiben erhalten, auf der Trezor entsteht der Bestand automatisch
#     "third"  Zahlung/Verkauf an Dritte -> Veraeusserung nach § 23
#   Einzahlung zu OKX:
#     "external" (Standard) Zugang von aussen, Einstandswert = Tageskurs
#     "carry"  kommt von der eigenen Trezor -> die Coins (mit Kaufdaten aus der
#              Bestands-CSV) wechseln nur das Konto

WITHDRAWAL_MODES = {"csv", "carry", "third"}
DEPOSIT_MODES = {"external", "carry"}


def flows(conn):
    modes = _state(conn, "flow_modes", {})
    ids = {int(k) for k in modes}
    rows = [t for t in conn.execute(
        """SELECT id, ts_utc, tx_type, tax_bucket, note FROM transactions
           WHERE account = 'funding' AND source != ?
             AND tx_type IN ('deposit', 'withdrawal', 'transfer')
           ORDER BY ts_utc DESC""", (MIRROR_SOURCE,))
        if t["tx_type"] != "transfer" or t["id"] in ids]
    out = []
    for t in rows:
        ents = [e for e in _entries(conn, t["id"])
                if e["asset"] != "EUR" and L.D(e["amount"]) != 0]
        if not ents:
            continue
        m = modes.get(str(t["id"]))
        orig = (m or {}).get("orig") or t["tx_type"]
        if orig not in ("deposit", "withdrawal"):
            continue
        mode = (m or {}).get("mode") or ("csv" if orig == "withdrawal" else "external")
        out.append({
            "id": t["id"], "ts": t["ts_utc"], "direction": orig,
            "mode": mode, "note": t["note"],
            "entries": [{"asset": e["asset"], "amount": e["amount"]} for e in ents],
        })
    return out


def set_flow_mode(conn, tx_id, mode):
    t = conn.execute("SELECT * FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not t:
        raise ValueError(f"Vorgang {tx_id} existiert nicht.")
    modes = _state(conn, "flow_modes", {})
    rec = modes.get(str(tx_id)) or {}
    orig = rec.get("orig") or t["tx_type"]
    if orig not in ("deposit", "withdrawal"):
        raise ValueError("Nur Ein- und Auszahlungen lassen sich so zuordnen.")
    allowed = WITHDRAWAL_MODES if orig == "withdrawal" else DEPOSIT_MODES
    if mode not in allowed:
        raise ValueError(f"Unbekannte Zuordnung: {mode}")

    # Zuerst in den Ausgangszustand zuruecksetzen ...
    conn.execute("DELETE FROM transactions WHERE source = ? AND external_id = ?",
                 (MIRROR_SOURCE, f"mirror-{tx_id}"))
    conn.execute("UPDATE transactions SET tx_type = ?, tax_bucket = 'neutral' WHERE id = ?",
                 (orig, tx_id))

    # ... dann den gewaehlten Modus anwenden.
    if mode == "carry":
        # Original wird zur Umbuchung; Gegenbuchung auf dem Wallet-Konto. Beide
        # Seiten sind steuerlich neutral, die Lots behalten ihr Kaufdatum.
        conn.execute("UPDATE transactions SET tx_type = 'transfer' WHERE id = ?", (tx_id,))
        mirror = [L.Entry(e["asset"], -L.D(e["amount"]))
                  for e in _entries(conn, tx_id)
                  if e["asset"] != "EUR" and L.D(e["amount"]) != 0
                  and e["kind"] == "principal"]
        if mirror:
            L.add_transaction(conn, L.Transaction(
                ts_utc=t["ts_utc"], tx_type="transfer", account="wallet",
                tax_bucket="neutral", source=MIRROR_SOURCE,
                external_id=f"mirror-{tx_id}",
                note=("Von OKX auf Trezor" if orig == "withdrawal"
                      else "Von Trezor zu OKX") + " (automatische Gegenbuchung)",
                entries=mirror))
    elif mode == "third":
        conn.execute("UPDATE transactions SET tax_bucket = 'par23' WHERE id = ?", (tx_id,))

    default = "csv" if orig == "withdrawal" else "external"
    if mode == default:
        modes.pop(str(tx_id), None)
    else:
        modes[str(tx_id)] = {"mode": mode, "orig": orig}
    _set_state(conn, "flow_modes", modes)
    conn.commit()


# --- Saldo-Abgleich ----------------------------------------------------------

def get_actual_balances(conn):
    return _state(conn, "actual_balances", {"okx": {}, "wallet": {}, "asOf": None})


def set_actual_balances(conn, okx=None, wallet=None):
    cur = get_actual_balances(conn)
    def clean(d):
        out = {}
        for k, v in (d or {}).items():
            k = (k or "").strip().upper()
            if not k:
                continue
            s = str(v).strip()
            if s == "":
                continue
            out[k] = L.dstr(L.D(s))
        return out
    if okx is not None:
        cur["okx"] = clean(okx)
    if wallet is not None:
        cur["wallet"] = clean(wallet)
    cur["asOf"] = L.now_utc()
    _set_state(conn, "actual_balances", cur)
    return cur


def _ledger_balances(conn, accounts):
    out = {}
    q = ",".join("?" for _ in accounts)
    for r in conn.execute(
            f"""SELECT e.asset, e.amount FROM entries e JOIN transactions t
                ON t.id = e.transaction_id WHERE t.account IN ({q})""", accounts):
        out[r["asset"]] = out.get(r["asset"], Decimal(0)) + L.D(r["amount"])
    return out


def balance_check(conn):
    actual = get_actual_balances(conn)
    led_okx = _ledger_balances(conn, OKX_ACCOUNTS)
    led_wal = _ledger_balances(conn, ("wallet",))
    assets = sorted({a for a, v in led_okx.items() if abs(v) > TOL}
                    | {a for a, v in led_wal.items() if abs(v) > TOL}
                    | set(actual.get("okx", {})) | set(actual.get("wallet", {})))
    rows = []
    for a in assets:
        row = {"asset": a}
        for key, led in (("okx", led_okx), ("wallet", led_wal)):
            mine = led.get(a, Decimal(0))
            raw = actual.get(key, {}).get(a)
            theirs = L.D(raw) if raw is not None else None
            diff = (mine - theirs) if theirs is not None else None
            # Toleranz: 0,01 % des Bestands, mindestens 1e-8 (Rundung bei OKX)
            tol = max(TOL, abs(theirs or mine) * Decimal("0.0001"))
            row[key] = {
                "ledger": mine, "actual": theirs, "diff": diff,
                "ok": None if diff is None else abs(diff) <= tol,
            }
        rows.append(row)
    checked = [r[k]["ok"] for r in rows for k in ("okx", "wallet") if r[k]["ok"] is not None]
    return {"rows": rows, "asOf": actual.get("asOf"),
            "checked": len(checked), "mismatches": sum(1 for x in checked if not x)}


# --- Automatische Pruefungen -------------------------------------------------

def _tx_detail(conn, tx_id, **extra):
    """Eine Buchung so aufbereitet, dass man sie im Pruefen-Tab wiedererkennt."""
    t = conn.execute(
        "SELECT id, ts_utc, tx_type, account, tax_bucket, source, external_id, note "
        "FROM transactions WHERE id = ?", (tx_id,)).fetchone()
    if not t:
        return None
    d = dict(t)
    d["entries"] = [{"asset": e["asset"], "amount": e["amount"], "kind": e["kind"]}
                    for e in _entries(conn, tx_id)]
    d.update(extra)
    return d


def ignore_duplicate(conn, ids):
    """Vermeintliche Doppelbuchung als korrekt markieren (z. B. zwei
    Teilausfuehrungen derselben Order in derselben Sekunde)."""
    ign = _state(conn, "ignored_duplicates", [])
    key = ",".join(str(i) for i in sorted(int(x) for x in ids))
    if key not in ign:
        ign.append(key)
        _set_state(conn, "ignored_duplicates", ign)

def data_checks(conn):
    issues = []

    # 1) Negativer Bestand im Zeitverlauf -> aeltere Buchungen fehlen.
    running, first_neg = {}, {}
    for r in conn.execute(
            """SELECT t.id, t.ts_utc, e.asset, e.amount FROM entries e
               JOIN transactions t ON t.id = e.transaction_id
               ORDER BY t.ts_utc, t.id, e.id"""):
        a = r["asset"]
        running[a] = running.get(a, Decimal(0)) + L.D(r["amount"])
        if running[a] < -TOL and a not in first_neg:
            first_neg[a] = (r["ts_utc"], running[a], r["id"])
    for a, (ts, v, tid) in sorted(first_neg.items()):
        prev = [r["id"] for r in conn.execute(
            """SELECT DISTINCT t.id FROM transactions t JOIN entries e
               ON e.transaction_id = t.id WHERE e.asset = ? AND
               (t.ts_utc < ? OR (t.ts_utc = ? AND t.id < ?))
               ORDER BY t.ts_utc DESC, t.id DESC LIMIT 3""", (a, ts, ts, tid))]
        items = [_tx_detail(conn, tid, role="Ab hier negativ")] + \
                [_tx_detail(conn, i, role="davor") for i in prev]
        issues.append({
            "items": [x for x in items if x],
            "level": "warn", "kind": "negative",
            "title": f"{a}: Bestand wird am {ts[:10]} negativ ({L.dstr(v)})",
            "detail": "Es wird mehr abgegeben, als vorher zugegangen ist. Meist fehlen "
                      "ältere Exporte (z. B. Funding History mit der Einzahlung) oder "
                      "Trezor-Käufe in der Bestands-CSV. Unten die Buchung, ab der der "
                      "Bestand negativ wird, und die drei davor."
                      + (" Bei EUR heißt das meist: die EUR-Einzahlung fehlt – für die "
                         "Steuer unkritisch, für die Portfolio-Rendite nicht."
                         if a == "EUR" else " Die Steuerrechnung ist dann unvollständig.")})

    # 2) Moegliche Doppelbuchungen: gleiche Sekunde, gleiche Buchungszeilen,
    #    aber unterschiedliche Vorgaenge.
    #    Nicht gemeldet: Gruppen, die nur aus OKX-Importzeilen mit jeweils
    #    eigener OKX-ID bestehen. Der Import uebernimmt jede OKX-Zeile genau
    #    einmal (Schluessel = OKX-ID); zwei solche Zeilen sind echte, getrennte
    #    Vorgaenge (typisch: Teilausfuehrungen einer Order mit gleicher Gebuehr).
    sig, meta = {}, {}
    for t in conn.execute(
            "SELECT id, ts_utc, tx_type, source, external_id FROM transactions "
            "WHERE source != ?", (MIRROR_SOURCE,)):
        ents = tuple(sorted((e["asset"], L.dstr(L.D(e["amount"])), e["kind"])
                            for e in _entries(conn, t["id"])))
        sig.setdefault((t["ts_utc"], t["tx_type"], ents), []).append(t["id"])
        meta[t["id"]] = (t["source"], t["external_id"])

    def okx_distinct(ids):
        srcs = [meta[i][0] for i in ids]
        exts = [meta[i][1] for i in ids]
        return (all(s not in ("manual", "wallet_csv") for s in srcs)
                and all(exts) and len(set(exts)) == len(exts))

    ignored = set(_state(conn, "ignored_duplicates", []))
    dups = [ids for ids in sig.values() if len(ids) > 1 and not okx_distinct(ids)
            and ",".join(str(i) for i in sorted(ids)) not in ignored]
    if dups:
        items = []
        for g, ids in enumerate(dups[:25]):
            for i in ids:
                d = _tx_detail(conn, i, group=g, groupIds=sorted(ids))
                if d:
                    items.append(d)
        issues.append({
            "level": "warn", "kind": "duplicate",
            "title": f"{len(dups)} mögliche Doppelbuchung(en)",
            "detail": "Gleicher Zeitpunkt, gleiche Art, gleiche Beträge – aber getrennt "
                      "gebucht, mindestens eine davon manuell oder aus der Bestands-CSV. "
                      "Typisch: dieselbe Buchung manuell und per Import erfasst – dann die "
                      "manuelle löschen. Ist es doch korrekt, „Ist korrekt“ tippen."
                      + (f" Angezeigt: die ersten 25 von {len(dups)}." if len(dups) > 25 else ""),
            "items": items})

    # 3) Umbuchungen zwischen Funding und Trading ohne Gegenstueck.
    tr = []
    for t in conn.execute(
            """SELECT t.id, t.ts_utc, t.account, e.asset, e.amount FROM transactions t
               JOIN entries e ON e.transaction_id = t.id
               WHERE t.tx_type = 'transfer' AND t.source != ?
                 AND t.account IN ('funding','trading','earn')""", (MIRROR_SOURCE,)):
        tr.append(dict(t))
    used, open_ = set(), []
    for i, a in enumerate(tr):
        if i in used:
            continue
        ta = datetime.fromisoformat(a["ts_utc"].replace("Z", "+00:00"))
        match = None
        for j, b in enumerate(tr):
            if j == i or j in used or b["account"] == a["account"] or b["asset"] != a["asset"]:
                continue
            if abs(L.D(a["amount"]) + L.D(b["amount"])) > TOL:
                continue
            tb = datetime.fromisoformat(b["ts_utc"].replace("Z", "+00:00"))
            if abs((ta - tb).total_seconds()) <= 300:
                match = j
                break
        if match is None:
            open_.append(a)
        else:
            used.update({i, match})
    if open_:
        # Zu jeder offenen Umbuchung die Kandidaten aus der Umgebung zeigen:
        # gleicher Coin, anderes OKX-Konto, +-24 h, beliebige Art. So sieht
        # man, ob die Gegenseite z. B. unter einer unbekannten OKX-Bezeichnung
        # als "adjustment" gelandet ist oder einen anderen Betrag hat.
        items = []
        for o in open_[:20]:
            d = _tx_detail(conn, o["id"], role="ohne Gegenstück")
            if d:
                items.append(d)
            t0 = datetime.fromisoformat(o["ts_utc"].replace("Z", "+00:00"))
            for c in conn.execute(
                    """SELECT DISTINCT t.id, t.ts_utc FROM transactions t
                       JOIN entries e ON e.transaction_id = t.id
                       WHERE e.asset = ? AND t.id != ? AND t.account != ?
                         AND t.account IN ('funding','trading','earn')
                         AND t.ts_utc BETWEEN ? AND ?
                       ORDER BY t.ts_utc""",
                    (o["asset"], o["id"], o["account"],
                     (t0 - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S"),
                     (t0 + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))):
                cd = _tx_detail(conn, c["id"], role="in der Nähe (±24 h)")
                if cd:
                    items.append(cd)
        issues.append({
            "items": items,
            "level": "info", "kind": "transfer",
            "title": f"{len(open_)} Umbuchung(en) ohne Gegenstück",
            "detail": "Bei OKX erscheint eine Umbuchung zwischen Funding- und Trading-"
                      "Konto in beiden Exporten. Fehlt eine Seite, ist vermutlich eine "
                      "der beiden Dateien (Funding oder Trading History) für diesen "
                      "Zeitraum nicht importiert. Steuerlich unkritisch, aber die "
                      "Kontostände je Konto stimmen dann nicht. Unter jeder Umbuchung "
                      "stehen die Buchungen im selben Coin auf den anderen OKX-Konten "
                      "(±24 h) – daran sieht man, ob die Gegenseite anders gebucht wurde."
                      + (f" Angezeigt: die ersten 50 von {len(open_)}." if len(open_) > 50 else "")})

    # 4) Krypto-Ein-/Auszahlungen, die noch auf Standard stehen.
    fl = flows(conn)
    pending_w = [f for f in fl if f["direction"] == "withdrawal" and f["mode"] == "csv"]
    pending_d = [f for f in fl if f["direction"] == "deposit" and f["mode"] == "external"]
    if pending_w or pending_d:
        issues.append({
            "items": [x for x in (_tx_detail(conn, f["id"]) for f in (pending_w + pending_d)[:50]) if x],
            "level": "info", "kind": "flows",
            "title": f"{len(pending_w) + len(pending_d)} Krypto-Ein-/Auszahlung(en) prüfen",
            "detail": "Unten unter „Ein- und Auszahlungen“ festlegen, ob es Umbuchungen "
                      "mit deiner Trezor sind. Standard: Auszahlung = Coins stehen in der "
                      "Bestands-CSV; Einzahlung = Zugang von außen zum Tageskurs."})

    unknown = L.unknown_count(conn)
    if unknown:
        uids = [r["id"] for r in conn.execute(
            "SELECT id FROM transactions WHERE tax_bucket = 'unknown' "
            "ORDER BY ts_utc LIMIT 50")]
        issues.append({
            "items": [x for x in (_tx_detail(conn, i) for i in uids) if x],
            "level": "warn", "kind": "unknown",
            "title": f"{unknown} Buchung(en) ohne Steuertopf",
            "detail": "Unten unter „Unklare Buchungen“ zuordnen. Bis dahin ist der "
                      "Steuerreport gesperrt."})
    return issues


def overview(conn):
    return {"checks": data_checks(conn), "balances": balance_check(conn),
            "flows": flows(conn)}
