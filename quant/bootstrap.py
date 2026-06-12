"""Bootstrap from league history (poe2scout DailyStatsHistory).

QUANT normally earns its calibration live over ~2 weeks. This shortcut mines
the daily averages since league start instead: a daily-scale walk-forward
re-runs the DIP logic over history (trailing robust anchor, idiosyncratic
z-score vs the market, knife gate) and MEASURES how often such dips actually
recovered and how much of the gap they closed. Those measurements replace the
guessed Beta/Normal priors, and the fetched history seeds the daily table so
the engine has a league anchor from its very first poll.

What this honestly cannot do: prove your limit orders would have filled —
daily listing medians carry no intraday path. So the bootstrap shortens the
CALIBRATION ramp, not the graduation bar; the 2-week paper run still validates
execution, and the priors here are capped at modest pseudo-counts so live
graded outcomes keep the final say.
"""
import math

from . import store
from .config import DB_PATH
from .models import daily_anchor, median
from .score import calib_default
from .util import best_match, clamp

PSEUDO_N_CAP = 30      # daily-scale evidence never outweighs ~30 live outcomes
HOLD_DAYS = 3          # mirrors the live 72h DIP horizon
WARMUP = 12            # trailing window for the anchor


def fetch_history(io, league, c, top_n=150, log=print):
    """Pull DailyStatsHistory for the top-N items by traded volume into the
    daily table. ~1 request per item, politely spaced."""
    vols = {}
    for typ in ("Currency", "Fragments", "Essences", "Runes", "Delirium", "Ritual"):
        try:
            d = io.ninja(league, typ)
            vols.update({nm: v for nm, v in d["vol_div"].items() if v})
            io.sleep(0.25)
        except Exception as e:
            log(f"  warn: ninja {typ}: {e}")
    try:
        idx = {i["Text"]: i["ItemId"] for i in io.items_index(league)
               if i.get("Text") and i.get("ItemId")}
    except Exception as e:
        log(f"  poe2scout item index unavailable ({e}) — nothing fetched")
        return 0
    ranked = sorted(vols, key=lambda nm: -vols[nm])
    picked = [(nm, idx.get(nm) or idx.get(best_match(nm, idx.keys()) or ""))
              for nm in ranked if nm not in ("Exalted Orb",)]
    picked = [(nm, iid) for nm, iid in picked if iid][:top_n]
    n = 0
    for i, (nm, iid) in enumerate(picked):
        try:
            d = io.daily_stats(league, iid)
            for ds in d.get("DailyStats") or []:
                if ds.get("Time") and ds.get("Average") is not None:
                    store.daily_upsert(c, nm, ds["Time"][:10], ds["Average"], ds.get("Volume"))
            n += 1
        except Exception:
            pass
        io.sleep(0.3)
        if (i + 1) % 25 == 0:
            log(f"  …{i + 1}/{len(picked)} items fetched")
    c.commit()
    return n


def walk_daily(daily_map, adv):
    """Walk-forward over daily history: detect dips the way the live signal
    would, then look 3 days ahead for the truth. No peeking inside a step."""
    series = {nm: [(d, a) for d, a, _ in rows]
              for nm, rows in daily_map.items() if len(rows) >= WARMUP + HOLD_DAYS + 2}
    # pass 1: per-item z path, so pass 2 can subtract the market median (idio)
    zpath = {}
    for nm, rows in series.items():
        for t in range(WARMUP, len(rows)):
            window = [a for _, a in rows[t - WARMUP:t]]
            anch = daily_anchor(window)
            if not anch or rows[t][1] <= 0:
                continue
            theta, sd = anch
            zpath[(nm, t)] = ((math.log(rows[t][1]) - theta) / sd, theta, sd, rows[t][0])
    mkt = {}
    for (nm, t), (z, _, _, date) in zpath.items():
        mkt.setdefault(date, []).append(z)
    mkt = {d: median(zs) for d, zs in mkt.items()}
    events = []
    for nm, rows in series.items():
        for t in range(WARMUP, len(rows) - HOLD_DAYS):
            got = zpath.get((nm, t))
            if not got:
                continue
            z, theta, sd, date = got
            idio = z - mkt.get(date, 0.0)
            slope3 = rows[t][1] / rows[t - 3][1] - 1 if rows[t - 3][1] else 0
            if z > -adv["dip_z"] or idio > -adv["idio_z"] or slope3 < -0.12:
                continue
            x0 = math.log(rows[t][1])
            target = theta - 0.385 * sd          # same quantile the live card uses
            if target <= x0 + 0.005:
                continue
            future = [math.log(a) for _, a in rows[t + 1:t + 1 + HOLD_DAYS] if a > 0]
            if not future:
                continue
            best = max(future)
            events.append({"item": nm, "date": date, "z": round(z, 2),
                           "hit": 1 if best >= target else 0,
                           "rev": clamp((best - x0) / (theta - x0), -1.0, 1.5)})
    hits = sum(e["hit"] for e in events)
    n = len(events)
    return {"events": n, "hits": hits,
            "hit_rate": round(hits / n, 3) if n else None,
            "rev_mean": round(sum(e["rev"] for e in events) / n, 3) if n else None,
            "items": len(series),
            "sample": sorted(events, key=lambda e: e["z"])[:5]}


def apply_priors(c, res, adv, log=print):
    """Measured history becomes the PRIOR. Live graded outcomes, if any exist,
    have already moved the posterior — never stomp on real evidence."""
    store.kv_set_json(c, "calib_boot", res)
    graded = c.execute("SELECT COUNT(*) FROM predictions WHERE outcome IS NOT NULL").fetchone()[0]
    if not res["events"]:
        log("  no qualifying dip events in history — priors unchanged")
        return False
    if graded:
        log(f"  live calibration already has {graded} graded outcomes — measured "
            "priors saved for reference (kv calib_boot), posteriors untouched")
        return False
    n0 = min(PSEUDO_N_CAP, max(res["events"] // 2, 4))
    cal = calib_default(adv)
    # +1/+1 Laplace smoothing: history is never allowed to claim certainty
    cal["DIP"]["hit"] = [round(res["hit_rate"] * n0 + 1, 2),
                         round((1 - res["hit_rate"]) * n0 + 1, 2)]
    cal["DIP"]["rev"] = [clamp(res["rev_mean"], 0.2, 1.2), 0.02, float(n0)]
    store.kv_set_json(c, "calib", cal)
    c.commit()
    log(f"  DIP priors set from history: hit {res['hit_rate']:.0%} "
        f"(pseudo-n {n0}, capped), reversion fraction {res['rev_mean']:.2f}")
    return True


def run(cfg, io=None, db_path=None, top_n=150, log=print):
    from .sources import LiveIO, resolve_league
    io = io or LiveIO()
    c = store.connect(str(db_path or DB_PATH))
    league = resolve_league(io, cfg)["name"]
    log(f"bootstrap: fetching league history for '{league}' (top {top_n} by volume)…")
    n = fetch_history(io, league, c, top_n, log)
    if n:
        log(f"  {n} items of daily history stored — the engine now has a league "
            "anchor from poll #1")
    res = walk_daily(store.daily_all(c), cfg["adv"])
    log(f"  walk-forward over history: {res['items']} items, {res['events']} dip "
        f"events, hit rate {res['hit_rate']}, mean reversion {res['rev_mean']}")
    applied = apply_priors(c, res, cfg["adv"], log)
    c.close()
    log("bootstrap done. NOT waived: the 2-week paper graduation — daily medians "
        "can't prove fills; the shadow book still validates execution forward.")
    return {"fetched": n, **res, "applied": applied}
