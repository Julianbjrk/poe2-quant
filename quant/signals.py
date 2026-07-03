"""Signal classes. Each produces Proposals; the engine gates, sizes and ranks.

A Proposal is a forecast, not advice yet:
  {sig, item, family, entry_px, target_px, p_fill, fill_h, p_hit, H_h,
   gain_pct, loss_pct, ev_pct, ret_mu, ret_sd, vol_div, why, det, deterministic}
gain/loss/ev are % of capital per cycle, AFTER estimated fees and slippage.
True-arbitrage signals (ROUTE, PARITY) carry no price-model risk, only
execution risk — they outrank statistical signals by construction.
"""
import math

from .models import (below_mean, fee_pct, ou_horizon, prob_ge, touch_median_h,
                     touch_prob)
from .score import beta_mean, beta_sd
from .util import Phi_inv, clamp, fmt_ex, fmt_pct


def _fees_rt(px, qty, adv, legs=2):
    return legs * fee_pct(px * max(qty, 1), adv["fee_curve"]) + adv["slippage_pct"]


def _ret_sd(p, gain, loss):
    return math.sqrt(max(p * (1 - p), 0.05)) * (gain + loss)


def dip(row, calib, adv):
    ou = row.get("ou")
    fees = _fees_rt(row["lvl_ex"], 1, adv)
    p_model = None   # the model's own probability, kept as a reliability diagnostic
    if ou and ou["n"] >= 24:
        z = (row["lvl"] - ou["theta"]) / ou["sd_st"]
        if z > -adv["dip_z"] or row["idio_z"] > -adv["idio_z"]:
            return None
        if row["drift_z"] <= adv["knife_drift_z"] or row["fam_z"] <= adv["family_z_floor"]:
            return None
        rev = calib["DIP"].get("rev", [0.7, 0.02, 12])[0]
        kappa = -math.log(ou["b"])
        H = clamp(3.0 / max(kappa, 1e-3), 6.0, float(adv["horizon_h"]["DIP"]))
        entry = math.exp(row["lvl"] - 0.10 * ou["sd_st"])     # rest slightly under latent
        mu_H, sd_H = ou_horizon(math.log(entry), ou, H, rev_frac=rev)
        # target = the price the model gives ≥ p_aim odds of reaching by H,
        # capped under the mean (the thesis never promises more than reversion)
        t_ln = min(mu_H + Phi_inv(1 - adv["dip_p_aim"]) * sd_H,
                   ou["theta"] - 0.10 * ou["sd_st"])
        target = math.exp(t_ln)
        if target <= entry * 1.005:
            return None
        # show/size on the empirically-calibrated rate (graded as first-passage
        # touch within H, so the odds match reality by construction); keep the
        # model's own marginal as the p_model diagnostic.
        p_hit = clamp(beta_mean(calib["DIP"]["hit"]), 0.05, 0.92)
        p_model = clamp(prob_ge(mu_H, sd_H, t_ln), 0.05, 0.92)
        miss_px = math.exp(below_mean(mu_H, sd_H, t_ln))
        gain = (target / entry - 1) * 100 - fees
        loss = max((1 - miss_px / entry) * 100 + fees, 0.5)
        dist = 0.10 * ou["sd_st"]
        why = (f"{fmt_pct((1 - entry / math.exp(ou['theta'])) * 100)} under its usual level — "
               "its own dip, not a family-wide one, and not in freefall")
        det = {"z_ou": round(z, 2), "idio_z": round(row["idio_z"], 2),
               "drift_z": round(row["drift_z"], 2), "ou_n": ou["n"],
               "theta_ex": round(math.exp(ou["theta"]), 4), "sd_st_pct": round(ou["sd_st"] * 100, 2),
               "rev_frac": round(rev, 2), "kappa_h": round(kappa, 3), "fees_pct": round(fees, 2)}
        gap_pct = (math.exp(ou["theta"]) / entry - 1) * 100
    elif row.get("d14") and row["d14"]["n"] >= 10:
        # league-history anchor: live before intraday models warm up, and its
        # hit prior is MEASURED by --bootstrap's walk over the same history
        d = row["d14"]
        z = (row["lvl"] - d["theta"]) / d["sd_st"]
        if z > -adv["dip_z"] or row["idio_z"] > -adv["idio_z"]:
            return None
        if row["drift_z"] <= adv["knife_drift_z"] or row["fam_z"] <= adv["family_z_floor"]:
            return None
        entry = math.exp(row["lvl"] - 0.02 * d["sd_st"])
        t_ln = d["theta"] - 0.385 * d["sd_st"]   # same quantile the bootstrap graded
        target = math.exp(t_ln)
        if target <= entry * 1.005:
            return None
        p_hit = clamp(beta_mean(calib["DIP"]["hit"]), 0.35, 0.75)
        H = 72.0
        gain = (target / entry - 1) * 100 - fees
        loss = max(d["sd_st"] * 80, 1.0) + fees / 2
        dist = 0.02 * d["sd_st"]
        why = (f"{fmt_pct((1 - entry / math.exp(d['theta'])) * 100)} under its league-history "
               f"norm ({d['n']} days) — intraday model still warming up")
        det = {"z_daily": round(z, 2), "idio_z": round(row["idio_z"], 2),
               "days": d["n"], "fees_pct": round(fees, 2)}
        gap_pct = (math.exp(d["theta"]) / entry - 1) * 100
    elif row.get("n24", 0) >= 6 and row.get("sd24"):
        z24 = (row["px"] - row["m24"]) / row["sd24"]
        if z24 > -2.2 or row["idio_z"] > -adv["idio_z"] or row["drift_z"] <= adv["knife_drift_z"]:
            return None
        entry, target = row["px"], row["m24"]
        if target <= entry:
            return None
        H = float(adv["horizon_h"]["DIP"])
        p_hit = clamp(0.45 + 0.04 * (-z24 - 2.2), 0.40, 0.58)  # young history: stay humble
        gain = (target / entry - 1) * 100 - fees
        loss = max(row["sd24"] / entry * 100 + fees, 1.0)
        dist = 0.0
        why = "well under its 24h average — early-history read, sized down by lower confidence"
        det = {"z24": round(z24, 2), "n24": row["n24"], "fees_pct": round(fees, 2)}
        gap_pct = (target / entry - 1) * 100
    else:
        return None
    det["trend7"] = row.get("trend7")          # diagnostic only — never sizes
    det["src_gap_pct"] = row.get("src_gap_pct")
    if p_model is None:
        p_model = p_hit
    ev = p_hit * gain - (1 - p_hit) * loss
    return {"sig": "DIP", "item": row["item"], "family": row["family"],
            "entry_px": entry, "target_px": target,
            "p_fill": touch_prob(dist + 1e-4, row["sig_h"], adv["fill_window_h"]),
            "fill_h": max(touch_median_h(dist + 1e-4, row["sig_h"]), 0.3),
            "p_hit": p_hit, "p_model": p_model, "H_h": H, "gain_pct": gain,
            "loss_pct": loss, "ev_pct": ev,
            "ret_mu": ev, "ret_sd": _ret_sd(p_hit, gain, loss), "gap_pct": gap_pct,
            "vol_div": row["vol_div"], "why": why, "det": det, "deterministic": False}


