"""Scoring and self-calibration. Forecasts are graded with PROPER scoring
rules (Brier for binaries, CRPS for distributions) — hit rates alone are
gameable, proper scores are not. Calibrated quantities live in conjugate
posteriors (Beta, Normal with pseudo-counts) shrunk toward the priors in
config: tiny samples move you off the prior at the rate the data earns.
"""
import math
from datetime import datetime, timezone

from .util import Phi, clamp, fmt_p, fmt_pct

SIGS = ("DIP", "MAKE", "ROUTE", "PARITY", "PIN", "TIDE", "MOMO")


# ------------------------------------------------------- proper scores -----
def brier(pairs):
    """pairs: [(p, y)] with y ∈ {0,1}."""
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def crps_gauss(y, mu, sd):
    if sd <= 1e-9:
        return abs(y - mu)
    z = (y - mu) / sd
    pdf = math.exp(-0.5 * z * z) / math.sqrt(2 * math.pi)
    return sd * (z * (2 * Phi(z) - 1) + 2 * pdf - 1 / math.sqrt(math.pi))


# ----------------------------------------------------------- posteriors ----
def calib_default(adv):
    cal = {}
    for sig in SIGS:
        a, b = adv["hit_prior"].get(sig, [6, 4])
        cal[sig] = {"hit": [float(a), float(b)], "fill": [7.0, 3.0]}
    m, sd, n = adv["rev_frac_prior"]
    # Normal posteriors are [mean, var, n, M2] (M2 = sum of squared deviations,
    # seeded as var*n so the prior acts like n pseudo-observations).
    cal["DIP"]["rev"] = [float(m), float(sd) ** 2, float(n), float(sd) ** 2 * float(n)]
    sv = float(adv["spread_capture_prior_pct"])
    cal["MAKE"]["spread"] = [sv, 4.0, 12.0, 4.0 * 12.0]
    return cal


def beta_mean(ab):
    return ab[0] / (ab[0] + ab[1])


def beta_sd(ab):
    a, b = ab
    n = a + b
    return math.sqrt(a * b / (n * n * (n + 1)))


def beta_update(ab, y):
    ab[0] += y
    ab[1] += 1 - y


def normal_update(msn, x, n_cap=200.0):
    """[mean, var, n, M2] ← one observation (Welford). The variance reflects the
    OBSERVED dispersion, not a decay schedule, so noisy data stays uncertain. n
    is capped (and M2 forgotten at the cap) so the estimate stays adaptive and
    M2 can't grow without bound. Tolerates a legacy [mean,var,n] tuple."""
    if len(msn) == 3:
        msn.append(msn[1] * max(msn[2], 1.0))   # seed M2 in place
    m, v, n, M2 = msn
    n2 = min(n + 1.0, n_cap)
    delta = x - m
    m2 = m + delta / n2
    M2 = M2 + delta * (x - m2)
    if n + 1.0 > n_cap:                          # at cap: forget ~one obs (sliding window)
        M2 *= (n_cap - 1.0) / n_cap
    msn[0], msn[1], msn[2], msn[3] = m2, M2 / max(n2 - 1.0, 1.0), n2, M2
    return msn


def calib_apply(calib, sig, pred, out):
    """Feed one graded prediction into the posteriors."""
    cal = calib.get(sig)
    if not cal:
        return
    if out.get("filled") is not None:
        beta_update(cal["fill"], 1 if out["filled"] else 0)
    if out.get("filled") and out.get("hit") is not None:
        beta_update(cal["hit"], 1 if out["hit"] else 0)
        if sig == "DIP" and "rev" in cal and pred.get("gap_pct"):
            # measure reversion from the best favorable move within the horizon
            # (mfe), not terminal mark-to-last — so a shorter eval horizon does
            # not bias the reversion fraction downward.
            num = out.get("mfe_pct")
            if num is None:
                num = out.get("realized_pct") or 0
            frac = clamp(num / pred["gap_pct"], -1.0, 1.5)
            normal_update(cal["rev"], frac)
        if sig == "MAKE" and "spread" in cal and out.get("realized_pct") is not None:
            normal_update(cal["spread"], clamp(out["realized_pct"], -5.0, 15.0))


