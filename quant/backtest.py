"""Walk-forward replay of your own tick history through the live signal path.

No peeking: at each historical poll the signals see only data up to that
moment (filters and OU fits are rebuilt incrementally), entries rest as limit
orders and fill only on later trade-through, exits at target or timeout.
Baseline comparison: persistence (assume no edge — every entry closed at the
final observed price). A tweak that can't beat persistence here never earns
default-on live.
"""
import math
from collections import defaultdict

from . import store
from .config import DB_PATH
from .models import fit_ou, kf_drift_z, kf_level, kf_new, kf_sd, kf_sig_h, kf_step, median
from .score import calib_default
from .signals import propose_all
from .util import hours_between


def run(cfg, db_path=None, max_polls=None, quiet=False):
    adv = cfg["adv"]
    c = store.connect(str(db_path or DB_PATH))
    rows = c.execute("SELECT ts, item, source, price_ex, vol_div FROM ticks ORDER BY ts").fetchall()
    c.close()
    if not rows:
        print("backtest: no tick history yet — run the app for a while first")
        return None
    polls = defaultdict(dict)
    vols = {}
    for ts, item, source, px, vol in rows:
        polls[ts][(item, source)] = px
        if vol:
            vols[item] = vol
    stamps = sorted(polls)
    if max_polls:
        stamps = stamps[:max_polls]
    filters, bars, last_ts = {}, defaultdict(list), None
    calib = calib_default(adv)
    open_orders, open_pos, closed = [], [], []
    warmup = stamps[0]
    for ts in stamps:
        obs_n = {}
        for (item, source), px in polls[ts].items():
            if px <= 0:
                continue
            st = filters.get(item)
            z = math.log(px)
            if st is None:
                filters[item] = st = kf_new(z)
            obs_n.setdefault(item, {})[source] = z
            if source == "ninja":
                hour = ts[:13]
                if not bars[item] or bars[item][-1][0] != hour:
                    bars[item].append([hour, px])
                else:
                    bars[item][-1][1] = px
        dt = hours_between(last_ts, ts) if last_ts else 0.1
        for item, obs in obs_n.items():
            kf_step(filters[item], max(dt, 1e-3), obs)
        last_ts = ts
        px_now = {item: px for (item, source), px in polls[ts].items() if source == "ninja"}
        # fills + exits on this poll's prices
        still = []
        for o in open_orders:
            p = px_now.get(o["item"])
            if p is not None and p <= o["px"]:
                open_pos.append({**o, "entry_ts": ts})
            elif hours_between(o["ts"], ts) <= adv["fill_window_h"]:
                still.append(o)
            else:
                closed.append({**o, "filled": 0, "ret": 0.0})
        open_orders = still
        still = []
        for p in open_pos:
            now = px_now.get(p["item"])
            if now is not None and p.get("target") and now >= p["target"]:
                closed.append({**p, "filled": 1, "hit": 1,
                               "ret": (p["target"] / p["px"] - 1) * 100 - 3})
            elif hours_between(p["entry_ts"], ts) > min(p.get("H_h", adv["max_hold_h"]),
                                                        adv["max_hold_h"]):
                mark = now or p["px"]
                closed.append({**p, "filled": 1, "hit": 0,
                               "ret": (mark / p["px"] - 1) * 100 - 3})
            else:
                still.append(p)
        open_pos = still
        if hours_between(warmup, ts) < 24:
            continue  # let filters and bars accumulate before trusting signals
        mrows = {}
        for item, px in px_now.items():
            st = filters[item]
            ou = fit_ou(bars[item][-24 * 14:]) if len(bars[item]) >= 8 else None
            mrows[item] = {"item": item, "family": "bt", "px": px,
                           "vol_div": vols.get(item, 0), "lvl": kf_level(st),
                           "lvl_ex": math.exp(kf_level(st)), "sd": kf_sd(st),
                           "drift_z": kf_drift_z(st), "sig_h": kf_sig_h(st), "ou": ou,
                           "m24": None, "sd24": None, "n24": 0, "dev": 0.0}
        devs = [r["lvl"] - r["ou"]["theta"] for r in mrows.values() if r["ou"]]
        fam = median(devs) if devs else 0.0
        for r in mrows.values():
            own = r["ou"]["sd_st"] if r["ou"] else max(r["sig_h"] * 5, 0.02)
            r["idio_z"] = ((r["lvl"] - (r["ou"]["theta"] if r["ou"] else r["lvl"])) - fam) / own
            r["fam_z"] = 0.0
        held = {p["item"] for p in open_pos} | {o["item"] for o in open_orders}
        for prop in propose_all(mrows, {}, [], calib, adv,
                                adv["min_volume_div_day"])[:3]:
            if prop["item"] in held:
                continue
            open_orders.append({"item": prop["item"], "sig": prop["sig"],
                                "px": prop["entry_px"], "target": prop.get("target_px"),
                                "ts": ts, "p_hit": prop["p_hit"],
                                "H_h": prop.get("H_h", adv["max_hold_h"])})
            held.add(prop["item"])
    # persistence baseline: same entries, closed at final price, no model exits
    final_px = {item: px for (item, source), px in polls[stamps[-1]].items() if source == "ninja"}
    base = [( (final_px.get(t["item"], t["px"]) / t["px"] - 1) * 100 - 3)
            for t in closed if t.get("filled")]
    by_sig = defaultdict(list)
    for t in closed:
        if t.get("filled"):
            by_sig[t["sig"]].append(t)
    report = {"polls": len(stamps), "orders": len(closed),
              "filled": sum(1 for t in closed if t.get("filled")), "by_sig": {}}
    for sig, ts_ in sorted(by_sig.items()):
        rets = [t["ret"] for t in ts_]
        hits = sum(1 for t in ts_ if t.get("hit"))
        report["by_sig"][sig] = {"n": len(ts_), "hit": round(hits / len(ts_), 2),
                                 "avg_ret_pct": round(sum(rets) / len(rets), 2)}
    report["persistence_avg_pct"] = round(sum(base) / len(base), 2) if base else None
    if not quiet:
        print(f"walk-forward over {report['polls']} polls — {report['orders']} orders, "
              f"{report['filled']} filled")
        for sig, s in report["by_sig"].items():
            print(f"  {sig:7} n={s['n']:<4} hit={s['hit']:<5} avg={s['avg_ret_pct']:+.2f}%")
        print(f"  persistence baseline (same entries, no exits): "
              f"{report['persistence_avg_pct']}%")
        print("ship a tweak only if it beats both the old config and persistence here, "
              "then let the live shadow book confirm")
    return report
