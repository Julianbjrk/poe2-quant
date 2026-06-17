"""Configuration: zero required, two optional, everything else earned.

config.json (surface)   — league, mode (paper/real), risk preset, pins. Created
                          on first run; the app is fully functional without
                          touching it.
config.advanced.json    — every knob, for power users. Absent by default.
A v0.4 config.json is migrated automatically (backup written first).
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
ADVANCED_PATH = ROOT / "config.advanced.json"
DB_PATH = ROOT / "quant.db"

SURFACE_DEFAULTS = {
    "league": "auto",
    "mode": "paper",            # paper | real — the app tells you when real is earned
    "risk": "conservative",     # conservative | standard | aggressive
    "pins": [],                 # manual theses: {"label","match","source","entry_max_ex","exit_target_ex","budget_div"}
    "update_branch": "claude/confident-mccarthy-clkrci",  # GitHub branch the updater tracks
    "auto_update": False,       # True = apply updates on startup; False = notify + one-click
    "auto_bootstrap": True,     # fetch league history + calibrate on first run, no flag needed
}

# Presets scale the few judgment calls; everything else is calibrated output.
PRESETS = {
    "conservative": {"p_edge_min": 0.65, "kelly_frac": 0.25, "vol_floor_x": 1.0, "max_positions": 3},
    "standard":     {"p_edge_min": 0.60, "kelly_frac": 0.35, "vol_floor_x": 0.7, "max_positions": 4},
    "aggressive":   {"p_edge_min": 0.55, "kelly_frac": 0.50, "vol_floor_x": 0.5, "max_positions": 5},
}

# PoE2 deterministic conversions. Recipes are DATA: verify each once in-game,
# then set "verified": true. Unverified recipes still scan but say so on the card.
DEFAULT_RECIPES = [
    {"give": [["Distilled " + a, 3]], "get": [["Distilled " + b, 1]],
     "note": "instilling combine 3:1", "verified": False}
    for a, b in [("Ire", "Guilt"), ("Guilt", "Greed"), ("Greed", "Paranoia"),
                 ("Paranoia", "Envy"), ("Envy", "Disgust"), ("Disgust", "Despair"),
                 ("Despair", "Fear"), ("Fear", "Suffering"), ("Suffering", "Isolation")]
]

ADVANCED_DEFAULTS = {
    "poll_minutes": 5,
    "paper_bankroll_div": 5.0,      # notional until real holdings are entered
    "min_volume_div_day": 150,      # × preset vol_floor_x
    "make_min_volume": 600,
    "max_pos_pct_volume": 2,        # position ≤ this % of item's daily traded value
    "liquid_reserve_pct": 25,
    "min_profit_ex": 10,
    "fee_curve": [[0, 1.0]],        # [(value_ex, fee_pct_per_side)] piecewise-linear; measure in-game once
    "slippage_pct": 1.0,            # haircut per executed leg beyond fees
    "dip_z": 1.8,                   # OU-units below mean to trigger DIP
    "dip_p_aim": 0.65,              # place the exit where P(reach by horizon) ≥ this
    "idio_z": 1.0,                  # item must be cheap vs its own family too
    "knife_drift_z": -1.3,          # reject entries when latent drift below this
    "family_z_floor": -2.5,         # family in freefall → no dip entries
    "route_min_edge_pct": 4.0,
    "route_min_trades": 10,
    "route_band_pct": 40,           # each leg must sit within this of ninja mid
    "spread_capture_prior_pct": 6.0,
    "hit_prior": {"DIP": [6, 4], "MAKE": [6, 5], "ROUTE": [7, 3], "PARITY": [8, 2]},  # Beta(a,b)
    "rev_frac_prior": [0.7, 0.15, 12],   # mean, sd, pseudo-n for DIP reversion fraction
    "horizon_h": {"DIP": 72, "MAKE": 24, "ROUTE": 12, "PARITY": 12},
    "fill_window_h": 6,             # entry order must fill within this or expire
    "max_hold_h": 96,               # shadow positions force-marked after this
    "hysteresis": 0.8,              # active card survives until score < gate × this
    "rotation_margin": 1.3,         # replacement must be ≥30% better per hour
    "max_cards": 3,
    "shadow_cap": 8,                # concurrent shadow-book forecasts (independent of your slots)
    "gate_n_min": 20,               # graded outcomes before auto-gating can trip
    "circuit_z": 2.5,               # market-wide move (in its own sd) that halts entries
    "grad_days_min": 14,            # graduation: days of paper curve required…
    "grad_t_min": 1.64,             # …and t-stat of daily alpha vs worst benchmark
    "tick_keep_days": 14,           # DB keeps this rolling window of 5-min ticks…
    "archive_ticks": True,          # …and appends older ones to data_archive/ so nothing is lost
    "snap_keep_days": 60,
    "github_token": "",             # only needed to self-update a PRIVATE repo (read-only PAT)
    "recipes": DEFAULT_RECIPES,
    "scan_types": ["Currency", "Fragments", "Essences", "Runes", "SoulCores",
                   "LineageSupportGems", "Expedition", "Ritual", "Abyss",
                   "Delirium", "UncutGems", "Idols"],
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _migrate_v04(raw):
    """Fold a v0.4 config into (surface, advanced_overrides)."""
    surface = dict(SURFACE_DEFAULTS)
    adv = {}
    surface["league"] = raw.get("league", "auto")
    surface["mode"] = "paper" if raw.get("paper_mode", True) else "real"
    for p in raw.get("plays") or []:
        if "PLACEHOLDER" in str(p.get("label", "")) or p.get("id") == "jewellers":
            continue
        surface["pins"].append({
            "label": p.get("label") or p.get("id"), "match": p.get("match", ""),
            "source": p.get("source", "auto"),
            "entry_max_ex": p.get("entry_max_ex") or 0,
            "exit_target_ex": p.get("exit_target_ex") or 0,
            "budget_div": p.get("budget_div") or 1.0})
    r = raw.get("risk") or {}
    keep = {"min_volume_div_day", "make_min_volume", "max_pos_pct_volume",
            "liquid_reserve_pct", "min_profit_ex"}
    for k in keep & set(r):
        adv[k] = r[k]
    if r.get("fee_pct_per_side") is not None:
        adv["fee_curve"] = [[0, float(r["fee_pct_per_side"])]]
    if raw.get("poll_minutes"):
        adv["poll_minutes"] = raw["poll_minutes"]
    if raw.get("start_capital_div"):
        adv["paper_bankroll_div"] = raw["start_capital_div"]
    return surface, adv


def load():
    """-> cfg dict: surface keys + cfg['adv'] (merged advanced) + cfg['preset']."""
    surface, migrated_adv = dict(SURFACE_DEFAULTS), None
    if CONFIG_PATH.exists():
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
        if "paper_mode" in raw or "plays" in raw or "start_capital_div" in raw:
            CONFIG_PATH.rename(CONFIG_PATH.with_suffix(".json.v04.bak"))
            surface, migrated_adv = _migrate_v04(raw)
            CONFIG_PATH.write_text(json.dumps(surface, indent=2), encoding="utf-8")
            if migrated_adv and not ADVANCED_PATH.exists():
                ADVANCED_PATH.write_text(json.dumps(migrated_adv, indent=2), encoding="utf-8")
            print("Migrated v0.4 config.json → v1.0 (backup: config.json.v04.bak)")
        else:
            surface = _deep_merge(SURFACE_DEFAULTS, raw)
    else:
        CONFIG_PATH.write_text(json.dumps(SURFACE_DEFAULTS, indent=2), encoding="utf-8")
    adv = dict(ADVANCED_DEFAULTS)
    if ADVANCED_PATH.exists():
        adv = _deep_merge(adv, json.loads(ADVANCED_PATH.read_text(encoding="utf-8-sig")))
    cfg = dict(surface)
    if cfg.get("risk") not in PRESETS:
        cfg["risk"] = "conservative"
    if cfg.get("mode") not in ("paper", "real"):
        cfg["mode"] = "paper"
    cfg["adv"] = adv
    cfg["preset"] = PRESETS[cfg["risk"]]
    return cfg


def save_surface(cfg):
    out = {k: cfg[k] for k in SURFACE_DEFAULTS}
    CONFIG_PATH.write_text(json.dumps(out, indent=2), encoding="utf-8")