def decay_calib(calib, adv, days):
    """Exponentially relax every posterior toward its PRIOR with a half-life of
    calib_half_life_d days. Posteriors otherwise accumulate lifetime counts, so
    early-league evidence outweighs the current regime forever — but div/ex
    tripled inside 17 days, and the world a hit rate was measured in evaporates.
    Each posterior asymptotes at its prior and never crosses it (λ ∈ [0,1)).
    Mutates calib in place. Composes correctly across calls: decaying by d1 then
    d2 equals one decay by d1+d2. The prediction ledger, gates and grad_points
    are NOT touched — only the live posteriors."""
    if days <= 0:
        return calib
    hl = max(adv.get("calib_half_life_d", 14), 1e-6)
    lam = 0.5 ** (days / hl)
    prior = calib_default(adv)
    for sig in SIGS:
        cal, pr = calib.get(sig), prior.get(sig)
        if not cal or not pr:
            continue
        for key in ("hit", "fill"):
            if key in cal and key in pr:
                cal[key][0] = pr[key][0] + (cal[key][0] - pr[key][0]) * lam
                cal[key][1] = pr[key][1] + (cal[key][1] - pr[key][1]) * lam
        for key in ("rev", "spread"):     # Normal states [mean, var, n, M2]
            if key in cal and key in pr:
                st, ps = cal[key], pr[key]
                st[0] = ps[0] + (st[0] - ps[0]) * lam   # mean → prior mean
                st[2] = ps[2] + (st[2] - ps[2]) * lam   # pseudo-count → seed n0
                st[3] = ps[3] + (st[3] - ps[3]) * lam   # M2 → seed M2
                st[1] = st[3] / max(st[2] - 1.0, 1.0)   # keep var consistent
    return calib


# ------------------------------------------------------------ summaries ----
def summarize(graded):
    """graded: rows from store.predictions_graded. -> per-sig stats + buckets."""
    out = {}
    for sig in SIGS:
        rows = [g for g in graded if g["sig"] == sig]
        if not rows:
            continue
        fills = [(g["pred"].get("p_fill", 0.5), 1 if g["out"].get("filled") else 0) for g in rows]
        hits = [(g["pred"].get("p_hit", 0.5), 1 if g["out"].get("hit") else 0)
                for g in rows if g["out"].get("filled")]
        rets = [(g["out"]["realized_pct"], g["pred"].get("ret_mu", 0), g["pred"].get("ret_sd", 5))
                for g in rows if g["out"].get("realized_pct") is not None]
        edges = [r[0] for r in rets]
        n_e = len(edges)
        mean_e = sum(edges) / n_e if n_e else None
        sd_e = math.sqrt(sum((x - mean_e) ** 2 for x in edges) / n_e) if n_e > 1 else None
        buckets = []
        for lo, hi in ((0, 0.6), (0.6, 0.75), (0.75, 1.01)):
            sel = [(p, y) for p, y in hits if lo <= p < hi]
            if sel:
                buckets.append({"lo": lo, "hi": min(hi, 1.0), "n": len(sel),
                                "p_mean": round(sum(p for p, _ in sel) / len(sel), 2),
                                "freq": round(sum(y for _, y in sel) / len(sel), 2)})
        out[sig] = {
            "n": len(rows), "n_filled": len(hits), "n_closed": n_e,
            "fill_brier": round(brier(fills), 3) if fills else None,
            "fill_freq": round(sum(y for _, y in fills) / len(fills), 2) if fills else None,
            "hit_brier": round(brier(hits), 3) if hits else None,
            "hit_pred": round(sum(p for p, _ in hits) / len(hits), 2) if hits else None,
            "hit_freq": round(sum(y for _, y in hits) / len(hits), 2) if hits else None,
            "edge_mean_pct": round(mean_e, 2) if mean_e is not None else None,
            "edge_sd_pct": round(sd_e, 2) if sd_e is not None else None,
            "crps": round(sum(crps_gauss(y, mu, sd) for y, mu, sd in rets) / n_e, 2) if n_e else None,
            "buckets": buckets,
        }
    return out


