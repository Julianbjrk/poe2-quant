"""The poll pipeline. Pure-ish: all I/O goes through the injected `io` object
(live, fake, or replay) and the SQLite handle, so tests and backtests drive
the exact production path.

fetch → ticks → latent filters → market rows → proposals → gates+sizing →
card lifecycle → shadow book → predictions → exits → benchmarks → snapshot
"""
import json
import math
import threading
from pathlib import Path

from . import MODEL_V, store
from .models import (best_ratio, daily_anchor, fee_pct, fit_ou, kf_drift_z,
                     kf_level, kf_new, kf_sd, kf_sig_h, kf_step, median,
                     weighted_median)
from .score import (beta_mean, beta_sd, calib_apply, calib_default,
                    feature_reliability, fill_by_hour, graduation,
                    model_reliability, summarize, today, trust_line, update_gates)
from .util import (best_match, clamp, fmt_dur_h, fmt_ex, fmt_money, fmt_p,
                   fmt_pct, fmt_signed_ex, hours_between, now_iso)
from .signals import propose_all

_poll_lock = threading.Lock()
_tick_cache = {}  # db_path -> {(item, source): last_px}

PAIR_SRC = {"exalted": "pairex", "divine": "pairdiv", "chaos": "pairex"}
MAJORS = ("Exalted Orb", "Divine Orb", "Chaos Orb")


# ------------------------------------------------------------ portfolio ----
def portfolio(c, ledger, px_of, rate, base_div, rate0):
    pos = store.positions(c, ledger)
    pos_ex = 0.0
    for item, st in pos.items():
        st["mark_ex"] = px_of(item) or st["avg"]
        pos_ex += st["qty"] * st["mark_ex"]
    base_ex = (base_div or 0) * (rate0 or rate or 0)
    h = store.holdings(c)
    spent = store.net_spent_after(c, ledger, (h or {}).get("ts") if ledger == "real" else None)
    if ledger == "real" and h and rate:
        cash0 = (h.get("ex") or 0) + (h.get("div") or 0) * rate + (h.get("chaos") or 0) * 0
        liquid_ex = cash0 - spent
    else:
        liquid_ex = base_ex - store.net_spent_after(c, ledger, None)
    nw_div = (liquid_ex + pos_ex) / rate if rate else None
    return {"positions": pos, "liquid_ex": round(liquid_ex, 1),
            "positions_ex": round(pos_ex, 1),
            "nw_div": round(nw_div, 2) if nw_div is not None else None}


