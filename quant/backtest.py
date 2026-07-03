"""Walk-forward replay of your own tick history through the live signal path.

No peeking: at each historical poll the signals see only data up to that
moment (filters and OU fits are rebuilt incrementally), entries rest as limit
orders and fill only on later trade-through, exits at target or timeout.
Baseline comparison: persistence (assume no edge — every entry closed at the
final observed price). A tweak that can't beat persistence here never earns
default-on live.

The sample is the DB's rolling window PLUS the full-resolution monthly archive
(`data_archive/ticks-*.csv`) — the archive is the only statistically
meaningful history the app owns. Fees are the real gold curve, not a flat
guess, and pair books are replayed so ROUTE (the class that burned us) is
finally testable.
"""
import copy
import csv
import itertools
import math
from collections import defaultdict
from datetime import date
from pathlib import Path

from . import store
from .config import DB_PATH
from .models import (fee_pct, fit_ou, kf_drift_z, kf_level, kf_new, kf_sd,
                     kf_sig_h, kf_step, median)
from .score import calib_default
from .signals import propose_all
from .util import hours_between


def load_ticks(db_path=None, archive_dir=None):
    """Every tick the app owns: the live DB window PLUS the full-resolution
    monthly archive CSVs, deduped on (ts,item,source) and sorted by ts.
    Synthetic __-prefixed items (e.g. __BASKET__) are skipped — they are advice
    rows, not tradables. The DB wins any (ts,item,source) tie."""
    seen = {}
    d = Path(archive_dir) if archive_dir else None
    if d and d.exists():
        for f in sorted(d.glob("ticks-*.csv")):
            with open(f, encoding="utf-8", newline="") as fh:
                r = csv.reader(fh)
                next(r, None)                        # header
                for row in r:
                    if len(row) < 5 or not row[1] or row[1].startswith("__"):
                        continue
                    ts, item, source = row[0], row[1], row[2]
                    try:
                        pxf = float(row[3])
                    except (ValueError, TypeError):
                        continue
                    try:
                        volf = float(row[4]) if row[4] not in ("", None) else None
                    except (ValueError, TypeError):
                        volf = None
                    seen[(ts, item, source)] = (pxf, volf)
    c = store.connect(str(db_path or DB_PATH))
    try:
        for ts, item, source, px, vol in c.execute(
                "SELECT ts, item, source, price_ex, vol_div FROM ticks"):
            if item and not str(item).startswith("__"):
                seen[(ts, item, source)] = (px, vol)  # DB wins ties
    finally:
        c.close()
    return sorted((ts, item, source, px, vol)
                  for (ts, item, source), (px, vol) in seen.items())


def _rt_fee(px, adv):
    """Round-trip cost in %: both sides of the real gold-fee curve at this trade
    value, plus one slippage haircut. Replaces the old flat -3."""
    return 2 * fee_pct(px, adv["fee_curve"]) + adv["slippage_pct"]


def _iso_week(ts):
    y, w, _ = date.fromisoformat(ts[:10]).isocalendar()
    return f"{y}-W{w:02d}"