def _prob_buckets(pairs):
    """pairs: [(p, y)] — a model probability and its realized 0/1 outcome. ->
    calibration buckets (predicted band vs realized frequency), empty bands
    dropped."""
    out = []
    for lo, hi in ((0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
        sel = [(p, y) for p, y in pairs if lo <= p < hi]
        if sel:
            out.append({"lo": lo, "hi": min(hi, 1.0), "n": len(sel),
                        "p_mean": round(sum(p for p, _ in sel) / len(sel), 2),
                        "freq": round(sum(y for _, y in sel) / len(sel), 2)})
    return out


def model_reliability(graded):
    """Per-signal: the model's own probabilities bucketed against realized
    frequency. Two diagnostics: p_model vs hit (does a higher model estimate
    really mean a higher hit rate — the precondition for a per-card tilt), and
    p_fill_model vs fill (does the raw touch model predict fills — it did NOT for
    ROUTE: ~0.95 predicted, ~0.03 realized, which is why the shown p_fill is now
    the evidence-weighted blend, not this). The displayed odds are the pooled
    calibrated/blended rates; these stay honest diagnostics. Pure; no DB."""
    out = {}
    for sig in SIGS:
        hit_rows = [(g["pred"].get("p_model"), 1 if g["out"].get("hit") else 0)
                    for g in graded if g["sig"] == sig
                    and g["out"].get("filled") and g["pred"].get("p_model") is not None]
        fill_rows = [(g["pred"].get("p_fill_model"), 1 if g["out"].get("filled") else 0)
                     for g in graded if g["sig"] == sig
                     and g["pred"].get("p_fill_model") is not None]
        if not hit_rows and not fill_rows:
            continue
        out[sig] = {"n": len(hit_rows), "buckets": _prob_buckets(hit_rows),
                    "n_fill": len(fill_rows), "fill_buckets": _prob_buckets(fill_rows)}
    return out


def model_touch_reliability(graded):
    """Cousin of model_reliability for the PRICE-ONLY event: did the item reach
    target within H, regardless of whether our entry ever filled? Buckets
    p_model against realized touch over ALL forecasts (filled use hit; unfilled
    use the watched touch) — so it finally sees the ~4-of-7 DIPs that expired
    unfilled and taught the hit model nothing. SELECTION-BIAS caveat: touch is
    unconditional on fill and therefore EASIER than a filled hit, so these rates
    read high — a diagnostic of price forecasting, never a sizing input. Pure."""
    out = {}
    for sig in SIGS:
        rows = [(g["pred"].get("p_model"),
                 1 if g["out"].get("touch", g["out"].get("hit")) else 0)
                for g in graded if g["sig"] == sig
                and g["pred"].get("p_model") is not None
                and (g["out"].get("touch") is not None or g["out"].get("filled"))]
        if not rows:
            continue
        buckets = []
        for lo, hi in ((0.0, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.01)):
            sel = [(p, y) for p, y in rows if lo <= p < hi]
            if sel:
                buckets.append({"lo": lo, "hi": min(hi, 1.0), "n": len(sel),
                                "p_mean": round(sum(p for p, _ in sel) / len(sel), 2),
                                "freq": round(sum(y for _, y in sel) / len(sel), 2)})
        out[sig] = {"n": len(rows), "buckets": buckets}
    return out


def _feat_buckets(pairs, edges, labels):
    """pairs: [(x, hit)] for one raw feature; `edges` gives len(labels)+1 cut
    points defining consecutive bins [edges[i], edges[i+1]). -> bucket dicts,
    dropping any with n<5 (a bucket that thin says nothing)."""
    out = []
    for i, lab in enumerate(labels):
        lo, hi = edges[i], edges[i + 1]
        sel = [y for x, y in pairs if x is not None and lo <= x < hi]
        if len(sel) >= 5:
            out.append({"label": lab, "n": len(sel),
                        "freq": round(sum(sel) / len(sel), 2)})
    return out


def feature_reliability(graded):
    """Diagnostic: does a raw feature (entry depth z_ou, latent drift, book
    size) separate winners from losers WITHIN a signal? Per-signal realized hit
    frequency bucketed across feature ranges; buckets with n<5 are suppressed.
    Pure; no DB. NEVER used to size or gate — it only tells us which features
    would be worth a future per-card model tilt."""
    INF = float("inf")
    out = {}
    for sig in SIGS:
        rows = [(g["pred"].get("feat") or {}, 1 if g["out"].get("hit") else 0)
                for g in graded if g["sig"] == sig and g["out"].get("filled")]
        if len(rows) < 10:
            continue
        feats = {}
        if sig == "DIP":
            z = [(f.get("z_ou"), y) for f, y in rows if f.get("z_ou") is not None]
            b = _feat_buckets(z, [-INF, -2.5, INF], ["z_ou<-2.5", "z_ou>=-2.5"])
            if b:
                feats["z_ou"] = b
            dr = [(f.get("drift_z"), y) for f, y in rows if f.get("drift_z") is not None]
            b = _feat_buckets(dr, [-INF, -0.5, 0.5, INF],
                              ["drift<-0.5", "drift -0.5..0.5", "drift>0.5"])
            if b:
                feats["drift_z"] = b
        vd = sorted(f.get("vol_div") for f, _ in rows if f.get("vol_div") is not None)
        if len(vd) >= 15:
            t1, t2 = vd[len(vd) // 3], vd[2 * len(vd) // 3]
            pairs = [(f.get("vol_div"), y) for f, y in rows if f.get("vol_div") is not None]
            b = _feat_buckets(pairs, [-INF, t1, t2, INF],
                              ["vol low", "vol mid", "vol high"])
            if b:
                feats["vol_div"] = b
        if feats:
            out[sig] = {"n": len(rows), "feats": feats}
    return out


def fill_by_hour(graded):
    """Diagnostic: does the fill rate of resting entry orders swing with time
    of day / day of week? Mean promised p_fill vs realized fill frequency per
    UTC 6-hour band and weekday/weekend. Buckets with n<5 suppressed. Pure; no
    DB. Never sizes — it just flags whether we quote into thin hours."""
    def stat(rows):
        n = len(rows)
        return {"n": n, "p_fill_mean": round(sum(p for p, _ in rows) / n, 2),
                "fill_freq": round(sum(y for _, y in rows) / n, 2)}
    recs = []
    for g in graded:
        ts = g.get("ts") or ""
        if len(ts) < 13:
            continue
        try:
            wknd = datetime.fromisoformat(ts).weekday() >= 5
        except ValueError:
            continue
        recs.append((int(ts[11:13]), wknd,
                     g["pred"].get("p_fill", 0.5), 1 if g["out"].get("filled") else 0))
    out = {"utc_band": [], "day": []}
    for lab, lo, hi in (("00-06", 0, 6), ("06-12", 6, 12), ("12-18", 12, 18), ("18-24", 18, 24)):
        sel = [(p, y) for h, _, p, y in recs if lo <= h < hi]
        if len(sel) >= 5:
            out["utc_band"].append({"band": lab, **stat(sel)})
    for lab, wk in (("weekday", False), ("weekend", True)):
        sel = [(p, y) for _, w, p, y in recs if w == wk]
        if len(sel) >= 5:
            out["day"].append({"day": lab, **stat(sel)})
    return out if (out["utc_band"] or out["day"]) else {}


def update_gates(gates, summary, adv):
    """Auto-gate signals that can't earn their keep. Two independent triggers,
    both with hysteresis; gated signals keep shadow-trading to earn their way
    back:

      • edge — the realized edge can't clear zero (needs gate_n_min CLOSED trades);
      • hit-calibration — the fills hit far less often than the forecasts
        promised. A signal can fire hundreds of times yet rarely CLOSE (so the
        edge gate never trips) while its fabricated gains keep its EV positive —
        ROUTE's 0/14 hits vs a promised ~61% is overwhelming long before 20 close.
    """
    for sig, s in summary.items():
        prev = gates.get(sig, {}).get("off", False)
        off, evaluated = prev, False
        n, mean_e, sd_e = s["n_closed"], s["edge_mean_pct"], s["edge_sd_pct"]
        if n and n >= adv["gate_n_min"] and mean_e is not None and sd_e:
            evaluated = True
            p_pos = Phi(mean_e / (sd_e / math.sqrt(n)))
            if p_pos < 0.40:
                off = True
            elif p_pos > 0.55:
                off = False
        m, hp, hf = s["n_filled"], s["hit_pred"], s["hit_freq"]
        if m and m >= adv["gate_fill_min"] and hp is not None and hf is not None:
            evaluated = True
            # z of (realized − expected) hits under "forecasts are calibrated";
            # Poisson-binomial variance ≈ m·p̄·(1−p̄).
            z = (hf - hp) * m / math.sqrt(max(m * hp * (1 - hp), 1e-6))
            p_cal = Phi(z)
            if p_cal < 0.05:
                off = True
            elif p_cal > 0.30:
                off = False
        if evaluated:
            gates[sig] = {"off": off, "n": n, "edge": mean_e, "hit_freq": hf}
    return gates


# ----------------------------------------------------- trust + graduation --
def trust_line(graded30, mode, half_life_d=None):
    recency = f" · recency-weighted (½-life {int(half_life_d)}d)" if half_life_d else ""
    closed = [g for g in graded30 if g["out"].get("realized_pct") is not None]
    if len(closed) < 5:
        n = len(graded30)
        return (f"Still proving itself — {n} forecast{'s' if n != 1 else ''} graded so far; "
                f"the shadow book needs ~{max(0, 5 - len(closed))} more closed trades to say anything honest.")
    mean_e = sum(g["out"]["realized_pct"] for g in closed) / len(closed)
    hits = [g for g in closed if g["out"].get("hit")]
    return (f"Last 30d: {len(closed)} closed calls (incl. untaken, tracked anyway) · "
            f"{len(hits)} hit target · avg {fmt_pct(mean_e, signed=True)} per trade after est. fees"
            + (" · paper mode" if mode == "paper" else "") + recency)


def graduation(grad_points, adv, mode):
    """grad_points: [{'d': date, 'alpha': paper nw minus worst benchmark, div}].
    Daily increments must clear zero with t ≥ grad_t_min over ≥ grad_days_min days."""
    bydate = {}
    for p in grad_points:
        bydate[p["d"]] = p["alpha"]  # last of day wins
    days = sorted(bydate)
    if len(days) < 3:
        return {"ready": False, "line": "Graduation: collecting data — needs "
                f"{adv['grad_days_min']} days of paper results (have {max(0, len(days) - 1)})."}
    diffs = [bydate[b] - bydate[a] for a, b in zip(days, days[1:])]
    n = len(diffs)
    mean = sum(diffs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in diffs) / max(n - 1, 1)) or 1e-9
    t = mean / (sd / math.sqrt(n))
    ready = n >= adv["grad_days_min"] and t >= adv["grad_t_min"]
    if mode == "real":
        return {"ready": True, "line": "Real mode — every fill is yours; paper history stays separate."}
    if ready:
        return {"ready": True, "t": round(t, 2),
                "line": f"Graduation: EARNED — {n} days of paper alpha, t={t:.1f} vs the worst "
                        "benchmark. Switching to real is statistically defensible now."}
    return {"ready": False, "t": round(t, 2),
            "line": f"Graduation: not yet — {n}/{adv['grad_days_min']} days, t={t:.1f} "
                    f"(needs ≥{adv['grad_t_min']}). The app recommends real money only when "
                    "its own scored results clear this bar."}


def today():
    return datetime.now(timezone.utc).date().isoformat()
