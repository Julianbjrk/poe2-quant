"""The forecasting math. Stdlib only, every formula closed-form.

- Kalman filter per item over LOG price: 2D state (level, drift/hour), each
  source is a separate noisy observation with its own adaptive variance.
  Aggregator numbers are treated as what they are: noisy, lagged observations
  of a latent price.
- Ornstein–Uhlenbeck (discrete AR(1)) fit on hourly bars with shrinkage toward
  a slow-reversion prior — gives the full distribution of price at horizon H.
  P(target reached) uses the marginal at H, which UNDER-states first-passage:
  the conservative direction, on purpose.
- Brownian touch probability for fill-time forecasts (reflection principle).
- Integer-ratio quantization: the in-game exchange takes whole-number ratios,
  so edge math runs on the nearest representable ratio and cards can print the
  literal order to type. Items too cheap to represent fail here — that IS the
  lot-size guard, principled this time.
"""
import math
from datetime import datetime

from .util import Phi, clamp

# ------------------------------------------------------------- kalman ------
RV0 = 4e-4          # prior hourly return variance (2%/√h) until data speaks
R0 = {"ninja": 1.0e-4, "pairex": 4.0e-4, "pairdiv": 4.0e-4}  # obs noise priors


def kf_new(z):
    return {"m": [z, 0.0], "P": [[2.5e-3, 0.0], [0.0, 2.5e-5]],
            "rv": RV0, "R": {}, "n": 0}


def kf_predict(st, dt_h):
    dt = clamp(dt_h, 1e-3, 48.0)
    l, d = st["m"]
    st["m"] = [l + d * dt, d]
    p = st["P"]
    # P = F P F' + Q
    p00 = p[0][0] + dt * (p[0][1] + p[1][0]) + dt * dt * p[1][1]
    p01 = p[0][1] + dt * p[1][1]
    p11 = p[1][1]
    q = st["rv"]
    st["_p00_noq"] = p00   # FPF'₀₀ before process noise — the baseline the rv estimator subtracts
    st["P"] = [[p00 + q * dt, p01], [p01, p11 + q * dt * 0.02]]


def kf_update(st, z, source):
    R = st["R"].get(source, R0.get(source, 4e-4))
    p, m = st["P"], st["m"]
    S = p[0][0] + R
    if S <= 0:
        return
    y = z - m[0]
    k0, k1 = p[0][0] / S, p[1][0] / S
    st["m"] = [m[0] + k0 * y, m[1] + k1 * y]
    st["P"] = [[(1 - k0) * p[0][0], (1 - k0) * p[0][1]],
               [p[1][0] - k1 * p[0][0], p[1][1] - k1 * p[0][1]]]
    # adaptive OBSERVATION noise only — the process-variance (rv) estimate moved
    # to kf_step, which can subtract this R and the predicted level variance to
    # isolate the item's true per-hour volatility rather than the noise floor.
    st["R"][source] = clamp(0.95 * R + 0.05 * max(y * y - p[0][0], 1e-8), 1e-6, 0.25)
    st["n"] += 1
    return y, R


def kf_step(st, dt_h, obs):
    """obs: {source: log_price}. One predict, sequential updates. The first
    update's innovation drives the process-variance estimate: E[y²] = S =
    FPF'₀₀ + q·dt + R, so (y² − FPF'₀₀ − R)/dt is a method-of-moments estimate
    of the per-hour volatility q — tracking the item's own volatility, not the
    obs-noise floor the old rv ← 0.97·rv + 0.03·y² converged to."""
    kf_predict(st, dt_h)
    dt = clamp(dt_h, 1e-3, 48.0)
    first = True
    for source, z in obs.items():
        yR = kf_update(st, z, source)
        if first and yR is not None:
            y, R = yR
            # (y² − FPF'₀₀ − R)/dt is a per-sample estimate of q; it is SIGNED on
            # purpose — samples below expectation must cancel those above, or the
            # estimate biases upward toward the obs-noise floor (the very bug this
            # fixes). Only the smoothed rv is floored, never the raw contribution.
            st["rv"] = clamp(0.97 * st["rv"] + 0.03 * (y * y - st["_p00_noq"] - R) / dt,
                             1e-6, 1e-2)
            first = False


def kf_level(st):
    return st["m"][0]


def kf_sd(st):
    return math.sqrt(max(st["P"][0][0], 1e-12))


def kf_drift_z(st):
    return st["m"][1] / math.sqrt(max(st["P"][1][1], 1e-12))


def kf_sig_h(st):
    """Realized per-√hour vol estimate (log units)."""
    return math.sqrt(max(st["rv"], 1e-8))


# ----------------------------------------------------------------- OU ------
B_PRIOR, N_PRIOR = 0.97, 24.0   # slow hourly reversion until the item proves otherwise


