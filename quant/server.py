"""HTTP server + JSON API. Loopback by default; --host binds wider behind a
random token printed at startup (the page embeds it for its own API calls)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__, config, store
from .engine import poll
from .util import now_iso, snap_name


def make_handler(io, token):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json"):
            b = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _authed(self):
            if not token or self.client_address[0] in ("127.0.0.1", "::1"):
                return True
            q = parse_qs(urlparse(self.path).query)
            return (q.get("t", [None])[0] == token
                    or self.headers.get("X-Quant-Token") == token)

        def do_GET(self):
            if not self._authed():
                return self._send(403, '{"err":"token required — use the URL printed at startup"}')
            path = urlparse(self.path).path
            if path == "/":
                from .ui import PAGE
                return self._send(200, PAGE.replace("__TOKEN__", token or ""),
                                  "text/html; charset=utf-8")
            if path == "/api/state":
                return self._send(200, json.dumps(self.state()))
            if path == "/api/debrief":
                q = parse_qs(urlparse(self.path).query)
                since = q.get("since", [""])[0]
                c = store.connect(config.DB_PATH)
                ev = store.events(c, ["card_event", "fill"], since_ts=since)[-30:]
                c.close()
                return self._send(200, json.dumps({"events": ev}))
            if path == "/api/update":  # force a fresh check
                from . import update
                cfg = config.load()
                res = update.check(cfg["update_branch"], token=update.token_from(cfg))
                c = store.connect(config.DB_PATH)
                store.kv_set_json(c, "update_status", {**res, "ts": now_iso()})
                c.commit()
                c.close()
                return self._send(200, json.dumps(res))
            self._send(404, "{}")

        def state(self):
            cfg = config.load()
            c = store.connect(config.DB_PATH)
            snap = store.kv_json(c, "last_snap")
            fills = [{k: f.get(k) for k in ("id", "ts", "ledger", "item", "side",
                                            "qty", "px", "note", "card_id")}
                     for f in (store.fills(c, "paper") + store.fills(c, "real"))]
            fills.sort(key=lambda f: -f["id"])
            orders = store.pending_orders(c, cfg["mode"])
            hist = [{"ts": s["ts"], "nw": s.get("nw_div"), "r": s.get("ex_per_div")}
                    for s in reversed(store.snaps_latest(c, 400))
                    if s.get("mode") == cfg["mode"]]
            c.close()
            c2 = store.connect(config.DB_PATH)
            upd = store.kv_json(c2, "update_status")
            names = store.kv_json(c2, "item_names") or []
            c2.close()
            return {"cfg": {k: cfg[k] for k in ("league", "mode", "risk", "pins")},
                    "snap": snap, "fills": fills[:40], "orders": orders, "hist": hist,
                    "update": upd, "version": __version__, "names": names}

        def do_POST(self):
            if not self._authed():
                return self._send(403, '{"err":"token required"}')
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            path = urlparse(self.path).path
            cfg = config.load()
            c = store.connect(config.DB_PATH)
            try:
                if path == "/api/take":
                    # one tap. paper: resting order, filled on trade-through (honest
                    # fills). real: the user confirmed their actual in-game numbers.
                    ledger = body.get("ledger") or cfg["mode"]
                    side = body.get("side", "buy")
                    ev = {"ledger": ledger, "item": body["item"], "side": side,
                          "qty": float(body["qty"]), "px": float(body["px"]),
                          "card_id": body.get("card_id"), "sig": body.get("sig"),
                          "target_px": body.get("target_px"),
                          # keep the card's own words so a resting order can be reopened
                          "head": body.get("head"), "plan": body.get("plan"),
                          "why": body.get("why"), "note": body.get("note", "card")}
                    if ledger == "paper" and not body.get("instant"):
                        eid = store.append(c, "order", ev)
                        out = {"ok": True, "order": eid}
                    else:
                        eid = store.append(c, "fill", ev)
                        out = {"ok": True, "fill": eid}
                elif path == "/api/fill":
                    item = snap_name(body["item"], store.kv_json(c, "item_names") or [])
                    eid = store.append(c, "fill", {
                        "ledger": body.get("ledger") or cfg["mode"], "item": item,
                        "side": body.get("side", "buy"), "qty": float(body["qty"]),
                        "px": float(body["px"]), "note": body.get("note", "manual")})
                    out = {"ok": True, "fill": eid, "item": item}
                elif path == "/api/fill_edit":
                    # event-sourced edit: void the old fill, append a corrected one.
                    # Nothing is rewritten; positions/benchmarks re-fold automatically.
                    old = store.event_by_id(c, int(body["id"]))
                    if not old or old.get("kind") != "fill":
                        return self._send(404, '{"ok":false,"err":"no such fill"}')
                    store.append(c, "fill_void", {"void_id": old["id"], "note": "edited"})
                    eid = store.append(c, "fill", {
                        "ledger": body.get("ledger") or old.get("ledger") or cfg["mode"],
                        "item": snap_name(body.get("item", old["item"]),
                                          store.kv_json(c, "item_names") or []),
                        "side": body.get("side", old["side"]),
                        "qty": float(body["qty"]), "px": float(body["px"]),
                        # keep the card linkage + exit target so the position behaves
                        "card_id": old.get("card_id"), "sig": old.get("sig"),
                        "target_px": old.get("target_px"),
                        "note": f"edit of #{old['id']}"})
                    out = {"ok": True, "fill": eid, "voided": old["id"]}
                elif path == "/api/update_apply":
                    from . import update
                    res = update.apply(cfg["update_branch"], token=update.token_from(cfg))
                    if res.get("ok"):
                        threading.Timer(0.8, update.restart).start()
                    return self._send(200, json.dumps(res))
                elif path == "/api/rename_item":
                    # re-key a held position to a scanned item so it can be priced:
                    # void each fill of the old name, re-append it under the canonical one
                    names = store.kv_json(c, "item_names") or []
                    old = body["old"]
                    new = snap_name(body["new"], names)
                    ledger = body.get("ledger") or cfg["mode"]
                    moved = 0
                    for f in store.fills(c, ledger):
                        if f["item"] != old:
                            continue
                        store.append(c, "fill_void", {"void_id": f["id"], "note": "rematched"})
                        store.append(c, "fill", {
                            "ledger": f["ledger"], "item": new, "side": f["side"],
                            "qty": f["qty"], "px": f["px"], "card_id": f.get("card_id"),
                            "sig": f.get("sig"), "target_px": f.get("target_px"),
                            "order_id": f.get("order_id"), "note": f"rematched from {old}"})
                        moved += 1
                    out = {"ok": True, "moved": moved, "item": new}
                elif path == "/api/order_fill":
                    # record that a resting order filled — possibly only PARTLY.
                    # The filled part becomes a buy; any remainder keeps resting.
                    oid = int(body["order_id"])
                    order = store.event_by_id(c, oid)
                    if not order or order.get("kind") != "order":
                        return self._send(404, '{"ok":false,"err":"no such order"}')
                    full = float(order["qty"])
                    q = max(0.0, min(float(body.get("qty") or full), full))
                    if q <= 0:
                        return self._send(200, '{"ok":false,"err":"quantity must be positive"}')
                    ledger = order.get("ledger") or cfg["mode"]
                    store.append(c, "fill", {
                        "ledger": ledger, "item": order["item"], "side": "buy", "qty": q,
                        "px": float(order["px"]), "card_id": order.get("card_id"),
                        "sig": order.get("sig"), "target_px": order.get("target_px"),
                        "order_id": oid, "note": "order filled"})
                    remaining = full - q
                    new_oid = None
                    if remaining > 1e-9:
                        new_oid = store.append(c, "order", {
                            "ledger": ledger, "item": order["item"], "side": "buy",
                            "qty": remaining, "px": float(order["px"]),
                            "card_id": order.get("card_id"), "sig": order.get("sig"),
                            "target_px": order.get("target_px"), "head": order.get("head"),
                            "plan": order.get("plan"), "why": order.get("why"),
                            "note": "remainder after partial fill"})
                    out = {"ok": True, "filled": q, "remaining": round(remaining, 4),
                           "new_order": new_oid, "item": order["item"], "px": float(order["px"])}
                elif path == "/api/discard_item":
                    # stop tracking a position (e.g. a CHECK item the scanner can't
                    # price): void its fills so it folds out. Event-sourced — reversible.
                    item = body["item"]
                    ledger = body.get("ledger") or cfg["mode"]
                    n = 0
                    for f in store.fills(c, ledger):
                        if f["item"] == item:
                            store.append(c, "fill_void", {"void_id": f["id"], "note": "discarded"})
                            n += 1
                    out = {"ok": True, "voided": n}
                elif path == "/api/void":
                    kind = "order_cancel" if body.get("kind") == "order" else "fill_void"
                    store.append(c, kind, {"void_id": int(body["id"]),
                                           "note": body.get("note", "corrected")})
                    out = {"ok": True}
                elif path == "/api/holdings":
                    store.append(c, "holdings_set", {
                        "div": float(body.get("div") or 0), "ex": float(body.get("ex") or 0),
                        "chaos": float(body.get("chaos") or 0)})
                    out = {"ok": True}
                elif path == "/api/mode":
                    if body.get("mode") in ("paper", "real"):
                        cfg["mode"] = body["mode"]
                    if body.get("risk") in ("conservative", "standard", "aggressive"):
                        cfg["risk"] = body["risk"]
                    config.save_surface(cfg)
                    out = {"ok": True}
                elif path == "/api/refresh":
                    c.commit()
                    c.close()
                    c = None
                    snap = poll(config.load(), io)
                    return self._send(200, json.dumps({"ok": True,
                                                       "errors": snap.get("errors", [])}))
                else:
                    return self._send(404, "{}")
                c.commit()
                self._send(200, json.dumps(out))
            finally:
                if c is not None:
                    c.close()
    return H


def serve(io, host, port, token):
    return ThreadingHTTPServer((host, port), make_handler(io, token))
