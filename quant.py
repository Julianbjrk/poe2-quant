#!/usr/bin/env python3
"""
QUANT — read-only PoE2 currency day-trading decision-support dashboard.

v0.3: market scanner + action cards. Every poll (default 5 min) it scans all
exchange-traded currencies on poe.ninja, accumulates its own intraday price
history in SQLite, pulls real pair data from poe2scout's exchange snapshots,
and turns whatever passes the safety gates into at most a handful of
plain-language action cards ("buy X at ≤P, list at Q, expected +Δ"). When
nothing passes, it says NO TRADE. Paper mode logs hypothetical fills so the
tool must prove itself before real divines are at stake.

You do all trading by hand in-game. No automation, no game interaction,
no credentials. Python 3.10+, stdlib only.

Run:        python quant.py            (serves http://localhost:8377)
Probe APIs: python quant.py --probe
One poll:   python quant.py --once
"""
import difflib, json, math, sqlite3, sys, threading, time, urllib.error, urllib.parse, urllib.request, webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DB_PATH = ROOT / "quant.db"
NINJA_BASE = "https://poe.ninja/poe2/api/economy"
SCOUT_BASE = "https://poe2scout.com/api"
REALM = "poe2"  # poe2scout path segment (see /api/Realms: game_api_id)
HEADERS = {"User-Agent": "QuantDashboard/0.3 (personal read-only price monitor)"}

# poe.ninja exchange `type` values verified to return data (case-sensitive).
NINJA_TYPES = ["Currency", "Fragments", "Essences", "Runes", "SoulCores",
               "LineageSupportGems", "Expedition", "Ritual", "Abyss",
               "Delirium", "UncutGems", "Idols"]
# squashed-lowercase -> canonical; extras map PoE2 item families to the ninja
# type that actually carries them (omens trade under Ritual, emotions under Delirium).
NINJA_TYPE_ALIASES = {t.lower(): t for t in NINJA_TYPES} | {
    "omens": "Ritual", "distilledemotions": "Delirium", "emotions": "Delirium",
    "soulcore": "SoulCores", "uncut": "UncutGems", "gems": "LineageSupportGems",
}

DEFAULT_CONFIG = {
    "league": "auto",
    "start_capital_div": 4.0,
    "poll_minutes": 5,
    "no_fill_hours": 48,
    "paper_mode": True,
    "risk": {
        "min_volume_div_day": 150,     # ignore anything thinner than this
        "high_conf_volume": 500,       # div/day needed for HIGH confidence
        "max_pos_pct_volume": 2,       # position ≤ this % of item's daily volume
        "max_bankroll_pct": 30,        # position ≤ this % of bankroll
        "liquid_reserve_pct": 25,      # never deploy this slice of bankroll
        "max_open_positions": 3,
        "fee_pct_per_side": 1.0,       # exchange gold-fee estimate, % of value, each way
        "min_edge_net_pct": 4,         # round-trip edge after fees must beat this
        "min_profit_ex": 10,           # don't suggest flips worth less than this
        "dip_trigger_pct": 4,          # 24h mean-reversion entry threshold
        "knife_guard_pct": 12,         # skip dips when 7d trend fell more than this
        "spread_capture_pct": 6,       # assumed capturable spread for MAKE plays
        "make_min_volume": 600,        # div/day needed before MAKE is suggested
        "route_min_dev_pct": 6,        # ex-pair vs div-pair divergence for ROUTE
        "max_cards": 3
    },
    "scan_types": NINJA_TYPES,
    "plays": [
        {
            "id": "jewellers", "label": "Watchlist example: Perfect Jeweller's",
            "source": "exchange:Currency", "match": "Perfect Jeweller's Orb",
            "entry_max_ex": 0, "exit_target_ex": 0, "abandon_drop_pct": 20,
            "budget_div": 1.0,
            "notes": "Optional manual watchlist. The scanner finds plays on its own; "
                     "source kinds: exchange:<NinjaType> currency:<cat> unique:<cat> auto"
        }
    ]
}