def make(row, calib, adv):
    if row["vol_div"] < adv["make_min_volume"]:
        return None
    spread = calib["MAKE"].get("spread", [6.0, 4.0, 12.0])[0]
    sd_day_pct = row["sig_h"] * math.sqrt(24) * 100
    spread = max(spread, 1.5 * sd_day_pct / 4)  # never quote tighter than the noise
    fees = _fees_rt(row["lvl_ex"], 1, adv)
    p_cycle = beta_mean(calib["MAKE"]["hit"])
    bid = row["lvl_ex"] * (1 - spread / 200)
    ask = row["lvl_ex"] * (1 + spread / 200)
    gain = spread - fees
    loss = max(0.5 * sd_day_pct + fees / 2, 1.0)  # one-leg inventory risk
    ev = p_cycle * gain - (1 - p_cycle) * loss
    dist = spread / 200
    return {"sig": "MAKE", "item": row["item"], "family": row["family"],
            "entry_px": bid, "target_px": ask,
            "p_fill": touch_prob(dist, row["sig_h"], adv["fill_window_h"] * 2),
            "fill_h": touch_median_h(dist, row["sig_h"]),
            "p_hit": p_cycle, "p_model": p_cycle, "H_h": float(adv["horizon_h"]["MAKE"]),
            "gain_pct": gain, "loss_pct": loss, "ev_pct": ev,
            "ret_mu": ev, "ret_sd": _ret_sd(p_cycle, gain, loss),
            "vol_div": row["vol_div"],
            "why": f"deep book ({row['vol_div']:,.0f} div/d) — work both sides of the spread patiently",
            "det": {"spread_pct": round(spread, 2), "p_cycle": round(p_cycle, 2),
                    "fees_pct": round(fees, 2), "day_sd_pct": round(sd_day_pct, 2),
                    "trend7": row.get("trend7"), "src_gap_pct": row.get("src_gap_pct")},
            "deterministic": False}


