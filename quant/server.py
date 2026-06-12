"""HTTP server + JSON API. Loopback by default; --host binds wider behind a
random token printed at startup (the page embeds it for its own API calls)."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, store
from .engine import poll
from .util import now_iso


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
            return {"cfg": {k: cfg[k] for k in ("league", "mode", "risk", "pins")},
                    "snap": snap, "fills": fills[:40], "orders": orders, "hist": hist}

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
                          "note": body.get("note", "card")}
                    if ledger == "paper" and not body.get("instant"):
                        eid = store.append(c, "order", ev)
                        out = {"ok": True, "order": eid}
                    else:
                        eid = store.append(c, "fill", ev)
                        out = {"ok": True, "fill": eid}
                elif path == "/api/fill":
                    eid = store.append(c, "fill", {
                        "ledger": body.get("ledger") or cfg["mode"], "item": body["item"],
                        "side": body.get("side", "buy"), "qty": float(body["qty"]),
                        "px": float(body["px"]), "note": body.get("note", "manual")})
                    out = {"ok": True, "fill": eid}
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
