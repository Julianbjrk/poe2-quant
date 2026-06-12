"""Storage. One SQLite file, WAL mode.

Design rule: user history is an append-only EVENT LOG (fills, voids, orders,
holdings, card transitions). Positions, liquidity and pending orders are folds
over that log — corrections are new events, never edits, so derived state can
always be replayed and can never be silently poisoned.

Tables:
  events       append-only ledger (kind + JSON payload)
  ticks        per-source raw observations, 5-min cadence, kept ~14 days
  bars         hourly OHLC per item+source, kept forever (slow models fit here)
  daily        league-long daily avg/vol from poe2scout
  predictions  one row per forecast a card made; graded in place when known
  snapshots    UI state history (net-worth curves, debrief), pruned
  kv           machine state blobs (filters, calibration, shadow book, baselines)
"""
import json
import sqlite3

from .util import now_iso

SCHEMA = [
    "CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts TEXT, kind TEXT, payload TEXT)",
    "CREATE INDEX IF NOT EXISTS ix_events ON events(kind, id)",
    "CREATE TABLE IF NOT EXISTS ticks(ts TEXT, item TEXT, source TEXT, price_ex REAL, vol_div REAL)",
    "CREATE INDEX IF NOT EXISTS ix_ticks ON ticks(item, source, ts)",
    "CREATE INDEX IF NOT EXISTS ix_ticks_ts ON ticks(ts)",
    "CREATE TABLE IF NOT EXISTS bars(item TEXT, source TEXT, hour TEXT, open REAL, high REAL,"
    " low REAL, close REAL, n INT, vol REAL, PRIMARY KEY(item, source, hour))",
    "CREATE TABLE IF NOT EXISTS daily(item TEXT, date TEXT, avg_ex REAL, vol REAL, PRIMARY KEY(item, date))",
    "CREATE TABLE IF NOT EXISTS predictions(id TEXT PRIMARY KEY, ts TEXT, card_id TEXT, sig TEXT,"
    " item TEXT, payload TEXT, outcome TEXT, graded_ts TEXT)",
    "CREATE TABLE IF NOT EXISTS snapshots(ts TEXT, payload TEXT)",
    "CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)",
]


def connect(path):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("PRAGMA synchronous=NORMAL")
    # a v0.4 ticks table lacks the source column — park it before the new
    # schema (and its indexes) are created, then _migrate_v04 copies it over
    cols = [r[1] for r in c.execute("PRAGMA table_info(ticks)")]
    if cols and "source" not in cols:
        c.execute("ALTER TABLE ticks RENAME TO ticks_v04")
    for ddl in SCHEMA:
        c.execute(ddl)
    _migrate_v04(c)
    return c


def _migrate_v04(c):
    if kv_get(c, "migrated_v04"):
        return
    old_fills = c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='fills'").fetchone()
    if old_fills:
        for ts, pid, side, qty, px, note, paper in c.execute(
                "SELECT ts,play_id,side,qty,price_ex,COALESCE(note,''),COALESCE(paper,0) FROM fills ORDER BY id"):
            item = pid[2:] if str(pid).startswith("c:") else pid
            append(c, "fill", {"ledger": "paper" if paper else "real", "item": item,
                               "side": side, "qty": qty, "px": px, "note": note or "v0.4 import"}, ts=ts)
        raw = kv_get(c, "holdings")
        if raw:
            try:
                h = json.loads(raw)
                append(c, "holdings_set", {"div": h.get("div") or 0, "ex": h.get("ex") or 0,
                                           "chaos": h.get("chaos") or 0}, ts=h.get("ts"))
            except Exception:
                pass
    if c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='ticks_v04'").fetchone():
        c.execute("INSERT INTO ticks SELECT ts, item, 'ninja', price_ex, vol_div FROM ticks_v04")
        c.execute("DROP TABLE ticks_v04")
    kv_set(c, "migrated_v04", "1")
    c.commit()


