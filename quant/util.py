"""Shared helpers: time, matching, Gaussian math, and the language lint.

All user-facing numbers MUST pass through the fmt_* functions below. They round
to the precision the models actually have — the UI is not allowed to imply
more. Probabilities render as plain odds ("7 in 10"), never as decimals.
"""
import math
from datetime import datetime, timedelta, timezone


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(ts):
    return datetime.fromisoformat(ts)


def add_hours(ts, h):
    return (parse_iso(ts) + timedelta(hours=h)).isoformat(timespec="seconds")


def hours_between(ts_old, ts_new):
    try:
        return (parse_iso(ts_new) - parse_iso(ts_old)).total_seconds() / 3600.0
    except Exception:
        return 0.0


def clamp(x, lo, hi):
    return lo if x < lo else hi if x > hi else x


# ------------------------------------------------------------- gaussian ----
def phi(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def Phi(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def inv_mills(a):
    """E[Z | Z > a] for standard normal = phi(a)/(1-Phi(a)), guarded."""
    p = 1.0 - Phi(a)
    return phi(a) / p if p > 1e-9 else a


def Phi_inv(p):
    """Standard normal quantile by bisection — plenty for card math."""
    p = clamp(p, 1e-6, 1 - 1e-6)
    lo, hi = -8.0, 8.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if Phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


# ------------------------------------------------------------- matching ----
def norm_name(s):
    return " ".join((s or "").replace("’", "'").casefold().split())


def best_match(query, names):
    """Most specific name matching query: exact > prefix > substring > all-words."""
    qn = norm_name(query)
    if not qn:
        return None
    best_tier, best_nm, best_len = 0, None, 10 ** 9
    for nm in names:
        nn = norm_name(nm)
        if nn == qn:
            tier = 4
        elif nn.startswith(qn):
            tier = 3
        elif qn in nn:
            tier = 2
        elif all(w in nn for w in qn.split()):
            tier = 1
        else:
            continue
        if tier > best_tier or (tier == best_tier and len(nn) < best_len):
            best_tier, best_nm, best_len = tier, nm, len(nn)
    return best_nm


def snap_name(typed, names):
    """Bind a typed/pasted item name to the canonical one in `names`.

    Conservative: only snaps when the typed name is the SAME item up to
    apostrophe style (’ vs '), case, and whitespace — which is exactly the
    copy/paste failure mode (the game uses a curly apostrophe). A genuinely
    different/unknown item (e.g. an unscanned catalyst) is left untouched, so
    distinct items never get merged."""
    if not typed:
        return typed
    qn = norm_name(typed)
    for nm in names or []:
        if norm_name(nm) == qn:
            return nm
    return typed


# -------------------------------------------------------- language lint ----
def fmt_ex(v):
    if v is None:
        return "?"
    v = float(v)
    if v >= 100:
        return f"{v:,.0f}"
    if v >= 10:
        return f"{v:.1f}".rstrip("0").rstrip(".")
    if v >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3f}".rstrip("0").rstrip(".")


def fmt_div(v):
    if v is None:
        return "?"
    return f"{float(v):,.2f}".rstrip("0").rstrip(".")


def fmt_money(ex, rate):
    """Both denominations when the div side is worth saying."""
    s = f"{fmt_ex(ex)} ex"
    if rate and abs(ex) / rate >= 0.01:
        s += f" (≈{fmt_div(ex / rate)} div)"
    return s


def fmt_signed_ex(ex, rate=None):
    sign = "+" if ex >= 0 else "−"
    return sign + fmt_money(abs(ex), rate)


def fmt_p(p):
    """Probability as plain odds. Never sharper than tenths."""
    n = round(clamp(p, 0.02, 0.98) * 10)
    if n <= 1:
        return "1 in 10 at best"
    if n >= 9:
        return "9 in 10"
    return f"{n} in 10"


def fmt_dur_h(h):
    if h is None:
        return "?"
    if h < 1.2:
        return "within the hour"
    if h < 20:
        return f"about {max(1, round(h))} hours"
    if h < 34:
        return "about a day"
    return f"about {round(h / 24)} days"


def fmt_pct(p, signed=False):
    s = f"{abs(p):.0f}%" if abs(p) >= 3 else f"{abs(p):.1f}%"
    if signed:
        s = ("+" if p >= 0 else "−") + s
    return s


def fmt_age_m(minutes):
    if minutes is None:
        return "never"
    if minutes < 1.5:
        return "just now"
    if minutes < 90:
        return f"{round(minutes)} min ago"
    if minutes < 36 * 60:
        return f"{round(minutes / 60)}h ago"
    return f"{round(minutes / 1440)}d ago"
