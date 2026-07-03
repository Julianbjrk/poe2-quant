"""Data sources: poe.ninja PoE2 exchange + poe2scout. Read-only, polite.

Pure parse_* functions (tested against recorded fixtures) are separated from
LiveIO, which does the fetching. The engine only ever talks to an IO object,
so tests and backtests inject fakes and the pipeline itself stays pure.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import __version__
from .util import norm_name

NINJA_BASE = "https://poe.ninja/poe2/api/economy"
SCOUT_BASE = "https://poe2scout.com/api"
REALM = "poe2"
HEADERS = {"User-Agent": f"QuantDashboard/{__version__} (personal read-only price monitor)"}

NINJA_TYPES = ["Currency", "Fragments", "Essences", "Runes", "SoulCores",
               "LineageSupportGems", "Expedition", "Ritual", "Abyss",
               "Delirium", "UncutGems", "Idols"]
NINJA_TYPE_ALIASES = {t.lower(): t for t in NINJA_TYPES} | {
    "omens": "Ritual", "distilledemotions": "Delirium", "emotions": "Delirium",
    "soulcore": "SoulCores", "uncut": "UncutGems", "gems": "LineageSupportGems",
}


class SourceError(Exception):
    pass


def get_json(url, timeout=25, retries=1):
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except (urllib.error.URLError, TimeoutError) as e:
            code = getattr(e, "code", None)
            if attempt < retries and (code is None or code >= 500):
                time.sleep(2)
                continue
            raise


# ------------------------------------------------------------ parsers ------
def parse_ninja(raw):
    """-> {price_ex, trend, vol_div, ex_per_div}. Item names live in the
    top-level `items` list (core.items only holds the 3 core currencies)."""
    if not isinstance(raw, dict) or "lines" not in raw:
        raise SourceError("poe.ninja: unexpected shape (no 'lines') — API may have moved")
    core = raw.get("core") or {}
    names = {}
    for src in (raw.get("items") or []), (core.get("items") or []):
        for i in src:
            if i.get("id"):
                names[i["id"]] = i.get("name") or i["id"]
    prim, trend, volp = {}, {}, {}
    for ln in raw.get("lines") or []:
        nm = names.get(ln.get("id"), ln.get("id"))
        if ln.get("primaryValue") is None:
            continue
        prim[nm] = float(ln["primaryValue"])
        sp = ln.get("sparkline") or {}
        if sp.get("totalChange") is not None:
            trend[nm] = float(sp["totalChange"])
        if ln.get("volumePrimaryValue") is not None:
            volp[nm] = float(ln["volumePrimaryValue"])
    rates = core.get("rates") or {}
    ex_per_primary = rates.get("exalted") or (1 / prim["Exalted Orb"] if prim.get("Exalted Orb") else 1.0)
    price_ex = {k: v * ex_per_primary for k, v in prim.items()}
    ex_per_div = price_ex.get("Divine Orb")
    vol_div = {}
    if ex_per_div:
        vol_div = {k: v * ex_per_primary / ex_per_div for k, v in volp.items()}
    return {"price_ex": price_ex, "trend": trend, "vol_div": vol_div, "ex_per_div": ex_per_div}


def parse_pairs(raw, ex_per_div):
    """SnapshotPairs -> (routes, note). routes: item -> {major -> {px_ex, trades, value_ex}}
    where major ∈ {exalted, divine, chaos}. Self-validates the divine route
    against ninja's ex/div before the data is trusted."""
    routes, note = {}, None
    majors = ("exalted", "divine", "chaos")
    for p in raw or []:
        one, two = p.get("CurrencyOne") or {}, p.get("CurrencyTwo") or {}
        for side, sdata, other in ((one, p.get("CurrencyOneData"), two),
                                   (two, p.get("CurrencyTwoData"), one)):
            if other.get("ApiId") not in majors or not sdata:
                continue
            rp = float(sdata.get("RelativePrice") or 0)
            vt = int(sdata.get("VolumeTraded") or 0)
            val = float(sdata.get("ValueTraded") or 0)
            if rp > 0 and vt > 0 and side.get("Text"):
                routes.setdefault(side["Text"], {})[other["ApiId"]] = {
                    "px_ex": rp, "trades": vt, "value_ex": val}
    chk = (routes.get("Divine Orb") or {}).get("exalted", {}).get("px_ex")
    if not chk or not ex_per_div or abs(chk - ex_per_div) / ex_per_div > 0.15:
        note = (f"pair data failed sanity check (divine via pairs={chk}, "
                f"ninja={ex_per_div}) — route/parity signals off this poll")
        routes = {}
    return routes, note


def scout_trend(item):
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


def _lg(league):
    return urllib.parse.quote(league, safe="")


# ------------------------------------------------------------- live IO -----
class LiveIO:
    """All network + clock side effects behind one object."""

    def __init__(self):
        self._leagues = {"ts": 0.0, "rows": None}
        self._pairs = {"ts": 0.0, "routes": None, "note": None}

    def now(self):
        return time.time()

    def sleep(self, s):
        time.sleep(s)

    def ninja(self, league, typ):
        t = NINJA_TYPE_ALIASES.get(norm_name(typ).replace(" ", "").replace("-", ""), typ or "Currency")
        q = urllib.parse.urlencode({"league": league, "type": t})
        d = parse_ninja(get_json(f"{NINJA_BASE}/exchange/current/overview?{q}"))
        if not d["price_ex"]:
            raise SourceError(f"poe.ninja type '{t}' returned no data for {league}; "
                              f"known-good: {', '.join(NINJA_TYPES)}")
        return d

    def leagues(self, force=False):
        if not force and self._leagues["rows"] is not None and time.time() - self._leagues["ts"] < 6 * 3600:
            return self._leagues["rows"]
        rows = get_json(f"{SCOUT_BASE}/{REALM}/Leagues")
        self._leagues.update(ts=time.time(), rows=rows)
        return rows

    def pairs(self, league, ex_per_div):
        if self._pairs["routes"] is not None and time.time() - self._pairs["ts"] < 55 * 60:
            return self._pairs["routes"], self._pairs["note"]
        try:
            raw = get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/SnapshotPairs", timeout=40)
            routes, note = parse_pairs(raw, ex_per_div)
        except Exception as e:
            routes, note = {}, f"SnapshotPairs unavailable: {e}"
        self._pairs.update(ts=time.time(), routes=routes, note=note)
        return routes, note

    def items_index(self, league):
        return get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/Items")

    def by_category(self, kind, category, league, search="", max_pages=4):
        items, page, pages = [], 1, 1
        while page <= min(pages, max_pages):
            q = {"Category": category, "Page": page, "PerPage": 250, "ReferenceCurrency": "exalted"}
            if search:
                q["Search"] = search
            data = get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/{kind}/ByCategory?"
                            + urllib.parse.urlencode(q))
            pages = int(data.get("Pages") or 1)
            items += data.get("Items") or []
            page += 1
        return items

    def daily_stats(self, league, item_id, days=60):
        return get_json(f"{SCOUT_BASE}/{REALM}/Leagues/{_lg(league)}/Items/{item_id}/"
                        f"DailyStatsHistory?DayCount={days}")


def resolve_league(io, cfg):
    """-> {name, divine_price, note}. 'auto' picks the current softcore league."""
    want = str(cfg.get("league") or "auto").strip()
    try:
        rows = io.leagues()
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