def fit_ou(closes):
    """closes: [(hour_iso, price)] hourly, oldest first. -> dict or None.

    Anchor (theta) is the MEDIAN of the window — the AR(1) intercept
    extrapolates trends and lands outside the data, which silently kills dip
    detection. The AR coefficient only sets reversion speed; deviation spread
    is measured directly around the anchor. Only CONTIGUOUS (1h-apart) bar
    pairs feed the reversion estimate, so a sleep/offline gap isn't mistaken
    for one hour of reversion."""
    pts = [(h, math.log(p)) for h, p in closes if p and p > 0]
    if len(pts) < 8:
        return None
    xs = [x for _, x in pts]
    s = sorted(xs)
    m = len(s) // 2
    theta = s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])
    pairs = []
    for (h0, x0), (h1, x1) in zip(pts, pts[1:]):
        try:
            dt = (datetime.fromisoformat(h1) - datetime.fromisoformat(h0)).total_seconds() / 3600.0
        except Exception:
            dt = 1.0
        if 0.5 <= dt <= 1.5:          # contiguous hourly step only
            pairs.append((x0, x1))
    if len(pairs) >= 5:
        n = len(pairs)
        mx = sum(p[0] for p in pairs) / n
        my = sum(p[1] for p in pairs) / n
        vx = sum((p[0] - mx) ** 2 for p in pairs) / n
        cxy = sum((p[0] - mx) * (p[1] - my) for p in pairs) / n
        b_hat = clamp(cxy / vx if vx > 1e-12 else B_PRIOR, 0.5, 0.999)
        # Kendall small-sample correction: OLS AR(1) is biased low by ≈(1+3b)/n,
        # which would shorten H = 3/κ (part of the graded event) for no real
        # reason. Corrects the raw estimate before shrinkage toward the prior.
        b_hat = clamp(b_hat + (1.0 + 3.0 * b_hat) / n, 0.5, 0.999)
        b = (n * b_hat + N_PRIOR * B_PRIOR) / (n + N_PRIOR)
    else:
        b = B_PRIOR               # too few contiguous pairs to estimate reversion
    dev2 = sum((x - theta) ** 2 for x in xs) / len(xs)
    sd_st = math.sqrt(max(dev2, 1e-8))
    return {"theta": theta, "b": b,
            "sig_h": sd_st * math.sqrt(max(1 - b * b, 1e-4)),
            "sd_st": sd_st, "n": len(xs)}


def ou_horizon(x0, ou, H_h, rev_frac=1.0):
    """Marginal distribution of log price at horizon H. rev_frac scales how much
    of the gap to the mean is expected to close (calibrated, prior 0.7)."""
    bH = ou["b"] ** H_h
    theta_eff = x0 + rev_frac * (ou["theta"] - x0)
    mean = theta_eff + (x0 - theta_eff) * bH
    sd = ou["sd_st"] * math.sqrt(max(1 - bH * bH, 1e-6))
    return mean, sd


def prob_ge(mean, sd, x):
    if sd <= 1e-9:
        return 1.0 if mean >= x else 0.0
    return 1.0 - Phi((x - mean) / sd)


def below_mean(mean, sd, x):
    """E[X | X < x] for X~N(mean,sd): truncated-normal lower mean."""
    if sd <= 1e-9:
        return min(mean, x)
    a = (x - mean) / sd
    Fa = Phi(a)
    if Fa < 1e-6:
        return x - sd
    return mean - sd * math.exp(-0.5 * a * a) / math.sqrt(2 * math.pi) / Fa


# ----------------------------------------------------- fill-time model -----
def touch_prob(dist_log, sig_h, H_h):
    """P(Brownian path moves dist within H hours) — reflection principle.
    Conservatively capped: aggregator data can't justify more than 0.95."""
    if dist_log <= 0:
        return 0.95
    if sig_h <= 1e-9 or H_h <= 0:
        return 0.0
    return clamp(2.0 * (1.0 - Phi(dist_log / (sig_h * math.sqrt(H_h)))), 0.0, 0.95)


def touch_prob_drift(d, mu, sig, T):
    """P(a Brownian motion with drift mu and vol sig reaches level +d within T) —
    the exact first-passage reflection formula. The graded DIP hit IS a touch of
    target within H, so this is the right p_model diagnostic; the endpoint
    marginal prob_ge understates it (a path can touch then retreat). Reduces to
    the driftless touch_prob at mu=0. Same conservative 0.95 cap as the aggregator
    data can justify. Diagnostic only — never sizes."""
    if d <= 0:
        return 0.95
    if sig <= 1e-9 or T <= 0:
        return 0.0
    a = clamp(2.0 * mu * d / (sig * sig), -50.0, 50.0)
    root = sig * math.sqrt(T)
    return clamp(Phi((mu * T - d) / root) + math.exp(a) * Phi((-mu * T - d) / root), 0.0, 0.95)