# ---------------------------------------------------------------- storage --
def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS snapshots(ts TEXT, payload TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS fills(
        id INTEGER PRIMARY KEY, ts TEXT, play_id TEXT, side TEXT,
        qty REAL, price_ex REAL, note TEXT)""")
    c.execute("CREATE TABLE IF NOT EXISTS sigstate(play_id TEXT, kind TEXT, since TEXT, PRIMARY KEY(play_id,kind))")
    c.execute("CREATE TABLE IF NOT EXISTS ticks(ts TEXT, item TEXT, typ TEXT, price_ex REAL, vol_div REAL)")
    c.execute("CREATE INDEX IF NOT EXISTS ix_ticks ON ticks(item, ts)")
    c.execute("CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS daily(item TEXT, date TEXT, avg_ex REAL, vol REAL, PRIMARY KEY(item, date))")
    try:
        c.execute("ALTER TABLE fills ADD COLUMN paper INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # column already exists
    return c

def kv_get(c, k):
    row = c.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return row[0] if row else None

def kv_set(c, k, v):
    c.execute("INSERT OR REPLACE INTO kv VALUES(?,?)", (k, v))

def holdings_get(c):
    """User-entered liquid capital: {div, ex, chaos, ts, base_div} or None."""
    raw = kv_get(c, "holdings")
    try:
        return json.loads(raw) if raw else None
    except Exception:
        return None

def spent_since(c, paper, ts):
    """Net ex spent on fills after ts (buys positive, sells negative)."""
    rows = c.execute("SELECT side,qty,price_ex FROM fills WHERE paper=? AND ts>?",
                     (1 if paper else 0, ts)).fetchall()
    return sum(q * p if s == "buy" else -q * p for s, q, p in rows)

def _merge_defaults(cfg, defaults):
    """Fill missing keys (one level deep for dicts) so old configs keep working."""
    for k, v in defaults.items():
        if k not in cfg:
            cfg[k] = v
        elif isinstance(v, dict) and isinstance(cfg[k], dict):
            for k2, v2 in v.items():
                cfg[k].setdefault(k2, v2)
    return cfg

def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, indent=2), encoding="utf-8")
        print(f"Wrote starter {CONFIG_PATH.name} — edit it with your playbook's play cards.")
    # utf-8-sig: tolerate the BOM that Notepad/PowerShell prepend on Windows
    return _merge_defaults(json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig")), DEFAULT_CONFIG)

def now_iso(): return datetime.now(timezone.utc).isoformat(timespec="seconds")

# ----------------------------------------------------------------- fetch ---
def get_json(url, timeout=25, retries=1):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if attempt < retries and (code is None or code >= 500):
                time.sleep(2); continue
            raise

def _norm(s):
    return " ".join((s or "").replace("’", "'").casefold().split())

def best_match(query, names):
    """Most specific name matching query: exact > prefix > substring > all-words."""
    qn = _norm(query)
    if not qn:
        return None
    best_tier, best_nm, best_len = 0, None, 10**9
    for nm in names:
        nn = _norm(nm)
        if nn == qn: tier = 4
        elif nn.startswith(qn): tier = 3
        elif qn in nn: tier = 2
        elif all(w in nn for w in qn.split()): tier = 1
        else: continue
        if tier > best_tier or (tier == best_tier and len(nn) < best_len):
            best_tier, best_nm, best_len = tier, nm, len(nn)
    return best_nm

def fmt_ex(v):
    if v is None: return "—"
    if v >= 100: return f"{v:,.0f}"
    if v >= 10: return f"{v:.1f}"
    if v >= 1: return f"{v:.2f}"
    return f"{v:.3f}"

# ----------------------------------------------------------- poe.ninja -----
def ninja_overview(league, typ):
    q = urllib.parse.urlencode({"league": league, "type": typ})
    return get_json(f"{NINJA_BASE}/exchange/current/overview?{q}")

def parse_ninja(raw):
    """-> {price_ex, trend, vol_div, spark, ex_per_div}. Names come from the
    top-level `items` list (core.items only holds the 3 core currencies)."""
    core = raw.get("core") or {}
    names = {}
    for src in (raw.get("items") or []), (core.get("items") or []):
        for i in src:
            if i.get("id"):
                names[i["id"]] = i.get("name") or i["id"]
    prim, trend, volp, spark = {}, {}, {}, {}
    for ln in raw.get("lines") or []:
        nm = names.get(ln.get("id"), ln.get("id"))
        if ln.get("primaryValue") is None:
            continue
        prim[nm] = float(ln["primaryValue"])
        sp = ln.get("sparkline") or {}
        if sp.get("totalChange") is not None:
            trend[nm] = float(sp["totalChange"])
        if sp.get("data"):
            spark[nm] = [float(x) for x in sp["data"] if x is not None]
        if ln.get("volumePrimaryValue") is not None:
            volp[nm] = float(ln["volumePrimaryValue"])
    # primaryValue is denominated in core.primary (currently divine);
    # core.rates gives units of X per 1 primary.
    rates = core.get("rates") or {}
    ex_per_primary = rates.get("exalted") or (1 / prim["Exalted Orb"] if prim.get("Exalted Orb") else 1.0)
    price_ex = {k: v * ex_per_primary for k, v in prim.items()}
    ex_per_div = price_ex.get("Divine Orb")
    vol_div = {}
    if ex_per_div:
        vol_div = {k: v * ex_per_primary / ex_per_div for k, v in volp.items()}
    return {"price_ex": price_ex, "trend": trend, "vol_div": vol_div,
            "spark": spark, "ex_per_div": ex_per_div}

# ----------------------------------------------------------- poe2scout -----
_leagues_cache = {"ts": 0.0, "rows": None}

def scout_leagues(force=False):
    if not force and _leagues_cache["rows"] is not None and time.time() - _leagues_cache["ts"] < 6 * 3600:
        return _leagues_cache["rows"]
    rows = get_json(f"{SCOUT_BASE}/{REALM}/Leagues")
    _leagues_cache.update(ts=time.time(), rows=rows)
    return rows

def resolve_league(cfg):
    """-> {name, divine_price, note}. 'auto' picks the current softcore league;
    explicit names are validated against poe2scout (Value or ShortName)."""
    want = str(cfg.get("league") or "auto").strip()
    try:
        rows = scout_leagues()
    except Exception as e:
        name = want if want.casefold() not in ("auto", "current", "") else "Standard"
        return {"name": name, "divine_price": None,
                "note": f"poe2scout league list unavailable ({e}); using '{name}'"}
    if want.casefold() in ("auto", "current", ""):
        cur = [l for l in rows if l.get("IsCurrent")]
        soft = [l for l in cur if not str(l.get("ShortName", "")).endswith("hc")
                and not str(l.get("Value", "")).startswith("HC")]
        pick = (soft or cur or rows)[0]
        return {"name": pick["Value"], "divine_price": pick.get("DivinePrice"), "note": None}
    for l in rows:
        if want.casefold() in (str(l.get("Value", "")).casefold(), str(l.get("ShortName", "")).casefold()):
            return {"name": l["Value"], "divine_price": l.get("DivinePrice"), "note": None}
    current = ", ".join(l["Value"] for l in rows if l.get("IsCurrent")) or "?"
    return {"name": want, "divine_price": None,
            "note": f"league '{want}' not in poe2scout list (current: {current}) — using it verbatim"}

def _lg(league): return urllib.parse.quote(league, safe="")

def scout_by_category(kind, category, league, search="", max_pages=4):
    """kind: 'Uniques' | 'Currencies'. Returns raw Items across pages,
    prices already converted to exalted via ReferenceCurrency."""
    items, page, pages = [], 1, 1
    while page <= min(pages, max_pages):
        q = {"Category": category, "Page": page, "PerPage": 250, "ReferenceCurrency": "exalted"}
        if search:
            q["Search"] = search
        data = get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/{kind}/ByCategory?{urllib.parse.urlencode(q)}")
        pages = int(data.get("Pages") or 1)
        items += data.get("Items") or []
        page += 1
    return items

def scout_items_index(league):
    """Full name->price index (~1300 items, all categories, prices in exalted)."""
    return get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/Items")

def scout_trend(item):
    """%change from oldest PriceLog to CurrentPrice (logs are daily, default 7)."""
    try:
        logs = [l for l in (item.get("PriceLogs") or []) if l and l.get("Price") is not None]
        cur = item.get("CurrentPrice")
        if len(logs) < 2 or not cur:
            return None
        logs.sort(key=lambda l: l.get("Time") or "")
        old = float(logs[0]["Price"])
        return (float(cur) - old) / old * 100 if old else None
    except Exception:
        return None

def match_scout(query, items, src):
    by = {}
    for i in items:
        if i.get("CurrentPrice") is None:
            continue
        for nm in (i.get("Name"), i.get("Text")):
            if nm:
                by.setdefault(nm, i)
    nm = best_match(query, by.keys())
    if not nm:
        return None
    i = by[nm]
    return {"name": i.get("Name") or i.get("Text"), "price_ex": float(i["CurrentPrice"]),
            "trend7": scout_trend(i), "qty": i.get("CurrentQuantity"), "vol_div": None, "src": src}

def auto_lookup(query, league, cache, pool):
    """Resolve any item by name via the full index, then enrich with
    PriceLogs/quantity from the right ByCategory endpoint."""
    if "_index" not in cache:
        cache["_index"] = scout_items_index(league)
    idx = cache["_index"]
    by = {}
    for i in idx:
        for nm in (i.get("Name"), i.get("Text")):
            if nm:
                pool.add(nm)
                by.setdefault(nm, i)
    nm = best_match(query, by.keys())
    if not nm:
        return None
    rec = by[nm]
    cat = rec.get("CategoryApiId")
    is_unique = bool(rec.get("Name"))  # uniques have Name, currency-likes don't
    kinds = ("Uniques", "Currencies") if is_unique else ("Currencies", "Uniques")
    for kind in kinds:
        try:
            items = scout_by_category(kind, cat, league, search=rec.get("Name") or rec.get("Text"))
            hit = match_scout(query, items, f"poe2scout auto:{cat}")
            if hit:
                return hit
        except Exception:
            continue
    if rec.get("CurrentPrice") is not None:
        return {"name": nm, "price_ex": float(rec["CurrentPrice"]), "trend7": None,
                "qty": None, "vol_div": None, "src": f"poe2scout auto:{cat}"}
    return None

# ------------------------------------------------ league-long history ------
_idx_cache = {"ts": 0.0, "by_text": {}}

def _items_by_text(league):
    if time.time() - _idx_cache["ts"] > 3600 or not _idx_cache["by_text"]:
        idx = scout_items_index(league)
        _idx_cache.update(ts=time.time(), by_text={
            i["Text"]: i["ItemId"] for i in idx if i.get("Text") and i.get("ItemId")})
    return _idx_cache["by_text"]

def daily_update(c, league, names, max_new=8):
    """Backfill daily average/volume since league start (poe2scout
    DailyStatsHistory, exalted-denominated) for the given items, at most once
    per item per day and max_new fetches per poll to stay polite."""
    today = datetime.now(timezone.utc).date().isoformat()
    try:
        by = _items_by_text(league)
    except Exception:
        return
    fetched = 0
    for nm in dict.fromkeys(names):
        if kv_get(c, "daily:" + nm) == today:
            continue
        kv_set(c, "daily:" + nm, today)
        iid = by.get(nm)
        if not iid:
            continue
        try:
            d = get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/Items/{iid}/DailyStatsHistory?DayCount=60")
        except Exception:
            continue
        for ds in d.get("DailyStats") or []:
            if ds.get("Time") and ds.get("Average") is not None:
                c.execute("INSERT OR REPLACE INTO daily VALUES(?,?,?,?)",
                          (nm, ds["Time"], float(ds["Average"]), float(ds.get("Volume") or 0)))
        fetched += 1
        if fetched >= max_new:
            break
        time.sleep(0.25)
    c.commit()

def stats14(c, name):
    """Mean/σ of the item's daily average price over the last 14 league days."""
    rows = [r[0] for r in c.execute(
        "SELECT avg_ex FROM daily WHERE item=? ORDER BY date DESC LIMIT 14", (name,))]
    if len(rows) < 5:
        return None
    m = sum(rows) / len(rows)
    sd = (sum((x - m) ** 2 for x in rows) / len(rows)) ** 0.5
    return {"mean14": m, "sd14": sd, "n": len(rows)}

# ------------------------------------------------- exchange pair data ------
_pairs_cache = {"ts": 0.0, "info": None, "note": None}

def fetch_pair_info(league, ex_per_div):
    """Per-item implied ex price via its exalted pair vs its divine pair, from
    poe2scout's hourly snapshot of the in-game exchange. Self-validates against
    ninja's ex/div before trusting the data. Cached ~1h (it's one big request)."""
    if _pairs_cache["info"] is not None and time.time() - _pairs_cache["ts"] < 55 * 60:
        return _pairs_cache["info"], _pairs_cache["note"]
    info, note = {}, None
    try:
        raw = get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/SnapshotPairs", timeout=40)
        routes = {}  # name -> {"exalted": {...}, "divine": {...}}
        for p in raw or []:
            one, two = p.get("CurrencyOne") or {}, p.get("CurrencyTwo") or {}
            for side, sdata, other in ((one, p.get("CurrencyOneData"), two),
                                       (two, p.get("CurrencyTwoData"), one)):
                if other.get("ApiId") not in ("exalted", "divine") or not sdata:
                    continue
                rp = float(sdata.get("RelativePrice") or 0)
                vt = int(sdata.get("VolumeTraded") or 0)
                val = float(sdata.get("ValueTraded") or 0)  # in ex
                if rp > 0 and vt > 0 and side.get("Text"):
                    routes.setdefault(side["Text"], {})[other["ApiId"]] = {
                        "px_ex": rp, "trades": vt, "value_ex": val}
        chk = (routes.get("Divine Orb") or {}).get("exalted", {}).get("px_ex")
        if not chk or not ex_per_div or abs(chk - ex_per_div) / ex_per_div > 0.15:
            note = f"pair data failed sanity check (divine via pairs={chk}, ninja={ex_per_div}) — route signals off"
        else:
            for name, r in routes.items():
                if "exalted" in r and "divine" in r:
                    a, b = r["exalted"]["px_ex"], r["divine"]["px_ex"]
                    info[name] = {"ex_px": a, "div_px": b,
                                  "dev_pct": (b - a) / a * 100,
                                  "trades": min(r["exalted"]["trades"], r["divine"]["trades"]),
                                  "value_ex": min(r["exalted"]["value_ex"], r["divine"]["value_ex"])}
    except Exception as e:
        note = f"SnapshotPairs unavailable: {e}"
    _pairs_cache.update(ts=time.time(), info=info, note=note)
    return info, note

# ------------------------------------------------------- tick history ------
_last_tick = None  # item -> (price_ex, vol_div), to skip storing unchanged rows

def store_ticks(c, ts, rows):
    global _last_tick
    if _last_tick is None:
        _last_tick = {}
        for item, px, vol in c.execute(
                "SELECT item, price_ex, vol_div FROM ticks WHERE ts = (SELECT MAX(ts) FROM ticks t2 WHERE t2.item = ticks.item)"):
            _last_tick[item] = (px, vol)
    ins = []
    for item, typ, px, vol in rows:
        prev = _last_tick.get(item)
        if prev and prev[0] == px and (prev[1] or 0) == (vol or 0):
            continue
        ins.append((ts, item, typ, px, vol))
        _last_tick[item] = (px, vol)
    if ins:
        c.executemany("INSERT INTO ticks VALUES(?,?,?,?,?)", ins)
    c.execute("DELETE FROM ticks WHERE ts < datetime('now','-14 days')")
    return len(ins)

def tick_metrics(c):
    """Per-item 24h stats from our own accumulated history."""
    out = {}
    for item, mean24, lo24, hi24, n in c.execute(
            "SELECT item, AVG(price_ex), MIN(price_ex), MAX(price_ex), COUNT(*) "
            "FROM ticks WHERE ts >= datetime('now','-1 day') GROUP BY item"):
        out[item] = {"mean24": mean24, "lo24": lo24, "hi24": hi24, "n24": n}
    row = c.execute("SELECT MIN(ts) FROM ticks").fetchone()
    age_h = 0.0
    if row and row[0]:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).total_seconds() / 3600
        except Exception:
            pass
    return out, age_h