# ---------------------------------------------------------------- kv -------
def kv_get(c, k):
    try:
        row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row else None


def kv_set(c, k, v):
    c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))


def kv_json(c, k, default=None):
    raw = kv_get(c, k)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def kv_set_json(c, k, obj):
    kv_set(c, k, json.dumps(obj))


# ------------------------------------------------------------- events ------
def append(c, kind, payload, ts=None):
    cur = c.execute("INSERT INTO events(ts, kind, payload) VALUES(?,?,?)",
                    (ts or now_iso(), kind, json.dumps(payload)))
    return cur.lastrowid


def events(c, kinds=None, since_id=0, since_ts=None):
    q, args = "SELECT id, ts, kind, payload FROM events WHERE id>?", [since_id]
    if kinds:
        q += f" AND kind IN ({','.join('?' * len(kinds))})"
        args += list(kinds)
    if since_ts:
        q += " AND ts>?"
        args.append(since_ts)
    q += " ORDER BY id"
    return [{"id": i, "ts": ts, "kind": k, **json.loads(p)}
            for i, ts, k, p in c.execute(q, args)]


def voided_ids(c):
    return {e["void_id"] for e in events(c, ["fill_void", "order_cancel"]) if e.get("void_id")}


def fills(c, ledger):
    dead = voided_ids(c)
    return [e for e in events(c, ["fill"])
            if e["ledger"] == ledger and e["id"] not in dead]


def positions(c, ledger):
    """item -> {qty, cost_ex, avg, realized_ex, target_px, sig, last_ts}. Fold over fills."""
    pos = {}
    for f in fills(c, ledger):
        st = pos.setdefault(f["item"], {"qty": 0.0, "cost_ex": 0.0, "realized_ex": 0.0,
                                        "target_px": None, "sig": None, "last_ts": f["ts"]})
        q, px = float(f["qty"]), float(f["px"])
        if f["side"] == "buy":
            st["qty"] += q
            st["cost_ex"] += q * px
            if f.get("target_px"):
                st["target_px"] = f["target_px"]
            if f.get("sig"):
                st["sig"] = f["sig"]
        else:
            avg = st["cost_ex"] / st["qty"] if st["qty"] > 1e-9 else px
            take = min(q, st["qty"])
            st["realized_ex"] += take * (px - avg)
            st["cost_ex"] -= take * avg
            st["qty"] -= take
        st["last_ts"] = f["ts"]
    for st in pos.values():
        st["avg"] = st["cost_ex"] / st["qty"] if st["qty"] > 1e-9 else 0.0
    return {k: v for k, v in pos.items() if v["qty"] > 1e-9}


def net_spent_after(c, ledger, ts):
    s = 0.0
    for f in fills(c, ledger):
        if f["ts"] > (ts or ""):
            s += float(f["qty"]) * float(f["px"]) * (1 if f["side"] == "buy" else -1)
    return s


def holdings(c):
    ev = events(c, ["holdings_set"])
    return ev[-1] if ev else None


def pending_orders(c, ledger):
    """Resting paper/real orders not yet filled or cancelled."""
    dead = voided_ids(c)
    filled = {e.get("order_id") for e in events(c, ["fill"]) if e.get("order_id")}
    return [e for e in events(c, ["order"])
            if e["ledger"] == ledger and e["id"] not in dead and e["id"] not in filled]