def touch_median_h(dist_log, sig_h):
    """Median first-passage time for distance d: P=0.5 ⇒ d/(σ√t)=Φ⁻¹(0.75)."""
    if dist_log <= 0:
        return 0.25
    if sig_h <= 1e-9:
        return 999.0
    return (dist_log / (0.6745 * sig_h)) ** 2


# ----------------------------------------------------------- regime --------
def regime_update(prev, dt_h, idx_ret, div_drift_z, disp, ts=None):
    """Pure market-regime classifier. prev: {state, slope_ewma, streak, cand,
    since_ts, disp} or None. idx_ret is this poll's volume-weighted index log
    return. Keeps an EWMA of the per-DAY index slope (48h half-life, for display)
    and flips state only after a direction holds for >=6 consecutive polls
    (hysteresis — a single-poll spike never flips it). BULL when the smoothed
    slope > +1%/day or divine drift_z > +1.5; BEAR mirrored; else CHOP.
    Diagnostic: nothing gates on the regime yet."""
    st = (dict(prev) if prev else
          {"state": "CHOP", "slope_ewma": 0.0, "streak": 0, "cand": "CHOP",
           "since_ts": ts, "disp": disp})
    dt_h = max(dt_h, 1e-3)
    a = 1.0 - 0.5 ** (dt_h / 48.0)
    st["slope_ewma"] = (1 - a) * st["slope_ewma"] + a * (idx_ret * 24.0 / dt_h)
    st["disp"] = disp
    up = st["slope_ewma"] > 0.01 or (div_drift_z or 0.0) > 1.5
    down = st["slope_ewma"] < -0.01 or (div_drift_z or 0.0) < -1.5
    cand = "BULL" if up and not down else "BEAR" if down and not up else "CHOP"
    st["streak"] = st["streak"] + 1 if cand == st.get("cand") else 1
    st["cand"] = cand
    if cand != st["state"] and st["streak"] >= 6:
        st["state"] = cand
        st["since_ts"] = ts
    return st


# ------------------------------------------------------ ratio quantizer ----
def best_ratio(price_ex, side, max_lot=20, tol_pct=4.0, good_pct=0.5):
    """Nearest in-game ratio that does not cross the limit.
    buy : offer G exalted for N items, unit G/N ≤ price (your ceiling)
    sell: list N items for E exalted, unit E/N ≥ price (your floor)
    Picks the SMALLEST lot whose rounding cost is ≤ good_pct — a tighter ratio
    is not worth a lot too chunky to fill or afford. Returns None when the
    item can't be expressed within tol — the principled lot-size guard."""
    if price_ex <= 0:
        return None
    best = None
    for n in range(1, max_lot + 1):
        if side == "buy":
            g = math.floor(price_ex * n)
            if g < 1:
                continue
            unit = g / n
            err = (price_ex - unit) / price_ex * 100
        else:
            g = math.ceil(price_ex * n)
            unit = g / n
            err = (unit - price_ex) / price_ex * 100
        if err < -1e-9:
            continue
        if best is None or err < best["err_pct"] - 1e-9:
            best = {"give": g if side == "buy" else n, "get": n if side == "buy" else g,
                    "unit": unit, "err_pct": err, "lot": n}
        if err <= good_pct:
            break
    if best is None or best["err_pct"] > tol_pct:
        return None
    return best


# ----------------------------------------------------------------- fees ----
def fee_pct(value_ex, curve):
    """Piecewise-linear gold-fee estimate (% per side) by trade value."""
    pts = sorted((float(v), float(f)) for v, f in (curve or [[0, 1.0]]))
    if value_ex <= pts[0][0]:
        return pts[0][1]
    for (v0, f0), (v1, f1) in zip(pts, pts[1:]):
        if value_ex <= v1:
            w = (value_ex - v0) / (v1 - v0) if v1 > v0 else 0.0
            return f0 + w * (f1 - f0)
    return pts[-1][1]


# ------------------------------------------------------------- helpers -----
def median(xs):
    s = sorted(xs)
    if not s:
        return 0.0
    m = len(s) // 2
    return s[m] if len(s) % 2 else 0.5 * (s[m - 1] + s[m])


def daily_anchor(prices):
    """Robust (median, MAD-sd) of log price for a window of DAILY averages —
    the league-history anchor used before intraday models have data."""
    xs = [math.log(p) for p in prices if p and p > 0]
    if len(xs) < 5:
        return None
    med = median(xs)
    mad = median([abs(x - med) for x in xs])
    return med, max(1.4826 * mad, 0.01)


def weighted_median(pairs):
    """pairs: [(value, weight)]"""
    s = sorted((v, w) for v, w in pairs if w > 0)
    if not s:
        return 0.0
    tot = sum(w for _, w in s)
    acc = 0.0
    for v, w in s:
        acc += w
        if acc >= tot / 2:
            return v
    return s[-1][0]