def positions_raw(c, paper):
    """play_id -> {qty, cost_ex, avg} from fills, for sizing/exit logic."""
    pos = {}
    for pid, side, qty, px in c.execute(
            "SELECT play_id, side, qty, price_ex FROM fills WHERE paper=? ORDER BY id", (1 if paper else 0,)):
        st = pos.setdefault(pid, {"qty": 0.0, "cost_ex": 0.0})
        if side == "buy":
            st["qty"] += qty; st["cost_ex"] += qty * px
        else:
            avg = st["cost_ex"] / st["qty"] if st["qty"] else 0
            st["cost_ex"] -= qty * avg; st["qty"] -= qty
    for st in pos.values():
        st["avg"] = st["cost_ex"] / st["qty"] if st["qty"] else 0
    return {k: v for k, v in pos.items() if v["qty"] > 1e-9}

# --------------------------------------------------------------- scanner ---
def scan_market(cfg, data_by_type, metrics, hist_h, pair_info, ex_per_div):
    """Score every scanned currency; return candidate rows sorted best-first."""
    r = cfg["risk"]
    fee_rt = 2 * r["fee_pct_per_side"]
    rows = []
    for typ, d in data_by_type.items():
        for name, px in d["price_ex"].items():
            if name == "Exalted Orb" or px <= 0:
                continue
            vol = d["vol_div"].get(name) or 0
            if vol < r["min_volume_div_day"]:
                continue
            tr7 = d["trend"].get(name)
            m = metrics.get(name) or {}
            sigs = []  # (kind, edge_pct, why, entry_px, target_px)
            # DIP — mean reversion vs our own 24h history (needs a few hours of ticks)
            if m.get("n24", 0) >= 6 and m.get("mean24"):
                dev = (px - m["mean24"]) / m["mean24"] * 100
                if dev <= -r["dip_trigger_pct"] and (tr7 is None or tr7 > -r["knife_guard_pct"]):
                    edge = min(-dev * 0.7, 10)  # assume 70% reversion, capped
                    sigs.append(("DIP", edge, f"{-dev:.0f}% under 24h mean ({fmt_ex(m['mean24'])} ex)", None, None))
            # DIP fallback before history exists — vs the 7d sparkline's own path
            elif d["spark"].get(name) and tr7 is not None:
                sp = d["spark"][name]
                if len(sp) >= 4:
                    devw = sp[-1] - sum(sp) / len(sp)
                    if devw <= -r["dip_trigger_pct"] * 1.5 and tr7 > -r["knife_guard_pct"] * 1.5:
                        edge = min(-devw * 0.5, 8)
                        sigs.append(("DIP", edge, f"{-devw:.0f}% under its 7d trendline", None, None))
            # MAKE — patient two-sided spread capture, top-liquidity only
            if vol >= r["make_min_volume"]:
                sigs.append(("MAKE", r["spread_capture_pct"],
                             f"deep book ({vol:,.0f} div/d) — work both sides of the spread", None, None))
            # ROUTE — same item priced differently via ex-pair vs div-pair;
            # entry/exit MUST use the pair prices, not ninja's mid. Both route
            # prices must also sit near ninja's mid: the divine pair gives fake
            # +5000% "divergences" on items worth far less than 1 div (lot-size
            # distortion), and those must never become cards.
            pa = pair_info.get(name)
            if (pa and abs(pa["dev_pct"]) >= r["route_min_dev_pct"] and pa["trades"] >= 10
                    and abs(pa["ex_px"] - px) / px <= 0.4 and abs(pa["div_px"] - px) / px <= 0.4):
                edge = min(abs(pa["dev_pct"]) - 2, 12)  # 2% slippage buffer
                cheap, rich = ("exalted", "divine") if pa["dev_pct"] > 0 else ("divine", "exalted")
                entry, target = min(pa["ex_px"], pa["div_px"]), max(pa["ex_px"], pa["div_px"])
                sigs.append(("ROUTE", edge,
                             f"{fmt_ex(pa['ex_px'])} ex via ex-pair vs {fmt_ex(pa['div_px'])} ex via div-pair "
                             f"({pa['dev_pct']:+.0f}%) — buy paying {cheap}, sell for {rich}",
                             entry, target))
            if not sigs:
                continue
            kind, edge, why, entry_px, target_px = max(sigs, key=lambda s: s[1])
            net = edge - fee_rt
            if net < r["min_edge_net_pct"]:
                continue
            conf = "HIGH" if (vol >= r["high_conf_volume"] and (kind == "MAKE" or m.get("n24", 0) >= 12 or hist_h < 24)) else "MED"
            rows.append({"name": name, "typ": typ, "sig": kind, "px": px, "vol": vol,
                         "trend7": tr7, "edge_net": net, "why": why, "conf": conf,
                         "entry_px": entry_px, "target_px": target_px})
    rows.sort(key=lambda x: x["edge_net"] * x["vol"] * (1.0 if x["conf"] == "HIGH" else 0.6), reverse=True)
    return rows