# -------------------------------------------------------------- ticks ------
def insert_ticks(c, ts, rows, last_cache):
    """rows: (item, source, price_ex, vol_div). Dedupes unchanged values via
    last_cache (caller-owned dict). Also maintains hourly bars."""
    n = 0
    hour = ts[:13] + ":00:00" + ts[19:]
    for item, source, px, vol in rows:
        if px is None or px <= 0:
            continue
        key = (item, source)
        prev = last_cache.get(key)
        if prev is not None and abs(prev - px) < 1e-12:
            continue
        last_cache[key] = px
        c.execute("INSERT INTO ticks VALUES(?,?,?,?,?)", (ts, item, source, px, vol))
        row = c.execute("SELECT open,high,low,n,vol FROM bars WHERE item=? AND source=? AND hour=?",
                        (item, source, hour)).fetchone()
        if row:
            o, h, l, cnt, v = row
            c.execute("UPDATE bars SET high=?, low=?, close=?, n=?, vol=? "
                      "WHERE item=? AND source=? AND hour=?",
                      (max(h, px), min(l, px), px, cnt + 1, max(v or 0, vol or 0), item, source, hour))
        else:
            c.execute("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?)",
                      (item, source, hour, px, px, px, px, 1, vol or 0))
        n += 1
    return n


def load_last_cache(c):
    cache = {}
    for item, source, px in c.execute(
            "SELECT item, source, price_ex FROM ticks t WHERE ts = "
            "(SELECT MAX(ts) FROM ticks t2 WHERE t2.item=t.item AND t2.source=t.source)"):
        cache[(item, source)] = px
    return cache


def ticks_since(c, ts, items=None):
    """item -> list[(ts, source, px)] strictly after ts (any source)."""
    q, args = "SELECT item, ts, source, price_ex FROM ticks WHERE ts>?", [ts]
    if items:
        q += f" AND item IN ({','.join('?' * len(items))})"
        args += list(items)
    out = {}
    for item, t, src, px in c.execute(q + " ORDER BY ts", args):
        out.setdefault(item, []).append((t, src, px))
    return out


def hourly_closes(c, item, source="ninja", limit=24 * 14):
    rows = c.execute("SELECT hour, close FROM bars WHERE item=? AND source=? "
                     "ORDER BY hour DESC LIMIT ?", (item, source, limit)).fetchall()
    return list(reversed(rows))


def daily_rows(c, item, limit=14):
    return [r[0] for r in c.execute(
        "SELECT avg_ex FROM daily WHERE item=? ORDER BY date DESC LIMIT ?", (item, limit))]


def prune(c, tick_days, snap_days):
    c.execute("DELETE FROM ticks WHERE ts < datetime('now', ?)", (f"-{int(tick_days)} days",))
    c.execute("DELETE FROM snapshots WHERE ts < datetime('now', ?)", (f"-{int(snap_days)} days",))


# -------------------------------------------------------- predictions ------
def predict_write(c, pid, card_id, sig, item, payload, ts=None):
    c.execute("INSERT OR IGNORE INTO predictions(id, ts, card_id, sig, item, payload) "
              "VALUES(?,?,?,?,?,?)", (pid, ts or now_iso(), card_id, sig, item, json.dumps(payload)))


def predict_grade(c, pid, outcome, ts=None):
    c.execute("UPDATE predictions SET outcome=?, graded_ts=? WHERE id=? AND outcome IS NULL",
              (json.dumps(outcome), ts or now_iso(), pid))


def predictions_graded(c, since_days=30):
    rows = c.execute(
        "SELECT id, ts, card_id, sig, item, payload, outcome FROM predictions "
        "WHERE outcome IS NOT NULL AND graded_ts >= datetime('now', ?) ORDER BY ts",
        (f"-{int(since_days)} days",)).fetchall()
    return [{"id": i, "ts": ts, "card_id": cid, "sig": s, "item": it,
             "pred": json.loads(p), "out": json.loads(o)} for i, ts, cid, s, it, p, o in rows]


def prediction_open(c, pid):
    row = c.execute("SELECT payload FROM predictions WHERE id=? AND outcome IS NULL", (pid,)).fetchone()
    return json.loads(row[0]) if row else None


# ----------------------------------------------------------- snapshots -----
def snap_write(c, ts, payload):
    c.execute("INSERT INTO snapshots VALUES(?,?)", (ts, json.dumps(payload)))


def snaps_latest(c, n=400):
    rows = c.execute("SELECT payload FROM snapshots ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    return [json.loads(r[0]) for r in rows]