def run(cfg, db_path=None, max_polls=None, quiet=False, archive_dir=None, ticks=None):
    adv = cfg["adv"]
    if archive_dir is None:
        archive_dir = Path(db_path or DB_PATH).parent / "data_archive"
    rows = ticks if ticks is not None else load_ticks(db_path, archive_dir)
    if not rows:
        if not quiet:
            print("backtest: no tick history yet — run the app for a while first")
        return None
    polls = defaultdict(dict)
    nvol = {}                       # latest ninja div/day per item
    for ts, item, source, px, vol in rows:
        polls[ts][(item, source)] = (px, vol)
        if source == "ninja" and vol:
            nvol[item] = vol
    stamps = sorted(polls)
    if max_polls:
        stamps = stamps[:max_polls]
    filters, bars, last_ts = {}, defaultdict(list), None
    pair_latest = {}                # item -> {"exalted":{...}, "divine":{...}}
    calib = calib_default(adv)
    open_orders, open_pos, closed = [], [], []
    warmup = stamps[0]
    for ts in stamps:
        obs_n = {}
        for (item, source), (px, vol) in polls[ts].items():
            if px is None or px <= 0:
                continue
            if source == "pairex":
                pair_latest.setdefault(item, {})["exalted"] = {
                    "px_ex": px, "trades": int(vol or 0), "value_ex": 0}
            elif source == "pairdiv":
                pair_latest.setdefault(item, {})["divine"] = {
                    "px_ex": px, "trades": int(vol or 0), "value_ex": 0}
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
        px_now = {item: px for (item, source), (px, vol) in polls[ts].items()
                  if source == "ninja"}
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
                               "ret": (p["target"] / p["px"] - 1) * 100 - _rt_fee(p["px"], adv)})
            elif hours_between(p["entry_ts"], ts) > min(p.get("H_h", adv["max_hold_h"]),
                                                        adv["max_hold_h"]):
                mark = now or p["px"]
                closed.append({**p, "filled": 1, "hit": 0,
                               "ret": (mark / p["px"] - 1) * 100 - _rt_fee(p["px"], adv)})
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
                           "vol_div": nvol.get(item, 0), "lvl": kf_level(st),
                           "lvl_ex": math.exp(kf_level(st)), "sd": kf_sd(st),
                           "drift_z": kf_drift_z(st), "sig_h": kf_sig_h(st), "ou": ou,
                           "m24": None, "sd24": None, "n24": 0, "dev": 0.0}
        devs = [r["lvl"] - r["ou"]["theta"] for r in mrows.values() if r["ou"]]
        fam = median(devs) if devs else 0.0
        for r in mrows.values():
            own = r["ou"]["sd_st"] if r["ou"] else max(r["sig_h"] * 5, 0.02)
            r["idio_z"] = ((r["lvl"] - (r["ou"]["theta"] if r["ou"] else r["lvl"])) - fam) / own
            r["fam_z"] = 0.0
        # replay the pair books as they stood this poll so ROUTE is testable
        routes = {item: pl for item, pl in pair_latest.items() if pl}
        held = {p["item"] for p in open_pos} | {o["item"] for o in open_orders}
        for prop in propose_all(mrows, routes, [], calib, adv,
                                adv["min_volume_div_day"])[:3]:
            if prop["item"] in held:
                continue
            open_orders.append({"item": prop["item"], "sig": prop["sig"],
                                "px": prop["entry_px"], "target": prop.get("target_px"),
                                "ts": ts, "p_hit": prop["p_hit"],
                                "H_h": prop.get("H_h", adv["max_hold_h"])})
            held.add(prop["item"])
    # persistence baseline: same entries, closed at final price, no model exits
    final_px = {item: px for (item, source), (px, vol) in polls[stamps[-1]].items()
                if source == "ninja"}
    base = [((final_px.get(t["item"], t["px"]) / t["px"] - 1) * 100 - _rt_fee(t["px"], adv))
            for t in closed if t.get("filled")]
    by_sig = defaultdict(list)
    for t in closed:
        if t.get("filled"):
            by_sig[t["sig"]].append(t)
    report = {"polls": len(stamps), "orders": len(closed),
              "filled": sum(1 for t in closed if t.get("filled")),
              "by_sig": {}, "orders_by_sig": {}}
    placed = defaultdict(lambda: [0, 0])   # sig -> [placed, filled]
    for t in closed:
        placed[t["sig"]][0] += 1
        if t.get("filled"):
            placed[t["sig"]][1] += 1
    report["orders_by_sig"] = {sig: {"placed": p, "filled": f}
                               for sig, (p, f) in sorted(placed.items())}
    for sig, ts_ in sorted(by_sig.items()):
        rets = [t["ret"] for t in ts_]
        hits = sum(1 for t in ts_ if t.get("hit"))
        report["by_sig"][sig] = {"n": len(ts_), "hit": round(hits / len(ts_), 2),
                                 "avg_ret_pct": round(sum(rets) / len(rets), 2)}
    filled = [t for t in closed if t.get("filled")]
    report["n_filled"] = len(filled)
    report["hit"] = round(sum(1 for t in filled if t.get("hit")) / len(filled), 2) if filled else None
    report["avg_ret_pct"] = round(sum(t["ret"] for t in filled) / len(filled), 2) if filled else None
    by_week = defaultdict(list)
    for t in filled:
        by_week[_iso_week(t["ts"])].append(t)
    report["by_week"] = {wk: {"n": len(ts_),
                              "hit": round(sum(1 for t in ts_ if t.get("hit")) / len(ts_), 2),
                              "avg_ret_pct": round(sum(t["ret"] for t in ts_) / len(ts_), 2)}
                         for wk, ts_ in sorted(by_week.items())}
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


def sweep(cfg, grid, db_path=None, archive_dir=None):
    """Grid-search adv knobs over the same history. grid: {adv_key: [values]}.
    Prints one row per combo (n / filled / hit / avg_ret / persistence) plus a
    per-ISO-week hit line — a combo that only wins in a single week is overfit,
    not a strategy. Ticks are loaded once and shared across every run."""
    if archive_dir is None:
        archive_dir = Path(db_path or DB_PATH).parent / "data_archive"
    ticks = load_ticks(db_path, archive_dir)
    if not ticks:
        print("sweep: no tick history yet — run the app for a while first")
        return []
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    print(f"parameter sweep — {len(combos)} combos over "
          f"{len({t[0] for t in ticks})} polls of shared history")
    print("  " + "  ".join(keys) + "   | n_filled  hit   avg_ret   persistence")
    reports = []
    for combo in combos:
        sub = copy.deepcopy(cfg)
        for k, v in zip(keys, combo):
            sub["adv"][k] = v
        rep = run(sub, db_path=db_path, quiet=True, ticks=ticks)
        reports.append((dict(zip(keys, combo)), rep))
        if rep is None:
            continue
        label = "  ".join(f"{v:>5}" for v in combo)
        weeks = rep.get("by_week", {})
        wk_str = " ".join(f"{wk.split('-W')[-1]}:{w['hit']}" for wk, w in weeks.items()) or "—"
        print(f"  {label}   | {rep['n_filled']:>7}  {rep['hit']}  "
              f"{rep['avg_ret_pct']}%   {rep['persistence_avg_pct']}%")
        print(f"      by week (hit): {wk_str}")
    print("prefer a combo that clears persistence AND holds up across weeks, "
          "not one that spikes in a single week")
    return reports


# default grid for --backtest-sweep: the three DIP entry knobs that most change
# how aggressively it dips in
DEFAULT_GRID = {"dip_z": [1.5, 1.8, 2.2], "idio_z": [0.7, 1.0, 1.3],
                "dip_p_aim": [0.6, 0.65, 0.7]}