# --------------------------------------------------------------- sizing ----
def size_card(p, calib, adv, preset, bankroll_ex, deployable_ex, family_spent, rate):
    """Fractional Kelly on the conservative tail of the hit posterior, then the
    guardrails. Returns sizing dict or (None, reason)."""
    hit = calib.get(p["sig"], {}).get("hit", [6, 4])
    p_sd = beta_sd(hit)
    p_l = clamp(p["p_hit"] - p_sd, 0.02, 0.95)
    b = p["gain_pct"] / max(p["loss_pct"], 0.25)
    p_star = 1.0 / (1.0 + b)  # breakeven win prob
    from .util import Phi
    p_conf = Phi((p["p_hit"] - p_star) / max(p_sd, 1e-3))
    if p_conf < preset["p_edge_min"]:
        return None, (f"only {fmt_p(p_conf)} confident it beats break-even "
                      f"(needs {fmt_p(preset['p_edge_min'])})")
    kelly = max(0.0, p_l - (1 - p_l) / max(b, 0.1))
    f = preset["kelly_frac"] * min(kelly, 0.5)
    if f <= 0:
        return None, "edge too thin for any size once uncertainty is priced"
    vol_cap = (p["vol_div"] or 0) * (rate or 0) * adv["max_pos_pct_volume"] / 100
    fam_cap = max(bankroll_ex * 0.4 - family_spent.get(p["family"], 0.0), 0)
    spend_cap = min(f * bankroll_ex, vol_cap, deployable_ex, fam_cap)
    affordable = int(spend_cap // p["entry_px"])
    if affordable < 1:
        return None, "size after the caps is below one unit"
    ratio = best_ratio(p["entry_px"], "buy", max_lot=min(20, affordable))
    if not ratio:
        return None, "too cheap to express as a clean exchange ratio — tick size eats the edge"
    lot = ratio["get"]
    qty = int(spend_cap // ratio["unit"] // lot) * lot
    if qty < lot:
        return None, "size after the caps is below one exchange lot"
    spend = qty * ratio["unit"]
    profit = spend * p["ev_pct"] / 100
    if profit < adv["min_profit_ex"]:
        return None, (f"expected {fmt_ex(profit)} ex — under the {adv['min_profit_ex']} ex "
                      "floor; not worth the clicks")
    sell = best_ratio(p["target_px"], "sell") if p.get("target_px") else None
    return {"qty": qty, "spend_ex": round(spend, 1), "profit_ex": round(profit, 1),
            "ratio": ratio, "sell_ratio": sell, "kelly": round(kelly, 3),
            "size_note": (f"¼-Kelly at your bankroll → {fmt_money(f * bankroll_ex, rate)}; "
                          f"capped to {fmt_money(spend, rate)} by "
                          + ("daily-volume share" if spend_cap == vol_cap else
                             "family budget" if spend_cap == fam_cap else
                             "liquid reserve" if spend_cap == deployable_ex else "Kelly"))}, None


# ----------------------------------------------------------- card text -----
def card_text(card, rate):
    p, s = card["prop"], card["size"]
    r, sr = s["ratio"], s.get("sell_ratio")
    item = p["item"]
    if p["sig"] == "MAKE":
        head = (f"MAKE {item} — bid {s['qty']}×: set {r['give']} ex → {r['get']} "
                f"({fmt_ex(r['unit'])} ex each)")
        plan = (f"when filled, relist at {fmt_ex(p['target_px'])} ex"
                + (f" (set {sr['get']} ex ← {sr['give']})" if sr else "")
                + f" — full cycles work about {fmt_p(p['p_hit'])}; expected "
                + fmt_signed_ex(s["profit_ex"], rate) + " per cycle after est. fees")
    elif p["sig"] == "PARITY":
        head = f"CONVERT {item} — buy {s['qty']}×: set {r['give']} ex → {r['get']} ({fmt_ex(r['unit'])} ex each)"
        plan = (f"combine and sell the result — expected {fmt_signed_ex(s['profit_ex'], rate)}; "
                f"works about {fmt_p(p['p_hit'])}")
    else:
        head = (f"BUY {s['qty']}× {item} — set {r['give']} ex → {r['get']} "
                f"({fmt_ex(r['unit'])} ex each, ≈{fmt_money(s['spend_ex'], rate)} total)")
        plan = (f"then sell at {fmt_ex(p['target_px'])} ex"
                + (f" (set {sr['get']} ex ← {sr['give']})" if sr else "")
                + f" — works about {fmt_p(p['p_hit'])}; expected "
                + fmt_signed_ex(s["profit_ex"], rate)
                + f" after est. fees, usually done {fmt_dur_h(p['fill_h'] + p['H_h'] / 2)}")
    return head, plan


def exit_card(item, st, row, adv, rate):
    px = row["lvl_ex"] if row else None
    qty = st["qty"]
    if px is None:
        return {"id": f"EXIT:{item}", "act": "CHECK", "item": item, "qty": qty,
                "head": f"CHECK {item} — not in the scanner",
                "plan": f"holding {qty:g} at avg {fmt_ex(st['avg'])} ex — the scanner can't price this one",
                "why": "if you've sold it, log the sale to close this card; "
                       "if the buy was a mistake, void it under Record ▸",
                "px": None, "closeable": True}
    gain = (px - st["avg"]) / st["avg"] * 100 if st["avg"] else 0
    sd_pct = (row["ou"]["sd_st"] * 100) if row.get("ou") else 6.0
    target = st.get("target_px") or (
        math.exp(row["ou"]["theta"] - 0.25 * row["ou"]["sd_st"]) if row.get("ou")
        else st["avg"] * 1.06)
    fees = 2 * fee_pct(px * qty, adv["fee_curve"])
    if px >= target * 0.995:
        sr = best_ratio(max(target, px), "sell")
        return {"id": f"EXIT:{item}", "act": "SELL", "item": item, "qty": qty, "px": px,
                "target_px": round(max(target, px), 4),
                "head": f"SELL {qty:g}× {item} at {fmt_ex(max(target, px))} ex"
                        + (f" — set {sr['get']} ex ← {sr['give']}" if sr else ""),
                "plan": f"in at {fmt_ex(st['avg'])} ex → {fmt_pct(gain, signed=True)} — the thesis is done",
                "why": "exit anchors to the market's usual level, never to your cost"}
    abandon_px = st["avg"] * (1 - max(6.0, 2 * sd_pct) / 100)
    if px <= abandon_px and row["drift_z"] < -1.0:
        return {"id": f"EXIT:{item}", "act": "ABANDON", "item": item, "qty": qty, "px": px,
                "target_px": round(px, 4),
                "head": f"ABANDON {qty:g}× {item} — sell at market ({fmt_ex(px)} ex)",
                "plan": f"{fmt_pct(gain, signed=True)} vs entry and still falling "
                        f"(drop is {fmt_pct(max(6.0, 2 * sd_pct))}+, beyond its normal noise)",
                "why": "cut losers fast — thin-league dumps rarely bounce quickly"}
    done = clamp((px - st["avg"]) / (target - st["avg"]) * 100 if target != st["avg"] else 0, -200, 100)
    return {"id": f"EXIT:{item}", "act": "HOLD", "item": item, "qty": qty, "px": px,
            "target_px": round(target, 4), "closeable": True,
            "head": f"HOLD {qty:g}× {item} ({fmt_pct(gain, signed=True)}, now {fmt_ex(px)} ex)",
            "plan": f"exit at {fmt_ex(target)} ex — about {fmt_pct(done)} of the way there",
            "why": f"net of fees this needs {fmt_ex(target)} ex; no trigger yet"}


# ------------------------------------------------------ calibration load ---
def _load_calib_versioned(c, adv):
    """Load calibration; if the stored MODEL_V differs from the current one the
    graded-event definition changed, so old posteriors aren't comparable:
    archive them, re-init from priors (re-seeding DIP from the last bootstrap if
    present, bypassing its graded-count guard), and reset gates so every signal
    can earn its way back. Returns (calib, gates)."""
    stored = store.kv_get(c, "calib_model")
    calib = store.kv_json(c, "calib")
    if calib is not None and stored == MODEL_V:
        return calib, store.kv_json(c, "gates", {})
    if calib is not None:
        store.kv_set_json(c, "calib_archive:" + (stored or "unknown"), calib)
    calib = calib_default(adv)
    boot = store.kv_json(c, "calib_boot")
    if boot and boot.get("events") and boot.get("hit_rate") is not None:
        from .bootstrap import PSEUDO_N_CAP
        n0 = min(PSEUDO_N_CAP, max(boot["events"] // 2, 4))
        calib["DIP"]["hit"] = [round(boot["hit_rate"] * n0 + 1, 2),
                               round((1 - boot["hit_rate"]) * n0 + 1, 2)]
        rm = clamp(boot.get("rev_mean") or 0.7, 0.2, 1.2)
        calib["DIP"]["rev"] = [rm, 0.02, float(n0), 0.02 * float(n0)]
    store.kv_set_json(c, "calib", calib)
    store.kv_set(c, "calib_model", MODEL_V)
    store.kv_set_json(c, "gates", {})
    return calib, {}


# -------------------------------------------------------------- shadow -----
def shadow_process(c, shadow, ts, adv, calib):
    """Fill resting shadow orders on trade-through; close positions on target,
    abandon, or timeout; grade the prediction the moment the truth is known."""
    keep_o, opened = [], []
    items = {o["item"] for o in shadow["orders"]} | {p["item"] for p in shadow["pos"]}
    since = min([o["ts"] for o in shadow["orders"]]
                + [p["entry_ts"] for p in shadow["pos"]], default=ts)
    series = store.ticks_since(c, since, list(items)) if items else {}

    def crossed(item, since, level, side):
        for t, src, px in series.get(item, []):
            if t <= since or src != "ninja":
                continue
            if (side == "buy" and px <= level) or (side == "sell" and px >= level):
                return t
        return None

    def grade(pid, sig, out):
        # close the ledger row always; only feed posteriors graded under the
        # CURRENT forecast definition (skip stale-MODEL_V outcomes).
        pred = store.prediction_open(c, pid)
        if pred:
            store.predict_grade(c, pid, out, ts)
            if pred.get("model") == MODEL_V:
                calib_apply(calib, sig, pred, out)

    for o in shadow["orders"]:
        t_fill = crossed(o["item"], o["ts"], o["px"], "buy")
        if t_fill:
            opened.append({**o, "entry_ts": t_fill})
        elif hours_between(o["ts"], ts) > adv["fill_window_h"]:
            grade(o["pid"], o["sig"], {"filled": 0})
        else:
            keep_o.append(o)
    shadow["orders"] = keep_o
    keep_p = []
    for p in shadow["pos"] + opened:
        fees = 2 * fee_pct(p["px"] * p.get("qty", 1), adv["fee_curve"]) + adv["slippage_pct"]
        # the forecast horizon IS the evaluation horizon: a touch counts only
        # within H_h (max_hold_h is an absolute backstop), so the graded event
        # matches what the card's odds claim.
        hz = min(p.get("H_h", adv["max_hold_h"]), adv["max_hold_h"])
        t_hit = crossed(p["item"], p["entry_ts"], p["target"], "sell") if p.get("target") else None
        hit_in_window = bool(t_hit) and hours_between(p["entry_ts"], t_hit) <= hz
        timeout = hours_between(p["entry_ts"], ts) > hz
        if hit_in_window or timeout:
            # max favorable excursion within the horizon (gross) — the honest
            # reversion measurement, independent of the hit/timeout window.
            favs = [px for t, src, px in series.get(p["item"], [])
                    if src == "ninja" and t > p["entry_ts"]
                    and hours_between(p["entry_ts"], t) <= hz]
            mfe_pct = round((max(favs) / p["px"] - 1) * 100, 2) if favs else 0.0
            if hit_in_window:
                realized = (p["target"] / p["px"] - 1) * 100 - fees
                out = {"filled": 1, "hit": 1, "realized_pct": round(realized, 2),
                       "mfe_pct": mfe_pct, "t_hit_h": round(hours_between(p["entry_ts"], t_hit), 1)}
            else:
                last = [x for x in series.get(p["item"], []) if x[1] == "ninja"]
                mark = last[-1][2] if last else p["px"]
                realized = (mark / p["px"] - 1) * 100 - fees
                out = {"filled": 1, "hit": 0, "realized_pct": round(realized, 2), "mfe_pct": mfe_pct}
            grade(p["pid"], p["sig"], out)
        else:
            keep_p.append(p)
    shadow["pos"] = keep_p


# ------------------------------------------------- league-history feed -----
def _daily_backfill(c, io, league, names, budget=8):
    """Keep the daily table fresh for candidates/held items (≤budget requests
    per poll, once per item per day). --bootstrap does the bulk version."""
    if not hasattr(io, "items_index"):
        return
    idx = store.kv_json(c, "scout_idx", {})
    if not idx.get("map") or hours_between(idx.get("ts", "1970-01-01T00:00:00+00:00"),
                                           now_iso()) > 6:
        idx = {"ts": now_iso(), "map": {i["Text"]: i["ItemId"] for i in io.items_index(league)
                                        if i.get("Text") and i.get("ItemId")}}
        store.kv_set_json(c, "scout_idx", idx)
    fetched = 0
    for nm in dict.fromkeys(names):
        if store.kv_get(c, "daily:" + nm) == today():
            continue
        store.kv_set(c, "daily:" + nm, today())
        iid = idx["map"].get(nm)
        if not iid:
            continue
        try:
            d = io.daily_stats(league, iid)
        except Exception:
            continue
        for ds in d.get("DailyStats") or []:
            if ds.get("Time") and ds.get("Average") is not None:
                store.daily_upsert(c, nm, ds["Time"][:10], ds["Average"], ds.get("Volume"))
        fetched += 1
        if fetched >= budget:
            break
        io.sleep(0.25)


# ----------------------------------------------------------------- poll ----
def poll(cfg, io, db_path=None, store_snap=True):
    with _poll_lock:
        return _poll(cfg, io, db_path, store_snap)


def _poll(cfg, io, db_path, store_snap):
    from .config import DB_PATH
    from .sources import resolve_league
    adv, preset, mode = cfg["adv"], cfg["preset"], cfg["mode"]
    path = str(db_path or DB_PATH)
    c = store.connect(path)
    ts = io.now_iso() if hasattr(io, "now_iso") else now_iso()
    errors, snap = [], {"ts": ts, "mode": mode, "risk": cfg["risk"]}
    info = resolve_league(io, cfg)
    league = snap["league"] = info["name"]
    if info.get("note"):
        errors.append(info["note"])

    # ---- fetch --------------------------------------------------------
    data = {}
    for typ in adv["scan_types"]:
        try:
            data[typ] = io.ninja(league, typ)
            io.sleep(0.25)
        except Exception as e:
            errors.append(f"scan {typ}: {e}")
    cur = data.get("Currency") or {}
    rate = cur.get("ex_per_div") or info.get("divine_price")
    if not cur.get("ex_per_div") and rate:
        errors.append("ex/div taken from poe2scout (poe.ninja unavailable)")
    snap["ex_per_div"] = rate
    routes, pair_note = io.pairs(league, rate)
    if pair_note:
        errors.append(pair_note)

    px, vol, fam, tr7 = {}, {}, {}, {}
    for typ, d in data.items():
        for nm, p in d["price_ex"].items():
            px[nm], vol[nm], fam[nm] = p, d["vol_div"].get(nm) or 0.0, typ
            tr7[nm] = (d.get("trend") or {}).get(nm)   # ninja 7d % change (was discarded)
    snap["chaos_ex"] = px.get("Chaos Orb")
    # cross-source disagreement (free uncertainty signal): |ninja - pair exalted| / ninja
    src_gap = {}
    for nm, rts in routes.items():
        pe = (rts.get("exalted") or {}).get("px_ex")
        if pe and px.get(nm):
            src_gap[nm] = abs(px[nm] - pe) / px[nm] * 100

    # ---- ticks (per source) --------------------------------------------
    cache = _tick_cache.get(path)
    if cache is None:
        cache = _tick_cache[path] = store.load_last_cache(c)
    rows_t = [(nm, "ninja", p, vol.get(nm)) for nm, p in px.items()]
    for item, rts in routes.items():
        for major, d in rts.items():
            # vol slot carries TRADE COUNT for pair rows (div/day value for ninja
            # rows) — Task 3 replays pair books from this for ROUTE backtesting
            rows_t.append((item, PAIR_SRC.get(major, "pairex"), d["px_ex"], d["trades"]))
    n_ticks = store.insert_ticks(c, ts, rows_t, cache) if store_snap else 0

    # ---- latent filters -------------------------------------------------
    filters = store.kv_json(c, "filters", {})
    last_ts = store.kv_get(c, "filters_ts")
    dt = clamp(hours_between(last_ts, ts) if last_ts else adv["poll_minutes"] / 60,
               1e-3, 48.0)
    obs_by_item = {}
    for nm, p in px.items():
        obs_by_item.setdefault(nm, {})["ninja"] = math.log(p)
    for item, rts in routes.items():
        for major, d in rts.items():
            if d["px_ex"] > 0:
                obs_by_item.setdefault(item, {})[PAIR_SRC.get(major, "pairex")] = math.log(d["px_ex"])
    for item, obs in obs_by_item.items():
        st = filters.get(item)
        if st is None:
            filters[item] = st = kf_new(next(iter(obs.values())))
        kf_step(st, dt, obs)
    store.kv_set_json(c, "filters", filters)
    store.kv_set(c, "filters_ts", ts)

    # ---- OU refit (hourly) ----------------------------------------------
    ou_map = store.kv_json(c, "ou", {})
    if hours_between(store.kv_get(c, "ou_ts") or "1970-01-01T00:00:00+00:00", ts) >= 1.0:
        ou_map = {}
        for nm in px:
            fit = fit_ou(store.hourly_closes(c, nm))
            if fit:
                ou_map[nm] = fit
        store.kv_set_json(c, "ou", ou_map)
        store.kv_set(c, "ou_ts", ts)

    m24 = {r[0]: (r[1], math.sqrt(max(r[2] - r[1] * r[1], 0)), r[3]) for r in c.execute(
        "SELECT item, AVG(price_ex), AVG(price_ex*price_ex), COUNT(*) FROM ticks "
        "WHERE source='ninja' AND ts >= datetime('now','-1 day') GROUP BY item")}

    # ---- market rows + factor structure ---------------------------------
    # league-history anchor (daily table): live from poll #1 after --bootstrap
    d14_map = {}
    for nm, drows in store.daily_all(c).items():
        if len(drows) >= 10:
            anch = daily_anchor([a for _, a, _ in drows[-14:]])
            if anch:
                d14_map[nm] = {"theta": anch[0], "sd_st": anch[1],
                               "n": min(len(drows), 14)}
    rows = {}
    for nm, p in px.items():
        st = filters.get(nm)
        ou = ou_map.get(nm)
        d14 = d14_map.get(nm)
        mm = m24.get(nm)
        anchor = (ou["theta"] if ou else d14["theta"] if d14
                  else math.log(mm[0]) if mm and mm[0] > 0 else math.log(p))
        rows[nm] = {"item": nm, "family": fam[nm], "px": p, "vol_div": vol[nm],
                    "lvl": kf_level(st), "lvl_ex": math.exp(kf_level(st)),
                    "sd": kf_sd(st), "drift_z": kf_drift_z(st), "sig_h": kf_sig_h(st),
                    "ou": ou, "d14": d14, "m24": mm[0] if mm else None,
                    "sd24": mm[1] if mm else None, "n24": mm[2] if mm else 0,
                    "trend7": tr7.get(nm), "src_gap_pct": src_gap.get(nm),
                    "dev": kf_level(st) - anchor}
    devs = [(r["dev"], r["vol_div"]) for r in rows.values() if r["item"] not in MAJORS]
    mkt_dev = weighted_median(devs) if devs else 0.0
    sd_typ = (median([r["ou"]["sd_st"] for r in rows.values() if r["ou"]]
                     or [r["d14"]["sd_st"] for r in rows.values() if r["d14"]]) or 0.02)
    fam_dev = {}
    for f in set(fam.values()):
        fd = [r["dev"] for r in rows.values() if r["family"] == f and r["item"] not in MAJORS]
        fam_dev[f] = median(fd) if fd else 0.0
    for r in rows.values():
        own_sd = (r["ou"]["sd_st"] if r["ou"] else r["d14"]["sd_st"] if r["d14"]
                  else max(r["sig_h"] * 5, 0.02))
        r["idio_z"] = (r["dev"] - fam_dev.get(r["family"], 0.0)) / own_sd
        r["fam_z"] = fam_dev.get(r["family"], 0.0) / sd_typ
    market_z = mkt_dev / sd_typ
    circuit = abs(market_z) >= adv["circuit_z"]

    # ---- calibration + proposals ----------------------------------------
    calib, gates = _load_calib_versioned(c, adv)
    vol_floor = adv["min_volume_div_day"] * preset["vol_floor_x"]
    cand_rows = {nm: r for nm, r in rows.items() if nm not in MAJORS}
    props = propose_all(cand_rows, routes, adv["recipes"], calib, adv, vol_floor)

    # pins → PIN proposals + watch table
    pins_view = []
    for pin in cfg.get("pins") or []:
        nm = best_match(pin.get("match", ""), rows.keys())
        r = rows.get(nm) if nm else None
        pins_view.append({"label": pin.get("label") or pin.get("match"), "item": nm,
                          "px": round(r["px"], 4) if r else None,
                          "entry": pin.get("entry_max_ex") or None,
                          "exit": pin.get("exit_target_ex") or None})
        if r and pin.get("entry_max_ex") and r["px"] <= pin["entry_max_ex"]:
            tgt = pin.get("exit_target_ex") or r["px"] * 1.10
            gain = (tgt / r["px"] - 1) * 100 - 2 * fee_pct(r["px"], adv["fee_curve"])
            p_hit = beta_mean(calib["PIN"]["hit"])
            props.insert(0, {"sig": "PIN", "item": nm, "family": r["family"],
                             "entry_px": r["px"], "target_px": tgt, "p_fill": 0.9,
                             "fill_h": 1.0, "p_hit": p_hit, "p_model": p_hit, "H_h": 48.0,
                             "gain_pct": gain, "loss_pct": max(gain / 2, 3.0),
                             "ev_pct": p_hit * gain - (1 - p_hit) * max(gain / 2, 3.0),
                             "ret_mu": gain * p_hit, "ret_sd": max(gain, 4.0),
                             "vol_div": r["vol_div"],
                             "why": f"your pinned thesis '{pin.get('label')}' hit its entry ceiling",
                             "det": {"pin": pin.get("label")}, "deterministic": False})

    # ---- portfolio + benchmarks ------------------------------------------
    base = store.kv_json(c, "baselines", {})
    h = store.holdings(c)
    if rate and (not base or (h and h["ts"] > base.get("ts", ""))):
        members = sorted(cand_rows.values(), key=lambda r: -r["vol_div"])[:20]
        tot = sum(r["vol_div"] for r in members) or 1.0
        base = {"ts": ts, "rate0": rate,
                "real_div": (h and round((h.get("div") or 0) + ((h.get("ex") or 0)
                             + (h.get("chaos") or 0) * (snap["chaos_ex"] or 0)) / rate, 2)) or None,
                "paper_div": adv["paper_bankroll_div"],
                "index": {r["item"]: {"lvl": r["lvl"], "w": r["vol_div"] / tot} for r in members}}
        store.kv_set_json(c, "baselines", base)
    idx_now = 1.0
    if base.get("index"):
        s = sum(m["w"] * (rows[nm]["lvl"] - m["lvl"])
                for nm, m in base["index"].items() if nm in rows)
        idx_now = math.exp(s)

    def px_of(item):
        r = rows.get(item)
        return r["lvl_ex"] if r else None

    ports = {}
    for ledger in ("paper", "real"):
        bd = base.get("real_div") if ledger == "real" else base.get("paper_div", adv["paper_bankroll_div"])
        ports[ledger] = portfolio(c, ledger, px_of, rate, bd, base.get("rate0"))
        ports[ledger]["base_div"] = bd
    port = ports[mode]
    bench = {}
    if rate and base.get("rate0") and port["base_div"]:
        bd, r0 = port["base_div"], base["rate0"]
        bench = {"hold_div": bd, "hold_ex": round(bd * r0 / rate, 2),
                 "basket": round(bd * idx_now * r0 / rate, 2)}
    worst_bench = max(bench.values()) if bench else None  # hardest to beat
    deltas = ({k: round((port["nw_div"] or 0) - v, 2) for k, v in bench.items()}
              if bench and port["nw_div"] is not None else {})

    # ---- gates, sizing, card lifecycle -----------------------------------
    active = store.kv_json(c, "cards_active", [])
    pos_now = port["positions"]
    # resting paper orders are pending commitments: they count against both the
    # position cap and free capital, so cards can't pile up unfilled bids.
    orders_open = store.pending_orders(c, mode)
    n_orders = len(orders_open)
    orders_notional = sum(float(o.get("qty") or 0) * float(o.get("px") or 0) for o in orders_open)
    bankroll_ex = port["liquid_ex"] + port["positions_ex"]
    deployable = max(port["liquid_ex"] - bankroll_ex * adv["liquid_reserve_pct"] / 100
                     - orders_notional, 0)
    deployable0 = deployable
    family_spent = {}
    for item, st in pos_now.items():
        f = fam.get(item, "?")
        family_spent[f] = family_spent.get(f, 0.0) + st["cost_ex"]
    if store_snap:
        try:
            _daily_backfill(c, io, league, [p["item"] for p in props[:10]] + list(pos_now))
        except Exception as e:
            errors.append(f"daily history: {e}")

    kept, miss = [], None
    by_key = {(p["sig"], p["item"]): p for p in props}
    taken_ids = {e.get("card_id") for e in store.events(c, ["fill"]) if e.get("card_id")}
    for cardst in active:
        key = (cardst["prop"]["sig"], cardst["prop"]["item"])
        if cardst["id"] in taken_ids:
            store.append(c, "card_event", {"card_id": cardst["id"], "state": "TAKEN"}, ts)
            continue
        if hours_between(cardst["born"], ts) > adv["fill_window_h"]:
            store.append(c, "card_event", {"card_id": cardst["id"], "state": "EXPIRED",
                         "reason": "price never came to you — a missed trade costs nothing"}, ts)
            continue
        if key in by_key:
            p = by_key.pop(key)
            sized, why_not = size_card(p, calib, adv, preset, bankroll_ex, deployable,
                                       family_spent, rate)
            if sized:
                cardst.update(prop=p, size=sized)
                deployable -= sized["spend_ex"]
                family_spent[p["family"]] = family_spent.get(p["family"], 0.0) + sized["spend_ex"]
                kept.append(cardst)
                continue
        cardst["grace"] = cardst.get("grace", 2) - 1
        if cardst["grace"] > 0:  # hysteresis: marginal flicker doesn't kill a card
            kept.append(cardst)
        else:
            store.append(c, "card_event", {"card_id": cardst["id"], "state": "EXPIRED",
                                           "reason": "the edge faded before it filled"}, ts)
    # shadow book is loaded here so it can keep forecasting the top opportunities
    # regardless of whether the user has a free slot to act on them.
    shadow = store.kv_json(c, "shadow", {"orders": [], "pos": []})
    # track by (sig, item) so DIP and MAKE on the same item can both be graded
    shadow_tracked = ({(o["sig"], o["item"]) for o in shadow["orders"]}
                      | {(p["sig"], p["item"]) for p in shadow["pos"]}
                      | {(cs["prop"]["sig"], cs["prop"]["item"]) for cs in kept})
    shadow_room = max(0, adv["shadow_cap"] - len(shadow["orders"]) - len(shadow["pos"]))
    # per-signal quota so one loud signal can't monopolize the learning budget
    shadow_per_sig = {}
    for e in shadow["orders"] + shadow["pos"]:
        shadow_per_sig[e["sig"]] = shadow_per_sig.get(e["sig"], 0) + 1
    sig_quota = adv["shadow_cap"] // 5 + 1
    new_cards, shadow_new = [], []
    entries_off = circuit or not rate
    slots = max(0, min(adv["max_cards"], preset["max_positions"] - len(pos_now) - n_orders)
                - len(kept))
    fams_used = {cs["prop"]["family"] for cs in kept}
    for p in props:
        if (p["sig"], p["item"]) not in by_key or p["item"] in pos_now:
            continue
        gated = gates.get(p["sig"], {}).get("off")
        sized, why_not = size_card(p, calib, adv, preset, bankroll_ex, deployable,
                                   family_spent, rate)
        if sized and not gated and not entries_off and slots > 0 and p["family"] not in fams_used:
            cid = f"{p['sig']}:{p['item']}:{ts}"
            cs = {"id": cid, "born": ts, "prop": p, "size": sized, "grace": 2}
            new_cards.append(cs)
            store.append(c, "card_event", {"card_id": cid, "state": "ACTIVE"}, ts)
            deployable -= sized["spend_ex"]
            family_spent[p["family"]] = family_spent.get(p["family"], 0.0) + sized["spend_ex"]
            fams_used.add(p["family"])
            slots -= 1
            shadow_new.append(cs)
            shadow_tracked.add((p["sig"], p["item"]))
            shadow_per_sig[p["sig"]] = shadow_per_sig.get(p["sig"], 0) + 1
            shadow_room -= 1
        else:
            # forecast it in the shadow book anyway (slots full, capital tied up,
            # family used, or gated) so self-grading never stalls on user state —
            # bounded by a per-signal quota so no one signal starves the others.
            if ((p["sig"], p["item"]) not in shadow_tracked and shadow_room > 0
                    and shadow_per_sig.get(p["sig"], 0) < sig_quota):
                shadow_new.append({"id": f"{p['sig']}:{p['item']}:{ts}", "born": ts,
                                   "prop": p, "size": sized or {"qty": 1}, "shadow_only": True})
                shadow_tracked.add((p["sig"], p["item"]))
                shadow_per_sig[p["sig"]] = shadow_per_sig.get(p["sig"], 0) + 1
                shadow_room -= 1
            if miss is None and not gated and why_not:
                miss = {"item": p["item"], "sig": p["sig"], "reason": why_not}
    active = kept + new_cards
    store.kv_set_json(c, "cards_active", active)

    # ---- predictions + shadow book ---------------------------------------
    for cs in shadow_new:
        p = cs["prop"]
        pid = "p:" + cs["id"]
        store.predict_write(c, pid, cs["id"], p["sig"], p["item"], {
            "p_fill": round(p["p_fill"], 3), "fill_h": round(p["fill_h"], 1),
            "p_hit": round(p["p_hit"], 3), "p_model": round(p.get("p_model", p["p_hit"]), 3),
            "H_h": p["H_h"],
            "ret_mu": round(p["ret_mu"], 2), "ret_sd": round(p["ret_sd"], 2),
            "entry": p["entry_px"], "target": p.get("target_px"),
            "gap_pct": p.get("gap_pct"), "model": MODEL_V,
            "feat": {**p.get("det", {}),
                     "vol_div": round(p.get("vol_div") or 0),
                     "hour": int(ts[11:13])}}, ts)
        shadow["orders"].append({"pid": pid, "card_id": cs["id"], "sig": p["sig"],
                                 "item": p["item"], "px": p["entry_px"],
                                 "target": p.get("target_px"), "qty": cs["size"]["qty"],
                                 "H_h": p["H_h"], "ts": ts})
    if store_snap:
        shadow_process(c, shadow, ts, adv, calib)
    store.kv_set_json(c, "shadow", shadow)
    store.kv_set_json(c, "calib", calib)
    store.kv_set(c, "calib_model", MODEL_V)

    # ---- paper resting orders: fill on trade-through ----------------------
    if store_snap:
        for o in store.pending_orders(c, "paper"):
            series = store.ticks_since(c, o["ts"], [o["item"]]).get(o["item"], [])
            t_fill = next((t for t, src, v in series if src == "ninja"
                           and ((o["side"] == "buy" and v <= o["px"])
                                or (o["side"] == "sell" and v >= o["px"]))), None)
            if t_fill:
                store.append(c, "fill", {"ledger": "paper", "item": o["item"], "side": o["side"],
                                         "qty": o["qty"], "px": o["px"], "order_id": o["id"],
                                         "card_id": o.get("card_id"), "sig": o.get("sig"),
                                         "target_px": o.get("target_px"),
                                         "note": "paper order filled on trade-through"}, t_fill)

    # ---- grading-derived views (current MODEL_V only, never mix definitions)
    graded30 = store.predictions_graded(c, 30, model=MODEL_V)
    summary = summarize(graded30)
    reliability = model_reliability(graded30)
    feature_rel = feature_reliability(graded30)
    fill_hours = fill_by_hour(graded30)
    gates = update_gates(gates, summary, adv)
    store.kv_set_json(c, "gates", gates)

    # ---- exits -------------------------------------------------------------
    exit_cards = [exit_card(item, st, rows.get(item), adv, rate)
                  for item, st in pos_now.items()]

    # ---- snapshot -----------------------------------------------------------
    order = {"ABANDON": 0, "SELL": 1, "CHECK": 2}
    cards_ui = sorted((e for e in exit_cards if e["act"] != "HOLD"),
                      key=lambda e: order.get(e["act"], 3))
    holds = [e for e in exit_cards if e["act"] == "HOLD"]
    for cs in active:
        head, plan = card_text(cs, rate)
        p, s = cs["prop"], cs["size"]
        cards_ui.append({"id": cs["id"], "act": p["sig"], "item": p["item"],
                         "qty": s["qty"], "px": round(p["entry_px"], 4),
                         "target_px": round(p["target_px"], 4) if p.get("target_px") else None,
                         "head": head, "plan": plan, "why": p["why"],
                         "det": {**p["det"], "p_fill": round(p["p_fill"], 2),
                                 "fill_h": round(p["fill_h"], 1), "p_hit": round(p["p_hit"], 2),
                                 "ev_pct": round(p["ev_pct"], 2), "model": MODEL_V,
                                 "size": s["size_note"], "kelly": s["kelly"],
                                 "ratio_err_pct": s["ratio"]["err_pct"]},
                         "sig": p["sig"], "deterministic": p["deterministic"]})
    cards_ui += holds
    # always-on status: what the engine is doing right now, cards or not — so an
    # empty board is never a mystery (see point: "no way for me to tell")
    free_slots = max(0, preset["max_positions"] - len(pos_now) - n_orders)
    if not rate:
        entries_reason = "waiting for a price feed"
    elif circuit:
        entries_reason = "paused — the whole market is moving hard (circuit breaker)"
    elif free_slots <= 0:
        entries_reason = (f"all {preset['max_positions']} position slots are in use "
                          f"({len(pos_now)} held, {n_orders} resting)")
    elif deployable0 < 1:
        entries_reason = "no liquid capital free to deploy (set/raise holdings under Record)"
    elif miss:
        entries_reason = f"closest idea {miss['item']} — {miss['reason']}"
    else:
        entries_reason = None
    status = {"positions": len(pos_now), "orders": n_orders,
              "entry_cards": len(active), "scanned": len(rows),
              "slots_free": free_slots, "slots_total": preset["max_positions"],
              "deployable_ex": round(deployable0, 1),
              "deployable_div": round(deployable0 / rate, 2) if rate else None,
              "entries_reason": entries_reason}
    grad_pts = store.kv_json(c, "grad_points", [])
    if ports["paper"]["nw_div"] is not None and bench:
        bdp = ports["paper"]["base_div"]
        worst_p = max(bdp, bdp * base["rate0"] / rate, bdp * idx_now * base["rate0"] / rate)
        grad_pts = ([g for g in grad_pts if g["d"] != today()]
                    + [{"d": today(), "alpha": round(ports["paper"]["nw_div"] - worst_p, 3)}])[-90:]
        store.kv_set_json(c, "grad_points", grad_pts)
    snap.update({
        "errors": errors[:6], "circuit": circuit, "market_z": round(market_z, 2),
        "trust": trust_line(graded30, mode), "grad": graduation(grad_pts, adv, mode),
        "cards": cards_ui, "status": status,
        "no_trade": None if any(c["act"] not in ("HOLD",) for c in cards_ui) else {
            "checked": len(rows), "miss": miss,
            "line": "Nothing worth your divines right now."
                    + (f" {entries_reason.capitalize()}." if entries_reason else "")},
        "scan": [{k: p[k] for k in ("sig", "item", "ev_pct", "p_hit", "vol_div")}
                 | {"ev_pct": round(p["ev_pct"], 1), "p_hit": round(p["p_hit"], 2),
                    "vol_div": round(p["vol_div"] or 0)} for p in props[:12]],
        "gates": gates, "scoreboard": summary, "reliability": reliability,
        "feature_rel": feature_rel, "fill_hours": fill_hours,
        "port": {**{k: v for k, v in port.items() if k != "positions"},
                 "deltas": deltas, "bench": bench,
                 "positions": [{"item": i, **{k: round(v, 3) if isinstance(v, float) else v
                                              for k, v in st.items()}}
                               for i, st in pos_now.items()]},
        "other_nw": ports["real" if mode == "paper" else "paper"]["nw_div"],
        "pins": pins_view, "holdings": h,
        "stats": {"scanned": len(rows), "proposals": len(props), "ticks_added": n_ticks,
                  "shadow_open": len(shadow["orders"]) + len(shadow["pos"]),
                  "graded_30d": len(graded30), "index": round(idx_now, 4)},
    })
    if store_snap:
        store.kv_set_json(c, "last_snap", snap)
        # canonical item names for the trade-form autocomplete + server-side
        # snapping: everything the scanner prices, plus anything you hold/traded
        fill_items = {f["item"] for f in store.fills(c, "paper") + store.fills(c, "real")}
        names = sorted(set(px) | set(pos_now) | fill_items
                       | {p["item"] for p in pins_view if p.get("item")})
        store.kv_set_json(c, "item_names", names)
        store.snap_write(c, ts, {"ts": ts, "nw_div": port["nw_div"], "mode": mode,
                                 "ex_per_div": rate, "deltas": deltas})
        archive_dir = (Path(path).parent / "data_archive") if adv.get("archive_ticks", True) else None
        store.prune(c, adv["tick_keep_days"], adv["snap_keep_days"], archive_dir=archive_dir)
    c.commit()
    c.close()
    return snap