def build_cards(cfg, scan_rows, port_pos, price_map, trend_map, bankroll_ex, ex_per_div, ex_avail=None):
    """Entry cards from scan rows + exit/abandon cards for open scanner positions."""
    r = cfg["risk"]
    cards = []
    # exits first — closing a position never competes for card slots
    for pid, st in port_pos.items():
        if not pid.startswith("c:"):
            continue
        name = pid[2:]
        px = price_map.get(name)
        if px is None:
            cards.append({"pid": pid, "name": name, "act": "CHECK", "conf": "MED",
                          "head": f"CHECK {name} — no current price this poll",
                          "sub": f"holding {st['qty']:g} @ avg {fmt_ex(st['avg'])} ex",
                          "why": "item missing from scan; verify in-game", "qty": st["qty"], "px": None})
            continue
        gain = (px - st["avg"]) / st["avg"] * 100 if st["avg"] else 0
        tr7 = trend_map.get(name)
        target = st["avg"] * (1 + (r["min_edge_net_pct"] + 2 * r["fee_pct_per_side"]) / 100)
        if px >= target:
            cards.append({"pid": pid, "name": name, "act": "SELL", "conf": "HIGH",
                          "head": f"SELL {st['qty']:g}× {name} @ ≥{fmt_ex(px)} ex",
                          "sub": f"in at {fmt_ex(st['avg'])} ex → {gain:+.0f}% — target reached, take the profit",
                          "why": "list at market; reprice 5% under if no fill in a day",
                          "qty": st["qty"], "px": px})
        elif tr7 is not None and tr7 <= -r["knife_guard_pct"]:
            cards.append({"pid": pid, "name": name, "act": "ABANDON", "conf": "HIGH",
                          "head": f"ABANDON {st['qty']:g}× {name} — sell at market ({fmt_ex(px)} ex)",
                          "sub": f"7d trend {tr7:+.0f}% breached the guard; {gain:+.0f}% vs entry",
                          "why": "cut losers fast — thin-league dumps rarely bounce quickly",
                          "qty": st["qty"], "px": px})
        else:
            cards.append({"pid": pid, "name": name, "act": "HOLD", "conf": "MED",
                          "head": f"HOLD {st['qty']:g}× {name} ({gain:+.0f}%, now {fmt_ex(px)} ex)",
                          "sub": f"exit at ≥{fmt_ex(target)} ex" + (f" · 7d {tr7:+.0f}%" if tr7 is not None else ""),
                          "why": "no trigger yet", "qty": st["qty"], "px": px})
    # entry cards
    open_scan = [p for p in port_pos if p.startswith("c:")]
    slots = max(0, r["max_open_positions"] - len(open_scan))
    reserve_ex = bankroll_ex * r["liquid_reserve_pct"] / 100
    invested = sum(st["cost_ex"] for st in port_pos.values())
    available = bankroll_ex - reserve_ex - invested
    n_entries = 0
    for row in scan_rows:
        if n_entries >= min(r["max_cards"], slots):
            break
        pid = f"c:{row['name']}"
        if pid in port_pos:
            continue
        basis = row.get("entry_px") or row["px"]
        size_cap = min(bankroll_ex * r["max_bankroll_pct"] / 100,
                       row["vol"] * (ex_per_div or 0) * r["max_pos_pct_volume"] / 100,
                       max(available, 0))
        qty = int(size_cap // basis)
        if qty < 1:
            continue
        spend = qty * basis
        profit = spend * row["edge_net"] / 100
        # if the user's liquid exalted won't cover this, a div->ex conversion
        # hop is needed first — note it and charge the extra fee leg
        conv_note = ""
        if ex_avail is not None and ex_per_div:
            if spend > ex_avail + 1e-9:
                need = spend - ex_avail
                conv_fee = need * r["fee_pct_per_side"] / 100
                profit -= conv_fee
                conv_note = (f" · convert ≈{need / ex_per_div:.1f} div → ex first "
                             f"(extra fee ≈{conv_fee:.0f} ex, included)")
        if profit < r["min_profit_ex"]:
            continue
        if row["sig"] == "MAKE":
            half = cfg["risk"]["spread_capture_pct"] / 2
            bid, ask = row["px"] * (1 - half / 100), row["px"] * (1 + half / 100)
            head = f"MAKE {row['name']}: bid {qty}× @ {fmt_ex(bid)} ex, relist filled @ {fmt_ex(ask)} ex"
            sub = (f"patient two-sided orders around {fmt_ex(row['px'])} ex — "
                   f"expected +{fmt_ex(profit)} ex (+{row['edge_net']:.0f}%) per cycle after est. fees")
            entry_px = bid
        else:
            target = row.get("target_px") or basis * (1 + row["edge_net"] / 100)
            head = f"BUY up to {qty}× {row['name']} @ ≤{fmt_ex(basis)} ex (≈{spend / ex_per_div:.2f} div)" \
                if ex_per_div else f"BUY up to {qty}× {row['name']} @ ≤{fmt_ex(basis)} ex"
            sub = f"then list at {fmt_ex(target)} ex → expected +{fmt_ex(profit)} ex after est. fees"
            entry_px = basis
        cards.append({"pid": pid, "name": row["name"], "act": row["sig"], "conf": row["conf"],
                      "head": head, "sub": sub + conv_note,
                      "why": f"{row['why']} · vol {row['vol']:,.0f} div/d"
                             + (f" · 7d {row['trend7']:+.0f}%" if row["trend7"] is not None else ""),
                      "qty": qty, "px": round(entry_px, 4)})
        available -= spend
        if ex_avail is not None:
            ex_avail = max(ex_avail - spend, 0)
        n_entries += 1
    return cards

# ------------------------------------------------------------------ poll ---
_poll_lock = threading.Lock()

def _liq(rec):
    if rec.get("qty") is not None:
        return f"{rec['qty']:g} listed"
    if rec.get("vol_div"):
        return f"{rec['vol_div']:.0f} div/d"
    return None

def miss_msg(p, pool, league):
    sugg = difflib.get_close_matches(p["match"], list(pool), n=3, cutoff=0.45)
    s = f" Close names: {'; '.join(sugg)}." if sugg else ""
    return f"no match for '{p['match']}' via {p['source']} in {league}.{s} Tip: source \"auto\" searches everything."

def poll(cfg, store=True):
    with _poll_lock:
        return _poll(cfg, store)

def _poll(cfg, store):
    info = resolve_league(cfg)
    league = info["name"]
    snap = {"ts": now_iso(), "league": league, "ex_per_div": None, "paper": bool(cfg.get("paper_mode")),
            "items": {}, "misses": {}, "errors": [], "cards": [], "scan": [], "scan_stats": {}}
    if info.get("note"):
        snap["errors"].append(info["note"])
    ninja_cache, scout_cache, pool = {}, {}, set()

    def ninja(typ):
        t = NINJA_TYPE_ALIASES.get(_norm(typ).replace(" ", "").replace("-", ""), typ or "Currency")
        if t not in ninja_cache:
            d = parse_ninja(ninja_overview(league, t))
            if not d["price_ex"]:
                raise ValueError(f"poe.ninja type '{t}' returned no data for {league}; "
                                 f"known-good types: {', '.join(NINJA_TYPES)}")
            ninja_cache[t] = d
        return ninja_cache[t]

    # ---- scan every configured type (this also serves the plays below) ----
    for typ in cfg.get("scan_types") or ["Currency"]:
        try:
            ninja(typ)
            time.sleep(0.25)
        except Exception as e:
            snap["errors"].append(f"scan {typ}: {e}")
    cur = ninja_cache.get("Currency")
    snap["ex_per_div"] = cur["ex_per_div"] if cur else None
    if not snap["ex_per_div"] and info.get("divine_price"):
        snap["ex_per_div"] = info["divine_price"]
        snap["errors"].append("ex/div taken from poe2scout (poe.ninja unavailable)")
    ex_per_div = snap["ex_per_div"]

    pair_info, pair_note = fetch_pair_info(league, ex_per_div)
    if pair_note:
        snap["errors"].append(pair_note)

    c = db()
    ts = snap["ts"]
    tick_rows = [(name, typ, px, d["vol_div"].get(name))
                 for typ, d in ninja_cache.items()
                 for name, px in d["price_ex"].items()]
    n_ticks = store_ticks(c, ts, tick_rows) if store else 0
    metrics, hist_h = tick_metrics(c)

    price_map, trend_map = {}, {}
    for d in ninja_cache.values():
        price_map.update(d["price_ex"])
        trend_map.update(d["trend"])

    # ---- scanner -> action cards ----
    snap["chaos_ex"] = price_map.get("Chaos Orb")
    paper = bool(cfg.get("paper_mode"))
    port_pos = positions_raw(c, paper)
    rate_row = c.execute("SELECT payload FROM snapshots ORDER BY ts ASC LIMIT 1").fetchone()
    rate_start = None
    if rate_row:
        try:
            rate_start = json.loads(rate_row[0]).get("ex_per_div")
        except Exception:
            pass
    # bankroll: user-entered holdings win over the static start capital
    hold, ex_avail = holdings_get(c), None
    if hold and ex_per_div:
        cash0 = ((hold.get("ex") or 0) + (hold.get("div") or 0) * ex_per_div
                 + (hold.get("chaos") or 0) * (snap["chaos_ex"] or 0))
        net_spent = spent_since(c, paper, hold.get("ts") or "")
        liquid_now = cash0 - net_spent
        invested = sum(st["cost_ex"] for st in port_pos.values())
        bankroll_ex = liquid_now + invested
        ex_avail = max((hold.get("ex") or 0) - net_spent, 0)
    else:
        bankroll_ex = cfg["start_capital_div"] * (rate_start or ex_per_div or 0)
    scan_rows = scan_market(cfg, ninja_cache, metrics, hist_h, pair_info, ex_per_div)
    # league-long context: backfill daily history for top candidates + held
    # items, then score each candidate vs its 14-day mean (z-score)
    if store:
        held = [p[2:] for p in port_pos if p.startswith("c:")]
        try:
            daily_update(c, league, [r["name"] for r in scan_rows[:10]] + held)
        except Exception as e:
            snap["errors"].append(f"daily history: {e}")
    for row in scan_rows[:15]:
        s = stats14(c, row["name"])
        if s and s["sd14"]:
            z = (row["px"] - s["mean14"]) / s["sd14"]
            row["z14"] = round(z, 1)
            if row["sig"] == "DIP":
                if z > -0.5:
                    row["conf"] = "MED"  # not actually cheap vs league history
                elif z <= -1.5:
                    row["why"] += f" · cheap vs league history (z {z:+.1f})"
    snap["scan"] = [{k: (round(v, 2) if isinstance(v, float) else v) for k, v in r.items()}
                    for r in scan_rows[:15]]
    n_scanned = sum(len(d["price_ex"]) for d in ninja_cache.values())
    snap["scan_stats"] = {"scanned": n_scanned, "passed": len(scan_rows),
                          "markets": len(ninja_cache),
                          "hist_hours": round(hist_h, 1), "ticks_added": n_ticks}
    snap["cards"] = build_cards(cfg, scan_rows, port_pos, price_map, trend_map, bankroll_ex,
                                ex_per_div, ex_avail=ex_avail)
    # held scanner items must stay marked-to-market even when no card shows
    for pid in port_pos:
        if pid.startswith("c:") and pid[2:] in price_map:
            nm = pid[2:]
            snap["items"][pid] = {"name": nm, "price_ex": round(price_map[nm], 4),
                                  "trend7": trend_map.get(nm), "qty": None, "vol_div": None,
                                  "liq": None, "src": "scanner"}

    # ---- manual watchlist plays (unchanged behavior) ----
    for p in cfg["plays"]:
        kind, _, arg = p["source"].partition(":")
        try:
            rec = None
            if kind == "exchange":
                d = ninja(arg)
                pool |= set(d["price_ex"])
                nm = best_match(p["match"], d["price_ex"].keys())
                if nm:
                    rec = {"name": nm, "price_ex": d["price_ex"][nm], "trend7": d["trend"].get(nm),
                           "qty": None, "vol_div": d["vol_div"].get(nm), "src": f"poe.ninja {arg or 'Currency'}"}
            elif kind in ("unique", "currency"):
                key = (kind, arg)
                if key not in scout_cache:
                    scout_cache[key] = scout_by_category("Uniques" if kind == "unique" else "Currencies", arg, league)
                items = scout_cache[key]
                pool |= {nm for i in items for nm in (i.get("Name"), i.get("Text")) if nm}
                rec = match_scout(p["match"], items, f"poe2scout {kind}:{arg}")
            elif kind == "auto":
                rec = auto_lookup(p["match"], league, scout_cache, pool)
            else:
                snap["errors"].append(f"{p['id']}: unknown source kind '{kind}' "
                                      "(use exchange:<Type>, unique:<cat>, currency:<cat>, or auto)")
                continue
            if rec:
                snap["items"][p["id"]] = {
                    "name": rec["name"], "price_ex": round(rec["price_ex"], 4),
                    "trend7": rec["trend7"], "qty": rec["qty"],
                    "vol_div": round(rec["vol_div"], 1) if rec.get("vol_div") else None,
                    "liq": _liq(rec), "src": rec["src"]}
            else:
                snap["misses"][p["id"]] = miss_msg(p, pool, league)
        except Exception as e:
            snap["errors"].append(f"{p['id']}: {e}")

    if store:
        c.execute("INSERT INTO snapshots VALUES(?,?)", (snap["ts"], json.dumps(snap)))
        c.execute("DELETE FROM snapshots WHERE ts < datetime('now','-30 days')")
        c.commit()
    c.close()
    return snap

# --------------------------------------------------------------- analysis --
def latest_snapshots(c, n=400):
    rows = c.execute("SELECT payload FROM snapshots ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
    return [json.loads(r[0]) for r in rows]

def portfolio(c, cfg, snap, paper=False):
    fills = c.execute("SELECT ts,play_id,side,qty,price_ex,note FROM fills WHERE paper=? ORDER BY id",
                      (1 if paper else 0,)).fetchall()
    pos, spent_ex = {}, 0.0
    for ts, pid, side, qty, px, note in fills:
        st = pos.setdefault(pid, {"qty": 0.0, "cost_ex": 0.0, "realized_ex": 0.0, "last": ts})
        if side == "buy":
            st["qty"] += qty; st["cost_ex"] += qty * px; spent_ex += qty * px
        else:
            avg = st["cost_ex"] / st["qty"] if st["qty"] else 0
            st["realized_ex"] += qty * (px - avg)
            st["cost_ex"] -= qty * avg; st["qty"] -= qty; spent_ex -= qty * px
        st["last"] = ts
    rate = snap.get("ex_per_div") if snap else None
    first = None
    rows = latest_snapshots(c, 10000)
    for s in reversed(rows):
        if s.get("ex_per_div"):
            first = s["ex_per_div"]; break
    bankroll_ex0 = cfg["start_capital_div"] * (first or rate or 0)
    liquid_ex = bankroll_ex0 - spent_ex
    base_div = cfg["start_capital_div"]
    hold = holdings_get(c)
    if hold and rate:
        # user-entered liquid capital replaces the static start-capital model;
        # only fills logged after the entry subtract from it
        cash0 = ((hold.get("ex") or 0) + (hold.get("div") or 0) * rate
                 + (hold.get("chaos") or 0) * ((snap or {}).get("chaos_ex") or 0))
        liquid_ex = cash0 - spent_since(c, paper, hold.get("ts") or "")
        base_div = hold.get("base_div") or base_div
    pos_ex = 0.0
    for pid, st in pos.items():
        mkt = (snap or {}).get("items", {}).get(pid, {}).get("price_ex")
        st["mark_ex"] = (mkt or (st["cost_ex"]/st["qty"] if st["qty"] else 0))
        pos_ex += st["qty"] * st["mark_ex"]
    nw_div = (liquid_ex + pos_ex) / rate if rate else None
    return {"positions": pos, "liquid_ex": round(liquid_ex, 1), "positions_ex": round(pos_ex, 1),
            "networth_div": round(nw_div, 2) if nw_div else None, "base_div": round(base_div, 2),
            "rate_now": rate, "rate_start": first, "fills": len(fills)}

def signals(c, cfg, snap, port):
    out, ts = [], time.time()
    have = (snap or {}).get("items", {})
    for err in ((snap or {}).get("errors") or [])[:3]:
        out.append(("_err", "info", f"data warning: {err}"))
    for p in cfg["plays"]:
        d = have.get(p["id"]); pid = p["id"]
        st = port["positions"].get(pid, {"qty": 0})
        if not d:
            miss = (snap or {}).get("misses", {}).get(pid)
            out.append((pid, "info", f"{p['label']}: " + (miss or f"no market data matched '{p['match']}' — check match/source.")))
            continue
        px, tr = d["price_ex"], d.get("trend7")
        tag = f"{d['name']} @ {px:g} ex" + (f" ({tr:+.0f}% 7d)" if tr is not None else "")
        if d.get("liq"):
            tag += f" · {d['liq']}"
        if st["qty"] <= 0 and p["entry_max_ex"] and px <= p["entry_max_ex"]:
            out.append((pid, "entry", f"ENTRY {p['label']}: {tag} ≤ ceiling {p['entry_max_ex']} — deploy up to {p['budget_div']} div."))
            _mark(c, pid, "entry")
        elif st["qty"] > 0 and p["exit_target_ex"] and px >= p["exit_target_ex"]:
            since = _mark(c, pid, "exit")
            extra = ""
            if since and (ts - since) > cfg["no_fill_hours"] * 3600:
                extra = f" — exit live >{cfg['no_fill_hours']}h: reprice 5–10% under market."
            out.append((pid, "exit", f"EXIT {p['label']}: {tag} ≥ target {p['exit_target_ex']} — list and sell.{extra}"))
        elif st["qty"] > 0 and tr is not None and tr <= -abs(p["abandon_drop_pct"]):
            out.append((pid, "abandon", f"ABANDON {p['label']}: 7d {tr:+.0f}% breaches −{p['abandon_drop_pct']}% — liquidate at market."))
        else:
            out.append((pid, "hold", f"{p['label']}: {tag} — no trigger."))
    if port["rate_start"] and port["rate_now"]:
        drift = (port["rate_now"] - port["rate_start"]) / port["rate_start"] * 100
        if abs(drift) >= 5:
            out.append(("_rate", "info", f"div:ex drift {drift:+.0f}% since tracking start — re-check ex-priced targets."))
    return [{"play": a, "kind": b, "text": t} for a, b, t in out]

def _mark(c, pid, kind):
    row = c.execute("SELECT since FROM sigstate WHERE play_id=? AND kind=?", (pid, kind)).fetchone()
    if row:
        return datetime.fromisoformat(row[0]).timestamp()
    c.execute("INSERT OR REPLACE INTO sigstate VALUES(?,?,?)", (pid, kind, now_iso())); c.commit()
    return None

# ------------------------------------------------------------------- http --
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path == "/":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/api/state":
            cfg = load_config(); c = db()
            snaps = latest_snapshots(c, 200)
            snap = snaps[0] if snaps else None
            paper = bool(cfg.get("paper_mode"))
            port = portfolio(c, cfg, snap, paper=paper)
            real_port = port if not paper else portfolio(c, cfg, snap, paper=False)
            sigs = signals(c, cfg, snap, port)
            hist = [{"ts": s["ts"], "r": s["ex_per_div"]} for s in reversed(snaps) if s.get("ex_per_div")]
            fills_log = [{"id": i, "ts": t, "play_id": p, "side": s, "qty": q,
                          "price_ex": px, "note": n, "paper": pp}
                         for i, t, p, s, q, px, n, pp in c.execute(
                             "SELECT id,ts,play_id,side,qty,price_ex,note,paper FROM fills ORDER BY id DESC LIMIT 40")]
            hold = holdings_get(c)
            c.close()
            return self._send(200, json.dumps({"cfg": cfg, "snap": snap, "port": port,
                                               "real_port": real_port, "paper": paper,
                                               "signals": sigs, "rate_hist": hist,
                                               "fills_log": fills_log, "holdings": hold}))
        self._send(404, "{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        if self.path == "/api/fill":
            c = db()
            c.execute("INSERT INTO fills(ts,play_id,side,qty,price_ex,note,paper) VALUES(?,?,?,?,?,?,?)",
                      (now_iso(), body["play_id"], body["side"], float(body["qty"]),
                       float(body["price_ex"]), body.get("note", ""), 1 if body.get("paper") else 0))
            c.commit(); c.close()
            return self._send(200, '{"ok":true}')
        if self.path == "/api/fill_edit":
            c = db()
            c.execute("UPDATE fills SET side=?, qty=?, price_ex=?, note=?, paper=? WHERE id=?",
                      (body["side"], float(body["qty"]), float(body["price_ex"]),
                       body.get("note", ""), 1 if body.get("paper") else 0, int(body["id"])))
            c.commit(); c.close()
            return self._send(200, '{"ok":true}')
        if self.path == "/api/fill_delete":
            c = db()
            c.execute("DELETE FROM fills WHERE id=?", (int(body["id"]),))
            c.commit(); c.close()
            return self._send(200, '{"ok":true}')
        if self.path == "/api/holdings":
            c = db()
            snaps = latest_snapshots(c, 1)
            rate = snaps[0].get("ex_per_div") if snaps else None
            chaos_ex = snaps[0].get("chaos_ex") if snaps else None
            h = {"div": float(body.get("div") or 0), "ex": float(body.get("ex") or 0),
                 "chaos": float(body.get("chaos") or 0), "ts": now_iso()}
            h["base_div"] = round(h["div"] + (((h["ex"] + h["chaos"] * (chaos_ex or 0)) / rate) if rate else 0), 2)
            kv_set(c, "holdings", json.dumps(h)); c.commit(); c.close()
            return self._send(200, json.dumps({"ok": True, "holdings": h}))
        if self.path == "/api/refresh":
            snap = poll(load_config())
            return self._send(200, json.dumps({"ok": True, "errors": snap["errors"]}))
        self._send(404, "{}")

def poller_loop():
    while True:
        cfg = load_config()
        try:
            s = poll(cfg)
            st = s.get("scan_stats", {})
            print(f"[{s['ts']}] {s.get('league')} ex/div={s['ex_per_div']} "
                  f"scanned={st.get('scanned')} passed={st.get('passed')} cards={len(s['cards'])} "
                  f"ticks+={st.get('ticks_added')}"
                  + (f" errors={s['errors']}" if s["errors"] else ""))
        except Exception as e:
            print("poll failed:", e)
        time.sleep(max(2, int(cfg.get("poll_minutes", 5))) * 60)

def probe():
    cfg = load_config(); ok = True
    try:
        rows = scout_leagues()
        cur = ", ".join(l["Value"] for l in rows if l.get("IsCurrent")) or "?"
        print(f"PASS poe2scout /Leagues   {len(rows)} leagues, current: {cur}")
    except Exception as e:
        ok = False; print("FAIL poe2scout /Leagues:", e)
    info = resolve_league(cfg)
    league = info["name"]
    print(f"INFO league '{cfg['league']}' -> '{league}'" + (f"  [{info['note']}]" if info.get("note") else ""))

    ex_per_div = None
    try:
        d = parse_ninja(ninja_overview(league, "Currency"))
        ex_per_div = d["ex_per_div"]
        print(f"PASS poe.ninja Currency   ex/div = {ex_per_div:.1f}  ({len(d['price_ex'])} priced items)")
        if not ex_per_div: ok = False
    except Exception as e:
        ok = False; print("FAIL poe.ninja Currency:", e)

    print("INFO poe.ninja types:")
    for t in cfg.get("scan_types") or NINJA_TYPES:
        try:
            n = len(parse_ninja(ninja_overview(league, t))["price_ex"])
            print(f"     {t:20} {n} items")
        except Exception as e:
            print(f"     {t:20} ERR {e}")
        time.sleep(0.3)

    try:
        pairs, note = fetch_pair_info(league, ex_per_div)
        print(f"PASS poe2scout pairs      {len(pairs)} items with both ex+div routes"
              + (f"  [{note}]" if note else ""))
    except Exception as e:
        print("FAIL poe2scout pairs:", e)
    try:
        cats = get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/Items/Categories")
        u = " ".join(x["ApiId"] for x in cats.get("UniqueCategories", []))
        cc = " ".join(x["ApiId"] for x in cats.get("CurrencyCategories", []))
        print(f"PASS poe2scout categories  unique: {u}")
        print(f"                           currency: {cc}")
    except Exception as e:
        ok = False; print("FAIL poe2scout categories:", e)
    try:
        idx = scout_items_index(league)
        print(f"PASS poe2scout /Items     {len(idx)} items in index")
    except Exception as e:
        ok = False; print("FAIL poe2scout /Items:", e)

    print("INFO full dry-run poll (no store):")
    snap = poll(cfg, store=False)
    st = snap["scan_stats"]
    print(f"     scanned {st['scanned']} items, {st['passed']} passed gates, "
          f"{len(snap['cards'])} cards, history {st['hist_hours']}h")
    for card in snap["cards"]:
        print(f"     CARD [{card['act']}/{card['conf']}] {card['head']}")
    for p in cfg["plays"]:
        d = snap["items"].get(p["id"])
        if d:
            liq = f"  liq={d['liq']}" if d.get("liq") else ""
            tr = f"  7d={d['trend7']:+.0f}%" if d.get("trend7") is not None else ""
            print(f"     {p['id']:12} OK   {d['name']} @ {d['price_ex']:g} ex{tr}{liq}  [{d['src']}]")
        else:
            print(f"     {p['id']:12} MISS {snap['misses'].get(p['id'], 'no data')}")
    for e in snap["errors"]:
        print("     warn:", e)
    print("Probe", "OK — run: python quant.py" if ok else "had failures — see lines above.")

# -------------------------------------------------------------------- ui ---
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>QUANT · PoE2</title>
<link href="https://fonts.googleapis.com/css2?family=Cinzel:wght@500;700&family=IBM+Plex+Mono:wght@400;600&display=swap" rel="stylesheet">
<style>
:root{--bg:#161310;--panel:#1f1a15;--line:#3a322a;--ink:#e8e0d0;--dim:#998f7d;
--gold:#c9a86a;--up:#8aa86b;--warn:#c25e4c;--info:#6e93a8}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:14px/1.45 "IBM Plex Mono",ui-monospace,monospace;font-variant-numeric:tabular-nums}
header{display:flex;gap:16px;align-items:baseline;flex-wrap:wrap;padding:14px 20px;border-bottom:1px solid var(--line)}
h1{font-family:Cinzel,ui-serif,Georgia,serif;font-size:18px;letter-spacing:.18em;margin:0;color:var(--gold)}
.k{color:var(--dim);font-size:11px;text-transform:uppercase;letter-spacing:.12em}
.v{font-weight:600}.gold{color:var(--gold)}
.badge{border:1px solid var(--info);color:var(--info);font-size:10px;padding:2px 7px;letter-spacing:.15em}
#ribbon{position:relative;height:46px;margin:14px 20px;border:1px solid var(--line);background:var(--panel)}
#fillbar{position:absolute;top:0;bottom:0;left:0;background:linear-gradient(90deg,#4a3c22,var(--gold));opacity:.85}
#basetick{position:absolute;top:-5px;bottom:-5px;width:2px;background:var(--ink)}
#riblabel{position:absolute;inset:0;display:flex;align-items:center;justify-content:space-between;padding:0 12px;font-size:12px}
#dothis{margin:0 20px 14px}
.card{border:1px solid var(--line);border-left:4px solid var(--gold);background:var(--panel);padding:10px 14px;margin:8px 0}
.card.BUY,.card.DIP,.card.MAKE,.card.ROUTE{border-left-color:var(--up)}
.card.SELL{border-left-color:var(--gold)}.card.ABANDON{border-left-color:var(--warn)}
.card.HOLD,.card.CHECK{border-left-color:var(--line)}
.card .head{font-weight:600;font-size:15px}
.card .sub{margin-top:2px}
.card .why{margin-top:4px}
.card button{margin-top:8px;font-size:12px;padding:4px 10px}
.chip{display:inline-block;font-size:10px;padding:1px 7px;border:1px solid var(--dim);color:var(--dim);
letter-spacing:.12em;margin-left:8px;vertical-align:2px}
.chip.HIGH{border-color:var(--up);color:var(--up)}
#notrade{border:1px solid var(--line);background:var(--panel);padding:16px;text-align:center;color:var(--dim)}
main{display:grid;grid-template-columns:1.2fr .8fr;gap:14px;padding:0 20px 30px}
@media(max-width:860px){main{grid-template-columns:1fr}}
section{border:1px solid var(--line);background:var(--panel);padding:12px 14px}
h2{font-family:Cinzel,serif;font-size:12px;letter-spacing:.2em;color:var(--dim);margin:0 0 10px;font-weight:500}
.sig{border-left:3px solid var(--line);padding:6px 10px;margin:6px 0;background:rgba(0,0,0,.18)}
.sig.entry{border-color:var(--up)}.sig.exit{border-color:var(--gold)}
.sig.abandon{border-color:var(--warn)}.sig.info{border-color:var(--info)}
table{width:100%;border-collapse:collapse;font-size:13px}
td,th{padding:5px 6px;border-bottom:1px solid var(--line);text-align:right}
td:first-child,th:first-child{text-align:left}
.neg{color:var(--warn)}.pos{color:var(--up)}
button{background:none;border:1px solid var(--gold);color:var(--gold);font:inherit;
padding:6px 12px;cursor:pointer;letter-spacing:.06em}button:hover{background:rgba(201,168,106,.12)}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--gold);outline-offset:1px}
input,select{background:var(--bg);border:1px solid var(--line);color:var(--ink);font:inherit;padding:5px 7px;width:100%}
form{display:grid;grid-template-columns:1fr 1fr;gap:8px}
svg{display:block;width:100%;height:54px;margin-top:8px}
.stale{color:var(--warn)}
details summary{cursor:pointer;color:var(--dim);font-size:12px;letter-spacing:.1em}
#toast{position:fixed;right:18px;bottom:18px;background:var(--panel);border:1px solid var(--up);
color:var(--ink);padding:10px 14px;opacity:0;transition:opacity .3s;pointer-events:none;z-index:9;max-width:340px}
#toast.show{opacity:1}
.card.taken{opacity:.55;border-left-color:var(--dim)}
.card button:disabled{border-color:var(--up);color:var(--up);cursor:default}
#trades a{color:var(--info);text-decoration:none;margin-left:6px}
#capf{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
@media(prefers-reduced-motion:no-preference){.sig.entry,.sig.exit,.sig.abandon,.card{animation:in .5s ease}}
@keyframes in{from{opacity:0;transform:translateX(-4px)}to{opacity:1}}
</style></head><body>
<header><h1>QUANT</h1><span class="k" id="league"></span><span class="badge" id="mode" hidden>PAPER</span>
<span><span class="k">ex / div </span><span class="v gold" id="rate">—</span></span>
<span><span class="k">net worth </span><span class="v gold" id="nw">—</span><span class="k"> div</span></span>
<span><span class="k">last poll </span><span class="v" id="age">—</span></span>
<span style="margin-left:auto"><button id="refresh">Refresh prices now</button></span></header>
<div id="ribbon"><div id="fillbar"></div><div id="basetick"></div>
<div id="riblabel"><span class="k">vs holding <span id="base"></span> div</span><span class="v" id="delta"></span></div></div>
<div id="dothis"><h2 style="font-family:Cinzel,serif;font-size:12px;letter-spacing:.2em;color:var(--dim);margin:0 0 6px">DO THIS NOW</h2>
<div id="cards"></div></div>
<main>
<div><section><h2>Watchlist & warnings</h2><div id="sigs"></div></section>
<section style="margin-top:14px"><h2>Watchlist plays</h2><table id="plays"><thead>
<tr><th>Play</th><th>Price ex</th><th>7d</th><th>Liq</th><th>Entry≤</th><th>Exit≥</th><th>Qty</th><th>Avg</th></tr></thead><tbody></tbody></table>
<details style="margin-top:10px"><summary>Scanner — top candidates this poll</summary>
<table id="scan" style="margin-top:8px"><thead><tr><th>Item</th><th>Sig</th><th>Price ex</th><th>Edge %</th><th>Vol div/d</th><th>7d</th><th>Conf</th></tr></thead><tbody></tbody></table>
<div class="k" id="scanstats" style="margin-top:6px"></div></details></section></div>
<div><section><h2>Holdings</h2><table><tbody id="hold"></tbody></table>
<svg id="spark" aria-label="ex per div history"></svg><div class="k">ex/div history</div></section>
<section style="margin-top:14px"><h2>Capital</h2><form id="capf">
<input id="c_div" placeholder="divine" inputmode="decimal"><input id="c_ex" placeholder="exalted" inputmode="decimal"><input id="c_chaos" placeholder="chaos" inputmode="decimal">
<button type="button" id="c_go" style="grid-column:1/4">Set current holdings</button></form>
<p class="k" style="margin:8px 0 0">Enter the LIQUID currency you hold right now (not invested positions). Sizing, conversion advice and the ribbon baseline use this; fills logged after setting subtract from it.</p>
<div class="k" id="capnow" style="margin-top:6px"></div></section>
<section style="margin-top:14px"><h2>Log a fill</h2><form id="fill">
<select id="f_play"></select><select id="f_side"><option>buy</option><option>sell</option></select>
<input id="f_qty" placeholder="qty" inputmode="decimal"><input id="f_px" placeholder="price in ex" inputmode="decimal">
<input id="f_note" placeholder="note (optional)" style="grid-column:1/3">
<label style="grid-column:1/3;font-size:12px;color:var(--dim)"><input type="checkbox" id="f_paper" style="width:auto;margin-right:6px">paper fill (practice, not real)</label>
<button type="button" id="f_go" style="grid-column:1/3">Record fill</button>
<button type="button" id="f_cancel" style="grid-column:1/3" hidden>Cancel edit</button></form>
<p class="k" style="margin:10px 0 0">Fills are how Quant sees your trades — there is no API for your own orders. Read-only tool: all trading happens by hand in-game.</p>
</section>
<section style="margin-top:14px"><h2>Trades</h2><table id="trades"><thead>
<tr><th>When</th><th>Play</th><th>Side</th><th>Qty</th><th>Px ex</th><th></th><th></th></tr></thead><tbody></tbody></table></section>
</div></main>
<div id="toast"></div>
<script>
const $=s=>document.querySelector(s);let CFG,PAPER=false,EDIT=null,FILLS={};
const TAKEN=new Set();
function cls(n){return n>0?"pos":n<0?"neg":""}
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");
clearTimeout(t._h);t._h=setTimeout(()=>t.classList.remove("show"),2800)}
function ensureOpt(id){if(![...$("#f_play").options].some(o=>o.value===id)){
const o=document.createElement("option");o.value=o.textContent=id;$("#f_play").appendChild(o);}}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function load(){const r=await fetch("/api/state");const d=await r.json();CFG=d.cfg;PAPER=d.paper;
const lg=(d.snap&&d.snap.league)||d.cfg.league;
$("#league").textContent=lg;document.title="QUANT · "+lg;
$("#mode").hidden=!PAPER;$("#f_paper").checked=PAPER;
const rate=d.port.rate_now;$("#rate").textContent=rate?rate.toFixed(0):"—";
$("#nw").textContent=d.port.networth_div??"—";
if(d.snap){const mins=Math.round((Date.now()-Date.parse(d.snap.ts))/60000);
$("#age").textContent=mins+"m ago";$("#age").className=mins>Math.max(15,3*(d.cfg.poll_minutes||5))?"v stale":"v";}
const base=d.port.base_div??d.cfg.start_capital_div,nw=d.port.networth_div||base;
$("#base").textContent=base;
const max=Math.max(nw,base)*1.25;$("#fillbar").style.width=(nw/max*100)+"%";
$("#basetick").style.left=(base/max*100)+"%";
const dl=(nw-base);$("#delta").textContent=(dl>=0?"+":"")+dl.toFixed(2)+" div"+(PAPER?" (paper)":"");
$("#delta").className="v "+cls(dl);
const cards=(d.snap&&d.snap.cards)||[];
const stats=(d.snap&&d.snap.scan_stats)||{};
if(cards.length){
$("#cards").innerHTML=cards.map((c,i)=>{
const tk=TAKEN.has(c.pid+"|"+c.act);
const lbl=side=>tk?"✓ logged":(side==="sell"?(PAPER?"Sold it (paper)":"I sold — log fill"):(PAPER?"Take it (paper)":"I bought — log fill"));
const btn=(c.act==="SELL"||c.act==="ABANDON")
 ?`<button data-i="${i}" data-side="sell" ${tk?"disabled":""}>${lbl("sell")}</button>`
 :(c.act==="HOLD"||c.act==="CHECK")?""
 :`<button data-i="${i}" data-side="buy" ${tk?"disabled":""}>${lbl("buy")}</button>`;
return `<div class="card ${c.act}${tk?" taken":""}"><div class="head">${esc(c.head)}<span class="chip ${c.conf}">${c.conf}</span></div>
<div class="sub">${esc(c.sub)}</div><div class="why k">${esc(c.why)}</div>${btn}</div>`}).join("");
document.querySelectorAll("#cards button").forEach(b=>{b.onclick=async()=>{
const c=cards[+b.dataset.i];const side=b.dataset.side;
if(PAPER){b.disabled=true;b.textContent="logging…";
await fetch("/api/fill",{method:"POST",body:JSON.stringify({play_id:c.pid,side:side,qty:c.qty,price_ex:c.px,paper:true,note:"card "+c.act})});
TAKEN.add(c.pid+"|"+c.act);b.textContent="✓ logged";b.closest(".card").classList.add("taken");
toast(`${side==="sell"?"SELL":"BUY"} ${c.qty}× ${c.name} @ ${c.px} ex logged (paper)`);load();}
else{ensureOpt(c.pid);$("#f_play").value=c.pid;$("#f_side").value=side;$("#f_qty").value=c.qty;$("#f_px").value=c.px;$("#f_note").value="card "+c.act;
toast("Fill form prefilled — confirm your real in-game numbers, then Record fill");
window.scrollTo({top:document.body.scrollHeight,behavior:"smooth"});}}});
}else{
$("#cards").innerHTML=`<div id="notrade">NO TRADE — nothing passed the safety gates this poll
(${stats.scanned??"?"} items scanned, history ${stats.hist_hours??0}h). Sitting tight is the correct move.</div>`;}
$("#sigs").innerHTML=d.signals.map(s=>`<div class="sig ${s.kind}">${esc(s.text)}</div>`).join("")||"<span class=k>no data yet — refresh</span>";
const items=(d.snap&&d.snap.items)||{};
$("#plays tbody").innerHTML=d.cfg.plays.map(p=>{const m=items[p.id]||{};const st=d.port.positions[p.id]||{qty:0,cost_ex:0};
const avg=st.qty?(st.cost_ex/st.qty).toFixed(1):"—";
return `<tr><td title="${esc(m.name??"")}">${esc(p.label)}</td><td>${m.price_ex??"—"}</td>
<td class="${cls(m.trend7)}">${m.trend7!=null?m.trend7.toFixed(0)+"%":"—"}</td>
<td class="k">${m.liq??"—"}</td>
<td>${p.entry_max_ex||"—"}</td><td>${p.exit_target_ex||"—"}</td>
<td>${st.qty||0}</td><td>${avg}</td></tr>`}).join("");
const scan=(d.snap&&d.snap.scan)||[];
$("#scan tbody").innerHTML=scan.map(s=>`<tr><td>${esc(s.name)}</td><td>${s.sig}</td><td>${s.px}</td>
<td>${s.edge_net}</td><td>${Math.round(s.vol)}</td>
<td class="${cls(s.trend7)}">${s.trend7!=null?Math.round(s.trend7)+"%":"—"}</td><td>${s.conf}</td></tr>`).join("")
||"<tr><td colspan=7 class=k>nothing passed gates</td></tr>";
$("#scanstats").textContent=`scanned ${stats.scanned??"?"} items across ${stats.markets??"?"} markets (currency, fragments, essences, runes, …) · passed ${stats.passed??0} · intraday history ${stats.hist_hours??0}h (signals sharpen as it grows)`;
FILLS={};(d.fills_log||[]).forEach(f=>FILLS[f.id]=f);
$("#trades tbody").innerHTML=(d.fills_log||[]).map(f=>`<tr><td>${f.ts.slice(5,16).replace("T"," ")}</td>
<td title="${esc(f.play_id)}">${esc(f.play_id.replace(/^c:/,""))}</td><td>${f.side}</td><td>${f.qty}</td><td>${f.price_ex}</td>
<td class="k">${f.paper?"paper":""}</td><td><a href="#" data-e="${f.id}">edit</a><a href="#" data-x="${f.id}">del</a></td></tr>`).join("")
||"<tr><td colspan=7 class=k>no fills yet</td></tr>";
const H=d.holdings;
if(H){if(document.activeElement&&document.activeElement.form!==$("#capf")){$("#c_div").value=H.div||"";$("#c_ex").value=H.ex||"";$("#c_chaos").value=H.chaos||"";}
$("#capnow").textContent=`set ${H.ts.slice(0,16).replace("T"," ")} → ${H.div||0} div + ${H.ex||0} ex + ${H.chaos||0} chaos ≈ ${H.base_div} div baseline`;}
else $("#capnow").textContent=`not set — using start capital ${d.cfg.start_capital_div} div`;
$("#hold").innerHTML=`<tr><td>Liquid${PAPER?" (paper)":""}</td><td>${d.port.liquid_ex} ex</td></tr>
<tr><td>Positions</td><td>${d.port.positions_ex} ex</td></tr>
<tr><td>Fills logged</td><td>${d.port.fills}</td></tr>`
+(PAPER?`<tr><td class="k">real net worth</td><td class="k">${d.real_port.networth_div??"—"} div</td></tr>`:"");
const posIds=Object.keys(d.port.positions).filter(k=>d.port.positions[k].qty>0);
const ids=[...new Set([...d.cfg.plays.map(p=>p.id),...posIds])];
$("#f_play").innerHTML=ids.map(i=>`<option value="${esc(i)}">${esc(i)}</option>`).join("");
const h=d.rate_hist;if(h.length>1){const ys=h.map(p=>p.r);
const mn=Math.min(...ys),mx=Math.max(...ys)||1;
const pts=h.map((p,i)=>`${i/(h.length-1)*100},${54-((p.r-mn)/(mx-mn||1))*46-4}`).join(" ");
$("#spark").innerHTML=`<polyline points="${pts}" fill="none" stroke="#c9a86a" stroke-width="1.5" vector-effect="non-scaling-stroke"/>`;
$("#spark").setAttribute("viewBox","0 0 100 54");$("#spark").setAttribute("preserveAspectRatio","none");}}
$("#refresh").onclick=async()=>{$("#refresh").textContent="Polling…";
try{await fetch("/api/refresh",{method:"POST"});}finally{$("#refresh").textContent="Refresh prices now";}load();};
$("#f_go").onclick=async()=>{const b={play_id:$("#f_play").value,side:$("#f_side").value,
qty:$("#f_qty").value,price_ex:$("#f_px").value,note:$("#f_note").value,paper:$("#f_paper").checked};
if(!b.qty||!b.price_ex)return alert("qty and price are required");
if(EDIT){b.id=EDIT;await fetch("/api/fill_edit",{method:"POST",body:JSON.stringify(b)});toast("fill #"+EDIT+" updated");}
else{await fetch("/api/fill",{method:"POST",body:JSON.stringify(b)});toast("fill recorded");}
EDIT=null;$("#f_go").textContent="Record fill";$("#f_cancel").hidden=true;
$("#f_qty").value=$("#f_px").value=$("#f_note").value="";load();};
$("#f_cancel").onclick=()=>{EDIT=null;$("#f_go").textContent="Record fill";$("#f_cancel").hidden=true;
$("#f_qty").value=$("#f_px").value=$("#f_note").value="";};
$("#trades").onclick=async e=>{const ed=e.target.dataset.e,dx=e.target.dataset.x;
if(ed){e.preventDefault();const f=FILLS[ed];if(!f)return;EDIT=+ed;ensureOpt(f.play_id);
$("#f_play").value=f.play_id;$("#f_side").value=f.side;$("#f_qty").value=f.qty;$("#f_px").value=f.price_ex;
$("#f_note").value=f.note||"";$("#f_paper").checked=!!f.paper;
$("#f_go").textContent="Update fill #"+ed;$("#f_cancel").hidden=false;
toast("editing fill #"+ed+" — change values, then Update");}
if(dx){e.preventDefault();if(confirm("Delete fill #"+dx+"?")){
await fetch("/api/fill_delete",{method:"POST",body:JSON.stringify({id:+dx})});toast("fill #"+dx+" deleted");load();}}};
$("#c_go").onclick=async()=>{const b={div:$("#c_div").value||0,ex:$("#c_ex").value||0,chaos:$("#c_chaos").value||0};
const r=await fetch("/api/holdings",{method:"POST",body:JSON.stringify(b)});const j=await r.json();
toast(`holdings set ≈ ${j.holdings.base_div} div — sizing and baseline updated`);load();};
load();setInterval(load,60000);
</script></body></html>"""

# ------------------------------------------------------------------- main --
if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--probe" in sys.argv:
        probe(); sys.exit(0)
    if "--once" in sys.argv:
        print(json.dumps(poll(load_config()), indent=2)); sys.exit(0)
    load_config()
    threading.Thread(target=poller_loop, daemon=True).start()
    port = 8377
    print(f"QUANT running → http://localhost:{port}  (Ctrl+C to stop)")
    try: webbrowser.open(f"http://localhost:{port}")
    except Exception: pass
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