MAJOR_LABEL = {"exalted": "exalted book", "divine": "divine book", "chaos": "chaos book"}


def route(item, rts, row, calib, adv):
    """Cross-book divergence: the same item at different effective ex prices via
    its exalted/divine/chaos pairs. Generalized cycle search over the majors —
    every ordered route pair is a candidate 3-hop cycle."""
    legs = [(m, d) for m, d in rts.items() if d["trades"] >= adv["route_min_trades"]]
    if len(legs) < 2:
        return None
    # Anchor on the EXALTED book (the base unit, always the reliable price),
    # falling back to ninja's mid; drop any book that disagrees with the anchor
    # beyond the band. This now runs for EVERY item — the old guard only ran when
    # ninja priced the item, so poe2scout-only items (alloys/catalysts/ores)
    # bypassed it and lot-size distortion in the coarse divine/chaos books became
    # phantom "+273%" routes that never filled.
    anchor = (rts.get("exalted") or {}).get("px_ex") or (row["px"] if row else None)
    if not anchor:
        return None
    band = adv["route_band_pct"] / 100
    legs = [(m, d) for m, d in legs if abs(d["px_ex"] - anchor) / anchor <= band]
    if len(legs) < 2:
        return None
    cheap = min(legs, key=lambda x: x[1]["px_ex"])
    rich = max(legs, key=lambda x: x[1]["px_ex"])
    dev = (rich[1]["px_ex"] - cheap[1]["px_ex"]) / cheap[1]["px_ex"] * 100
    if dev < adv["route_min_edge_pct"] or dev > adv["route_max_dev_pct"]:
        return None   # below the floor to bother, or a too-good-to-be-true distorted gap
    hops = 2 + (cheap[0] != "exalted") + (rich[0] != "exalted")
    fees = hops * fee_pct(cheap[1]["px_ex"], adv["fee_curve"]) + 2.0  # +2% stale-pair buffer
    gain = dev - fees
    if gain <= 0:
        return None
    p_hit = beta_mean(calib["ROUTE"]["hit"])
    loss = fees + 1.0
    ev = p_hit * gain - (1 - p_hit) * loss
    sig_h = row["sig_h"] if row else 0.01
    return {"sig": "ROUTE", "item": item, "family": (row or {}).get("family", "route"),
            "entry_px": cheap[1]["px_ex"], "target_px": rich[1]["px_ex"],
            "p_fill": touch_prob(1e-4, sig_h, adv["fill_window_h"]), "fill_h": 1.0,
            "p_hit": p_hit, "p_model": p_hit, "H_h": float(adv["horizon_h"]["ROUTE"]),
            "gain_pct": gain, "loss_pct": loss, "ev_pct": ev,
            "ret_mu": ev, "ret_sd": _ret_sd(p_hit, gain, loss),
            "vol_div": (row or {}).get("vol_div") or min(cheap[1]["value_ex"], rich[1]["value_ex"]) / 12,
            "why": (f"{fmt_ex(cheap[1]['px_ex'])} ex via the {MAJOR_LABEL[cheap[0]]} vs "
                    f"{fmt_ex(rich[1]['px_ex'])} ex via the {MAJOR_LABEL[rich[0]]} "
                    f"({fmt_pct(dev, signed=True)}) — buy one book, sell the other; "
                    "verify both books in-game first, pair data is up to an hour old"),
            "det": {"buy_via": cheap[0], "sell_via": rich[0], "dev_pct": round(dev, 1),
                    "hops": hops, "fees_pct": round(fees, 2),
                    "trades": min(cheap[1]["trades"], rich[1]["trades"]),
                    "trend7": (row or {}).get("trend7"), "src_gap_pct": (row or {}).get("src_gap_pct")},
            "deterministic": True}


def parity(recipes, px_map, vol_map, calib, adv):
    """Deterministic conversions: when the exchange prices violate a recipe
    identity beyond fees, the edge has no price risk — only execution risk."""
    out = []
    for r in recipes or []:
        try:
            cost = sum(px_map[nm] * q for nm, q in r["give"])
            value = sum(px_map[nm] * q for nm, q in r["get"])
        except KeyError:
            continue
        if cost <= 0:
            continue
        n_legs = len(r["give"]) + len(r["get"])
        fees = n_legs * fee_pct(max(cost, value), adv["fee_curve"]) + adv["slippage_pct"]
        edge = (value - cost) / cost * 100 - fees
        if edge < adv["route_min_edge_pct"]:
            continue
        p_hit = beta_mean(calib["PARITY"]["hit"])
        note = ""
        if not r.get("verified"):
            p_hit = min(p_hit, 0.6)
            note = " — recipe unverified: do it once with one unit first, then mark it verified in config"
        give_nm, give_q = r["give"][0]
        get_nm, get_q = r["get"][0]
        loss = fees + 1.0
        ev = p_hit * edge - (1 - p_hit) * loss
        vol = min(vol_map.get(give_nm) or 0, vol_map.get(get_nm) or 0)
        out.append({"sig": "PARITY", "item": give_nm, "family": "parity",
                    "entry_px": px_map[give_nm], "target_px": None,
                    "p_fill": 0.85, "fill_h": 1.0, "p_hit": p_hit, "p_model": p_hit,
                    "H_h": float(adv["horizon_h"]["PARITY"]),
                    "gain_pct": edge, "loss_pct": loss, "ev_pct": ev,
                    "ret_mu": ev, "ret_sd": _ret_sd(p_hit, edge, loss),
                    "vol_div": vol,
                    "why": (f"{r['note']}: {give_q}× {give_nm} costs "
                            f"{fmt_ex(cost)} ex but converts to {get_q}× {get_nm} worth "
                            f"{fmt_ex(value)} ex — the recipe beats the market{note}"),
                    "det": {"recipe": r["note"], "cost_ex": round(cost, 2),
                            "value_ex": round(value, 2), "verified": bool(r.get("verified")),
                            "fees_pct": round(fees, 2)},
                    "parity": {"give": r["give"], "get": r["get"]},
                    "deterministic": True})
    return out


def propose_all(rows, routes, recipes, calib, adv, vol_floor):
    """rows: prepared per-item market rows. -> proposals, best EV first."""
    props = []
    for row in rows.values():
        if row["vol_div"] < vol_floor:
            continue
        for fn in (dip, make):
            p = fn(row, calib, adv)
            if p:
                props.append(p)
    for item, rts in routes.items():
        if item in ("Divine Orb", "Exalted Orb", "Chaos Orb"):
            continue
        p = route(item, rts, rows.get(item), calib, adv)
        if p and p["vol_div"] >= vol_floor:
            props.append(p)
    px_map = {nm: r["px"] for nm, r in rows.items()}
    vol_map = {nm: r["vol_div"] for nm, r in rows.items()}
    props += parity(recipes, px_map, vol_map, calib, adv)
    props.sort(key=lambda p: (p["deterministic"], p["ev_pct"]), reverse=True)
    return props
