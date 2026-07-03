# PREDICTION_UPGRADES.md — the prediction-improvement program (v1.4 → v2)

**Audience: an implementing model.** Every task below is written to be executed mechanically.
Follow the House Rules, do the tasks in order inside each phase, one task per commit. When in
doubt, do the SMALLER thing the task says, not the smarter thing you imagine.

**Provenance.** Produced by a 4-lens code review (statistical methodology, learning speed, market
adaptivity, data exploitation) over quant v1.4.0, cross-checked against 17 days of live field data
(league "Runes of Aldur": basket 6.2× in ex, div/ex 165→461, DIP 3/3 hits +14.8%, ROUTE 406
forecasts / 3% fill / 0 of 14 hits, paper graduation t=−2.35), then adversarially filtered:
duplicate findings merged, and proposals that would overfit these sample sizes (per-item Betas,
tier/regime posterior splits on ~7 DIP forecasts per 17 days) were demoted to
"measure-first" diagnostics. Convergent findings (independent lenses proposing the same fix) were
prioritized.

---

## House Rules for the implementing model (read before every task)

1. **Run `python3 -m unittest discover -s tests` before you start and after every task.** All
   green before → all green after (plus your new tests). Never commit red.
2. **The honesty loop is sacred.** Every probability shown on a card must be either (a) the
   measured frequency of exactly the event the shadow book grades, or (b) explicitly labeled a
   model diagnostic (like `p_model`) and never used for sizing. If your change makes a shown
   number diverge from its graded event, you are doing it wrong.
3. **MODEL_V policy.** Changing *forecast math or a graded-event definition* for an EXISTING
   signal requires bumping `MODEL_V` in `quant/__init__.py` (the versioned-calibration machinery
   in `engine._load_calib_versioned` then archives+resets posteriors safely). ADDING a new signal
   with its own name does NOT require a bump. Diagnostics never require a bump. **Phase 1 batches
   all bump-worthy changes into ONE bump (m2→m3), flipped only in Task 11 — do not bump earlier.**
4. **stdlib only.** No pip installs, ever. `math`, `json`, `sqlite3`, `csv`, `datetime` are your
   toolbox.
5. **Version discipline.** Bump the app version (VERSION + `quant/__init__.py.__version__`) once
   per task/commit so the self-updater ships it. Do NOT confuse app version with MODEL_V.
6. **Don't touch:** the graded-event definitions in `engine.shadow_process` (except where a task
   explicitly says so), `store.py` event-sourcing semantics (append-only; corrections are new
   events), the ToS line (read-only, no automation, no trade-site scraping), the language lint
   (`util.fmt_*` — all user-facing numbers go through them).
7. **Every task ends with its Acceptance commands run and their expected output confirmed.**

Current constants you will reference: `MODEL_V = "m2"`, app v1.4.0. Signals:
DIP/MAKE/ROUTE/PARITY/PIN. Key files: `quant/models.py` (Kalman, OU, touch_prob, ratio, fees),
`quant/signals.py` (proposals), `quant/score.py` (calibration/scoring/gates), `quant/engine.py`
(poll pipeline, shadow book, sizing), `quant/bootstrap.py`, `quant/backtest.py`, `quant/store.py`.

---

# PHASE 0 — Truth & Throughput (no MODEL_V bump; independent tasks; do first)

These multiply how fast the app learns and how much it can prove, without touching any forecast.

### Task 1 — Shadow-book throughput + per-signal quotas
**Why:** Graded forecasts are the only food calibration eats. Field data: ROUTE monopolized the 8
shadow slots (406 forecasts) while DIP starved (7 in 17 days). Shadow forecasts are nearly free.
**Files:** `quant/config.py`, `quant/engine.py`, `tests/test_engine.py`.
**Steps:**
1. `config.py`: change `"shadow_cap": 8` → `24`.
2. `engine.py`, in the shadow allocation block (where `shadow_tracked` and `shadow_room` are
   computed, currently ~line 536-545): key `shadow_tracked` on `(sig, item)` tuples instead of
   bare `item` (three touch points: the set construction from `shadow["orders"]`/`shadow["pos"]`
   /`kept`, the membership test in the proposal loop, and the `.add(...)` calls).
3. Same block: compute `per_sig = {}` counts over `shadow["orders"] + shadow["pos"]` by `sig`;
   in the proposal loop, skip booking a shadow-only forecast when
   `per_sig.get(p["sig"], 0) >= adv["shadow_cap"] // 5 + 1` (5 = len of signal families in play);
   increment `per_sig` when you book one.
**Tests:** in `tests/test_engine.py` add `test_shadow_quota_prevents_signal_monopoly`: seed the
shadow kv with 20 open ROUTE orders (fabricate dicts with `sig:"ROUTE"`), run a poll that produces
a DIP proposal (reuse `dip_script()`), assert at least one new shadow entry with `sig=="DIP"`
exists afterward.
**Acceptance:** full suite green; the new test green.
**Do NOT:** change which proposals become *cards* (only shadow bookings), or touch grading.

### Task 2 — Feature freight: record everything cheap on every forecast + diagnostic reports
**Why:** Learning speed = labels/day × features/label. Features not recorded at forecast time are
unrecoverable. The ledger's `feat` dicts are currently write-only.
**Files:** `quant/engine.py`, `quant/signals.py`, `quant/sources.py` (read-only), `quant/score.py`,
`quant/ui.py`, tests.
**Steps:**
1. `engine.py` fetch loop (where `px/vol/fam` are built from each ninja payload): also build
   `tr7 = {}` from `d["trend"]` (parse_ninja already returns it; it is currently discarded).
   Add `"trend7": tr7.get(nm)` to every market row dict.
2. `engine.py` after routes are fetched: for items present in both feeds compute
   `src_gap_pct = abs(ninja_px - pair_ex_px) / ninja_px * 100` (pair ex price =
   `routes[item]["exalted"]["px_ex"]` when present); add `"src_gap_pct"` to the row.
3. `signals.py`: in each signal's `det` dict add `"trend7": row.get("trend7")` and
   `"src_gap_pct": row.get("src_gap_pct")` where a row exists (DIP/MAKE; ROUTE already has det —
   add the fields when `row` is not None).
4. `engine.py` `predict_write` payload: inside the `feat` value, merge
   `{"vol_div": round(p.get("vol_div") or 0), "hour": int(ts[11:13])}` on top of `p.get("det", {})`.
5. `score.py`: add two pure functions modeled on `model_reliability`:
   - `feature_reliability(graded)` → per signal, buckets over `feat` fields with hit/fill
     frequency and n: DIP `z_ou` (`< -2.5` vs `>= -2.5`), `drift_z` (`< -0.5`/`-0.5..0.5`/`> 0.5`),
     `vol_div` terciles computed from the rows themselves; suppress buckets with `n < 5`.
   - `fill_by_hour(graded)` → fill frequency + mean shown `p_fill` per UTC band
     (0-6/6-12/12-18/18-24) and weekday/weekend split, with n per bucket.
6. `engine.py` snapshot: add `"feature_rel": feature_reliability(graded30)` and
   `"fill_hours": fill_by_hour(graded30)` next to `"reliability"`.
7. `ui.py`: render both under the existing reliability panel in Engine room (same table style,
   hidden when empty — copy the `relwrap` pattern).
**Tests:** `test_score.py`: hand-build graded rows and assert bucket counts/suppression (`n<5`
bucket absent); `test_engine.py`: after a scripted poll, the newest prediction payload's `feat`
contains `trend7` (may be None), `vol_div`, `hour`.
**Do NOT:** use any of these features to change a probability or a gate. Diagnostic only.

### Task 3 — Backtest v2: full archive, real fees, parameter sweep, pair-book replay
**Why:** The archive is the only statistically meaningful sample the app owns (live DIP ≈ 7
forecasts/17d). Today the backtest sees only the 14-day live-tick window, charges a fake flat 3%
fee, and structurally cannot replay ROUTE (the class that burned us).
**Files:** `quant/backtest.py`, `quant/engine.py` (one line), `quant/main.py`, `quant/store.py`
(docstring only), tests.
**Steps:**
1. `engine.py`: where pair ticks are appended to `rows_t` (`(item, PAIR_SRC[...], d["px_ex"], None)`),
   put `d["trades"]` in the vol slot instead of `None`. Document in `store.py`'s module docstring:
   ticks.vol_div holds div/day value for ninja rows and TRADE COUNT for pair rows.
2. `backtest.py`: add `load_ticks(db_path, archive_dir)` that reads `data_archive/ticks-*.csv`
   with `csv.reader` (skip header; skip items starting with `"__"`), concatenates with the live
   `ticks` table, dedupes on `(ts,item,source)`, sorts by ts, and returns rows. Use it in `run()`.
3. `backtest.py`: replace every hardcoded `- 3` fee with
   `2 * fee_pct(px * qty_or_1, adv["fee_curve"]) + adv["slippage_pct"]` (import from models).
4. `backtest.py`: reconstruct a `routes` dict per poll from the latest `pairex`/`pairdiv` tick per
   item: `{item: {"exalted": {"px_ex": px, "trades": int(vol or 0), "value_ex": 0}, ...}}` and pass
   it to `propose_all` instead of `{}`. Grade fills against the ninja series only (as live does).
5. `backtest.py`: add `sweep(cfg, grid, db_path=None)`: grid is a dict of adv-key → list of
   values; for each combo, deep-copy `cfg["adv"]`, override, call `run(quiet=True)`, and print a
   table: combo | n | filled | hit | avg_ret | persistence, plus the same split by ISO week
   (overfit check: a combo that only wins in one week is suspect).
6. `main.py`: `--backtest-sweep` flag → a small built-in default grid over
   `dip_z ∈ {1.5, 1.8, 2.2}`, `idio_z ∈ {0.7, 1.0, 1.3}`, `dip_p_aim ∈ {0.6, 0.65, 0.7}`.
**Tests:** fixture CSV in `tests/fixtures/` with a synthetic archive month; `test_backtest.py`
(new file): `load_ticks` merges archive+db and skips `__BASKET__`-style rows; a 2-combo sweep
returns 2 distinct reports; a divergent pair book that never trades through yields ROUTE orders
with 0 fills.
**Do NOT:** let the backtest write to the live DB (it must open read-only paths / copy to temp).

### Task 4 — Grade the price event on unfilled forecasts (diagnostic partial credit)
**Why:** 4 of DIP's 7 forecasts died unfilled and taught the price model nothing. The price model
and fill model are separable; grade "did price reach target within H" on every forecast.
**Files:** `quant/engine.py`, `quant/score.py`, tests.
**Steps:**
1. `engine.py` `shadow_process`: when an order expires unfilled (the `fill_window_h` branch),
   instead of dropping after grading `{"filled": 0}`, append
   `{**o, "watch_until": <o["ts"] + H_h hours>}` to `shadow.setdefault("watch", [])` AND still
   grade the fill outcome — but move the `predict_grade` for these to the watch resolution:
   simplest correct implementation: grade `{"filled": 0}` only when the watch resolves, adding
   `"touch": 1|0` (any ninja tick ≥ target within H_h of `o["ts"]`) and `"mfe_pct"` (reuse the
   existing favorable-excursion scan). Keep `prediction_open`/model-tag guards exactly as in the
   position branch.
2. `score.py`: `calib_apply` must remain UNCHANGED in effect for these rows: it already only
   updates `hit` when `out["filled"]` is truthy — add a test locking that in.
3. `score.py`: add `model_touch_reliability(graded)` mirroring `model_reliability` but bucketing
   `p_model` against `out.get("touch", out.get("hit"))` over ALL rows (filled or not). Surface in
   the snapshot as `"touch_rel"` and render beside the reliability panel with the caption
   "diagnostic: price-only event, unconditional on fill — selection-biased easy, never sized".
**Tests:** `test_engine.py`: an order that never fills, whose item later crosses target within
H_h, grades `filled=0, touch=1`, and `calib["DIP"]["hit"]` counts are unchanged; one that never
crosses grades `touch=0`.
**Do NOT:** feed `touch` into any Beta used for sizing/display. It is a diagnostic.

---

# PHASE 1 — Forecast-math correctness batch (ONE MODEL_V bump at the end)

Order matters: land Tasks 5–10 with tests while `MODEL_V` stays `"m2"`; flip to `"m3"` only in
Task 11. Validate each via Task 3's backtest before merging (`run()` before/after; fills/hits must
move toward shown probabilities, or you revert).

### Task 5 — Stop feeding stale pair quotes to the Kalman filter as fresh observations
**Why (real bug):** `LiveIO.pairs` serves a cached dict for 55 minutes; the engine feeds those
prices to `kf_step` as new observations every 5-minute poll (~11× repeated), collapsing posterior
variance, dragging the latent level toward hour-old books, and poisoning `idio_z`.
**Files:** `quant/engine.py`, tests.
**Steps:** In `_poll`, BEFORE `store.insert_ticks` mutates the tick cache, compute
`changed = {(item, src) for each candidate obs where cache.get((item, src)) != px}` using the same
cache dict; build `obs_by_item` only from changed pairs (ninja obs change almost every poll and
flow through; unchanged pair repeats are skipped). Keep the insert_ticks call and its cache
mutation exactly where they are, AFTER the changed-set is computed.
**Tests:** `test_models`/`test_engine`: feed 11 polls of identical `pairex` price + moving ninja
prices through the poll path; assert final `P[0][0]` equals (±1e-9) a run with no pair source at
all; assert ninja observations were NOT skipped (level tracked the moving ninja price).
**Do NOT:** dedupe ninja observations that genuinely repeat AND drop their tick storage — tick
storage dedupe already exists and stays as is.

### Task 6 — Fix the Kalman process-variance estimator (rv measures noise, not volatility)
**Why (real bug):** `rv ← 0.97·rv + 0.03·y²` has fixed point ≈ `P00+R` — the observation-noise
level — not the item's per-hour variance; it's also updated once per SOURCE per poll and never
scaled by dt. Every `p_fill`, `fill_h`, MAKE quote width, and the `idio_z` fallback consume it.
**Files:** `quant/models.py`, tests.
**Steps:**
1. `kf_predict`: before adding `q*dt`, stash `st["_p00_noq"] = p00`.
2. `kf_update`: delete the `st["rv"] = ...` line.
3. `kf_step`: after the first update of the step (first source only), compute
   `st["rv"] = clamp(0.97*st["rv"] + 0.03*max((y*y - st["_p00_noq"] - R_used)/dt_h, 1e-8), 1e-6, 1e-2)`
   — you will need `kf_update` to return `(y, R)` or stash them on `st` for this.
**Tests:** `test_models.py::test_rv_tracks_true_volatility`: simulate a log random walk at 5-min
steps (dt=1/12) for 1000 steps with true `sigma_h ∈ {0.005, 0.05}` plus 1% obs noise; assert
`0.5*sigma_h < kf_sig_h(st) < 1.5*sigma_h` for both. (Current code fails both ends — verify that
first, then fix.)
**Do NOT:** change Q's structure here (the dt³ constant-velocity form remains future work).

### Task 7 — OU fit: small-sample bias correction + residual-based stationary sd
**Why:** OLS AR(1) is biased low (Kendall): `E[b̂]−b ≈ −(1+3b)/n`, which shortens `H = 3/κ` — part
of the graded event — for no real reason. And `sd_st` measured as dispersion around the median
absorbs trend, suppressing dip triggers exactly in trending leagues (observed: 7 DIP entries/17d).
**Files:** `quant/models.py`, tests.
**Steps:** in `fit_ou`: (1) after computing `b_hat` and before prior shrinkage, apply
`b_hat = clamp(b_hat + (1 + 3*b_hat)/n, 0.5, 0.999)`. (2) Compute residuals over the contiguous
pairs `e = x1 − (theta + b*(x0−theta))`; set `sig_h = RMS(e)`,
`sd_st = max(sig_h/sqrt(max(1−b*b, 1e-4)), <current dev2-based value as a floor>)`.
**Tests:** simulate OU b=0.97, 500 reps of n=100 pairs: mean fitted b within ±0.01 of truth
(document that pre-fix it centers near 0.93). Simulate OU + linear trend: `sd_st` within 1.3× the
true stationary sd (pre-fix ≈2×).
**Do NOT:** remove the contiguous-pairs gap handling or the median anchor.

### Task 8 — p_fill becomes evidence-weighted; align fill events with the grader
**Why (three lenses converged):** the per-signal fill Beta is updated on every graded order and
read by NOTHING — ROUTE showed p_fill≈0.95 while realizing 3% over 406 forecasts. Two event
mismatches compound it: MAKE's shown fill window is 2× the graded one, and DIP measures fill
distance from the latent level while the grader watches ninja ticks.
**Files:** `quant/signals.py`, `quant/engine.py`, tests.
**Steps:**
1. `signals.py`: add
   `def fill_blend(p_touch, fill_ab, prior=(7.0,3.0), k0=15.0): n_obs = max(fill_ab[0]+fill_ab[1]-(prior[0]+prior[1]), 0.0); return (n_obs*beta_mean(fill_ab) + k0*p_touch)/(n_obs + k0)`
   — evidence-weighted: with no graded fills it returns the touch model exactly; with ROUTE's
   field posterior it returns ≈0.05 regardless of the model.
2. Apply at every `p_fill` construction: DIP, MAKE, ROUTE, PARITY (replace the hardcoded 0.85),
   and PIN in `engine.py` (replace 0.9) — each already has `calib[sig]["fill"]` in reach.
3. Store the raw touch-model value as `"p_fill_model"` in each proposal and in the prediction
   payload (mirror the `p_hit`/`p_model` split).
4. Event alignment one-liners: MAKE `touch_prob(dist, sig_h, adv["fill_window_h"] * 2)` → drop the
   `* 2`; DIP `dist = 0.10*ou["sd_st"]` → `dist = max(math.log(row["px"]) - math.log(entry), 0.0)`
   (both fills are graded against ninja ticks crossing `entry_px` within `fill_window_h`).
5. Extend `model_reliability` to also bucket `p_fill_model` vs realized fills.
**Tests:** blend returns `p_touch` at the untouched prior; seed `calib["ROUTE"]["fill"]=[19,391]`
and assert every ROUTE proposal's `p_fill < 0.1` while `p_fill_model` stays ≈0.95; DIP with
`px` 3% above entry shows materially lower `p_fill` than px at entry.
**Do NOT:** flip MODEL_V here (Task 11 does).

### Task 9 — p_model upgraded to drifted first-passage
**Why:** the graded hit event is a *touch within H*; `p_model` is the endpoint marginal —
documented-conservative, and the field shows it (DIP pred 0.62, realized 3/3). A diagnostic too
coarse to show monotone resolution blocks the planned tilt forever.
**Files:** `quant/models.py`, `quant/signals.py`, tests.
**Steps:** add to models:
```python
def touch_prob_drift(d, mu, sig, T):
    # P(BM with drift mu, vol sig touches +d within T); exact reflection formula
    if d <= 0: return 0.95
    if sig <= 1e-9 or T <= 0: return 0.0
    a = clamp(2.0*mu*d/(sig*sig), -50.0, 50.0)
    return clamp(Phi((mu*T - d)/(sig*math.sqrt(T))) + math.exp(a)*Phi((-mu*T - d)/(sig*math.sqrt(T))), 0.0, 0.95)
```
In `dip()` OU branch: `mu = (mu_H - x0)/H`, `sig = sd_H/math.sqrt(H)`, `d = t_ln - x0`;
`p_model = clamp(touch_prob_drift(d, mu, sig, H), 0.05, 0.92)`.
**Tests:** at `mu=0` it equals `2*(1-Phi(d/(sig*sqrt(T))))` within 1e-9; it is ≥ the endpoint
marginal for `mu>0`; monotone in T.
**Do NOT:** touch displayed `p_hit` — this is the diagnostic only.

### Task 10 — Calibration half-life decay (track the regime, not the league average)
**Why:** posteriors accumulate lifetime counts, so early-league evidence outweighs the current
regime forever; div/ex tripled inside 17 days — the world a hit rate was measured in evaporates.
**Files:** `quant/score.py`, `quant/config.py`, `quant/engine.py`, tests.
**Steps:**
1. `config.py`: add `"calib_half_life_d": 14`.
2. `score.py`: `def decay_calib(calib, adv, days):` for each sig, with prior `(pa,pb)` from
   `adv["hit_prior"]` (fill prior `(7,3)`): `lam = 0.5 ** (days / adv["calib_half_life_d"])`;
   `a ← pa + (a-pa)*lam`, same for b, for both `hit` and `fill`; for Normal states
   `[m, v, n, M2]`: `n ← n0 + (n-n0)*lam`, `M2 ← M2*lam` with `n0` = the seeded pseudo-n.
   Decay must asymptote at the prior, never below.
3. `engine.py`: call it once per poll gated by a kv timestamp `calib_decay_ts` (copy the `ou_ts`
   pattern), passing elapsed days since last decay.
4. Append " · recency-weighted (½-life {N}d)" to the trust line so the display stays honest.
**Tests:** Beta [20,4] with prior [6,4] is exactly halfway back to prior after one half-life of
simulated elapsed time; repeated decay converges to the prior and never crosses it; a fresh grade
after heavy decay moves the posterior faster than it would have pre-decay.
**Do NOT:** decay the prediction ledger, gates state, or grad_points.

### Task 11 — The m3 flip (do LAST in Phase 1)
**Steps:** (1) confirm Tasks 5–10 merged and green; (2) run `python3 quant.py --backtest` and the
sweep, record before/after in the commit message; (3) set `MODEL_V = "m3"`; (4) full suite; the
existing migration tests must pass unchanged (they are parameterized on MODEL_V); (5) README note:
"m3: fill forecasts are now evidence-weighted and recency-weighted; calibration resets once,
DIP re-seeds from bootstrap"; (6) bump app version, commit, push.
**Acceptance:** on a copy of a real `quant.db`, one poll after the flip:
`kv calib_model == "m3"`, `calib_archive:m2` exists, gates reset, no exception in the poll log.

---

# PHASE 2 — Adaptation: regime state + the missing long signals (additive; no MODEL_V bump)

The field data's loudest fact: the league's entire profit was in trades the app cannot express
(hold div: 3×; hold the basket: 6.2×) while it said "Nothing worth your divines." These add the
missing signal classes as first-class, shadow-graded, gate-protected forecasts.

### Task 12 — Regime state (BULL/BEAR/CHOP), diagnostic first
**Files:** `quant/models.py`, `quant/engine.py`, tests.
**Steps:**
1. `models.py`: pure `def regime_update(prev, dt_h, idx_ret, div_drift_z, disp):` maintaining
   `{state, slope_ewma, streak, since_ts}`; EWMA of per-day index slope with 48h half-life; BULL
   when `slope_ewma > +1.0%/day or div_drift_z > +1.5` for ≥6 consecutive polls, BEAR mirrored,
   else CHOP; state changes only when the streak requirement is met (hysteresis).
2. `engine.py`: maintain a ROLLING volume-weighted index level in kv `regime_idx` (top-20 by
   volume, weights refreshed weekly; chain-link per-poll returns across weight changes — never
   compare levels across weight sets). Call `regime_update` after the factor block; persist to kv
   `regime`; add to the snapshot next to `market_z`; stamp `"regime": state` into every prediction
   payload's `feat`.
3. UI: one line in the status area: "regime: BULL for 9d (basket +2.1%/day)".
**Tests:** synthetic monotone index → BULL after exactly 6 polls, not 5; flat → CHOP; single-poll
spikes never flip state; chain-linking across a weight change produces no level jump.
**Do NOT:** gate anything on regime yet.

### Task 13 — TIDE: the denomination signal (hold divines when div/ex trends)
**Why:** div/ex 165→461 was the league's best trade; the app is structurally blind to it (MAJORS
are stripped from candidates). This was independently proposed by three lenses.
**Files:** `quant/signals.py`, `quant/score.py`, `quant/config.py`, `quant/engine.py`,
`quant/bootstrap.py`, tests.
**Steps:**
1. `score.py`: add `"TIDE"` to `SIGS`. `config.py`: `hit_prior["TIDE"] = [5,5]`,
   `horizon_h["TIDE"] = 72`, `"tide_drift_z": 1.5`, `"tide_target_pct": 5`.
2. `engine._load_calib_versioned`: after loading a same-version calib, run
   `for sig in SIGS: calib.setdefault(sig, {"hit": list(prior), "fill": [7.0, 3.0]})` so ADDING a
   signal never KeyErrors and never forces a reset. (This unblocks all of Phase 2.)
3. `signals.py`: `def tide(div_row, calib, adv):` trigger `div_row["drift_z"] >= adv["tide_drift_z"]`
   AND `(div_row.get("trend7") or 0) > 0`. Proposal: `sig="TIDE"`, `item="Divine Orb"`,
   `entry_px = px*1.01` (market-style: the shadow buy fills on the next tick since ticks ≤ level
   fill buys — no grading changes needed), `target_px = px*(1+tide_target_pct/100)`, `H_h=72`,
   `p_hit = beta_mean(calib["TIDE"]["hit"])`, gain/loss/EV via the same fee math as `dip`,
   `deterministic=False`.
4. `engine.py`: `propose_all` sees only `cand_rows` (MAJORS stripped) — pass `rows.get("Divine Orb")`
   explicitly and extend `propose_all`'s signature (`majors_rows=None`).
5. `engine.card_text`: a TIDE branch: "ROTATE: convert {N} ex → divines and hold — sell back when
   div/ex +{pct}% (or the trend breaks)". Sizing: normal `size_card` result but the card is about
   converting liquid ex; qty = spend/px as usual.
6. `bootstrap.py`: add `walk_trend(daily_map, adv)` beside `walk_daily`: over the Divine Orb daily
   series, event = "after trailing 3d return > +5%, the NEXT 3d return is also > +1%"; measure
   hit rate; extend `apply_priors` to seed `cal["TIDE"]["hit"]` with the same PSEUDO_N_CAP +
   Laplace treatment.
**Tests:** fake div row with drift_z 2.0 and trend7 +8 emits a TIDE proposal; shadow order fills
on the next tick and grades hit when div/ex crosses target; `walk_trend` on a synthetic 165→461
series reports persistent-positive with n>10; setdefault-migration adds TIDE to an existing m3
calib without touching other posteriors.
**Do NOT:** bump MODEL_V (additive signal, fresh ledger under the current tag).

### Task 14 — MOMO: per-item momentum, regime-gated, probation-gated
**Files:** `quant/signals.py`, `quant/score.py`, `quant/config.py`, `quant/engine.py`, tests.
**Steps:** `momo(row, calib, adv, regime)`: fire only when `regime == "BULL"`, `row["ou"]` exists,
`lvl > theta + 1.0*sd_st` (the anti-DIP), `drift_z >= 2.0`, `trend7 >= +10`. Entry market-style
(`px*1.01`), `target = px*(1 + clamp(2*sd_day_pct, 4, 15)/100)`, `H_h=48`, prior `[4,6]`
(skeptical). Probation: in `_load_calib_versioned`, when seeding a missing MOMO entry also seed
`gates["MOMO"] = {"off": True}` — the existing hit-calibration hysteresis un-gates it
automatically once ≥`gate_fill_min` shadow fills show calibrated hits. Wire `regime` into
`propose_all` (param), add SIGS/prior/horizon entries.
**Tests:** proposes only in BULL; starts gated (no card, still shadow-books via the Task-1 path);
un-gates after 8 synthetic graded fills at prior-consistent hit rate.
**Do NOT:** let MOMO fire in CHOP/BEAR even if drift is high.

### Task 15 — BASKET: make the index followable (advisory card, graded via a synthetic tick)
**Files:** `quant/engine.py`, `quant/signals.py`, `quant/score.py`, `quant/config.py`,
`quant/backtest.py`, tests.
**Steps:** each poll append one synthetic row `("__BASKET__", "ninja", 100*idx_level, tot_vol)` to
`rows_t` before `insert_ticks` (idx from Task 12's rolling index). `basket(idx_row, calib, adv,
regime)`: BULL-only, entry market-style, target +4%, `H_h=72`; the why/det carry the member list
+ weights so the card is executable by hand ("spread N ex across: X 40%, Y 25%…"). Grading needs
zero changes (crossed() reads ticks per item; `__BASKET__` now has them). Guards: `__BASKET__`
must never appear in cand_rows (it won't — rows are built from `px`), in `item_names`, or in
backtest tradables (Task 3's loader already skips `__`-prefixed). v1 is advice-only: the card has
no take-button wiring (`closeable`-style flag `advice_only: True`; UI hides the button).
**Tests:** synthetic tick written each poll; BASKET forecast grades hit when index ticks cross
target; `__BASKET__` absent from names/scan; no position fold ever contains it.

### Task 16 — Class-aware circuit breaker (halt reversion, not trend)
**Why:** `abs(market_z) >= circuit_z` halts ALL entries; in the mania it turned the app into a
spectator precisely when trend signals would have been in their element.
**Files:** `quant/signals.py` (constants), `quant/engine.py`, tests.
**Steps:** `REVERTING = {"DIP","MAKE"}`, `TREND = {"MOMO","TIDE","BASKET"}`. In `_poll`:
`blocked = everything` when no rate; `REVERTING | TREND` when `market_z <= -circuit_z` (crash);
`REVERTING` when `market_z >= +circuit_z` (mania); else `set()`. Replace the boolean check at the
card-creation site with `p["sig"] not in blocked` (ROUTE/PARITY governance unchanged). Update
`entries_reason` to name the class and the reason ("mean-reversion paused: market +2.7σ above its
anchor; trend signals active"). Shadow booking stays unconditional (verify by test).
**Tests:** `market_z=+3`: DIP blocked from cards but still shadow-books; a MOMO proposal (BULL,
ungated) still becomes a card. `market_z=-3`: both classes blocked.

### Task 17 — Regime-aware idle copy (never "nothing worth your divines" in a measured trend)
**Files:** `quant/engine.py`, `quant/ui.py`, tests.
**Steps:** when regime is BULL (streak ≥ 6 polls) and there are no entry cards, the no_trade line
becomes: "Market regime: BULL — basket {slope}%/day, div/ex drift z={z}. Idle exalted is losing to
the trend; TIDE/BASKET running in shadow (measured hit so far: {beta or 'collecting'})." All
numbers are measurements or graded frequencies — label the diagnostics as such. CHOP keeps the
current line verbatim.
**Tests:** synthetic bull snapshot with zero cards contains the regime line and NOT the bare
"Nothing worth your divines"; CHOP keeps the original string.

---

# PHASE 3 — Sharpening (each gated on evidence from Phase 0's reports)

- **Task 18 — Per-branch DIP Betas + reliability-gated logit tilt.** Tag `branch: ou|d14|m24` in
  DIP's det; `calib_apply` co-updates `hit_<branch>`; display uses the branch Beta once it has
  ≥12 graded fills. Tilt: `w = clamp(WLS slope of freq-vs-p_mean over reliability buckets with
  n≥8, 0, 1)`; `p_shown = inv_logit(logit(p_base) + w*(logit(p_model) − logit(mean p_model)))`,
  so `w=0 ⇒ p_shown == p_base` exactly. MODEL_V bump (m4) when it ships. **Precondition:**
  reliability buckets monotone over ≥50 closed DIPs (Task 2/4 make that reachable).
- **Task 19 — EV-optimized target quantile.** Replace fixed `dip_p_aim=0.65` with a 7-point scan
  `q ∈ 0.55..0.85` choosing `argmax q·gain_q − (1−q)·loss_q` (model probs for PLACEMENT only;
  shown p_hit stays calibrated). Same m4 bump. **Precondition:** Task 3 sweep shows the fixed
  quantile is off-optimum on the archive.
- **Task 20 — Seasonality-scaled fill horizon.** If Task 2's `fill_by_hour` shows bands with
  n≥20 differing materially, scale `touch_prob`'s effective horizon by the bar-activity profile
  `lambda[hour]` (bars.n is stored and never read). Batch into m4.
- **Task 21 — Conditioning splits (tier/regime posteriors).** ONLY if `feature_reliability` /
  regime-stamped ledger shows clear separation with n≥20 per bucket. Until then the stamps stay
  diagnostic. (Deliberately demoted: splitting ~7 forecasts/17d across buckets is noise-worship.)
- **Task 22 — Cross-source gap veto for DIP.** If the recorded `src_gap_pct` feature (Task 2)
  predicts fill/hit misses, add `adv["src_gap_veto_pct"]` rejecting DIP entries where the "dip" is
  invisible to the pair book. No bump (changes which proposals exist, not forecast math).

---

# NEW FEATURES — toward "rich in PoE2" (product, not estimator math)

1. **The Rotation Advisor (TIDE/BASKET/MOMO cards)** — Phase 2 IS the headline feature: the app
   finally has an opinion during manias, when most of the money is made. Expected impact is
   larger than every estimator fix combined (field data: 3–6× moves went unadvised for 17 days).
2. **Regime banner + idle-cash counsel** (Task 17) — the cheapest wealth feature: telling you what
   your idle exalted is losing to, with measured numbers, instead of "nothing to do".
3. **Wealth curve & weekly report card.** A "snapshot my wealth" button (re-uses holdings_set)
   plus a weekly auto-summary in Engine room: net worth vs all three benchmarks, per-signal graded
   record, best/worst trade, regime timeline, and one plain-language paragraph ("You beat holding
   ex by +12% but lost to the basket; TIDE would have added +9% — it un-gates next week if it
   keeps hitting"). Exportable via `--export`.
4. **League-start mode.** Day 0–3 of a league is the highest-edge window (chaotic pricing, violent
   trends). Auto-detected from league age (poe2scout league list): poll every 2 min instead of 5,
   run bootstrap immediately as history accrues, widen `shadow_cap`, and show a "league-start:
   collecting aggressively" banner. No strategy change — just faster learning exactly when data
   is richest.
5. **Recipe Lab.** PARITY currently reads recipes from config JSON. Add an Engine-room editor:
   list recipes with live cost/value/net-edge per the current poll, add/verify/disable without
   editing files, and a "record one manual test" flow that stamps `verified: true`. More verified
   recipes = more true-arb coverage, the highest-quality edge class the app has.
6. **Opportunity-cost line on HOLD cards.** HOLD cards show "exit at X, 40% of the way" — add
   "…or rotate: selling now funds {best current candidate} (EV {y}%/day vs this hold's {z}%/day)"
   using the profit-rate math sizing already computes. Makes capital velocity visible.
7. **Gold budget awareness.** A manual gold field on the Capital form; each card then shows its
   gold cost (fee curve × value) against your balance and warns when gold, not exalted, is the
   binding constraint. (The fee curve exists; this makes its second resource visible.)
8. **What NOT to build** (considered, rejected): per-item posteriors and ML models (sample sizes
   forbid — the ledger + shrinkage IS the right amount of statistics); sub-hour momentum
   (listing medians can't support it); live-listing snipe alerts or any trade-site polling
   (ToS bright line); auto-execution of any kind (the read-only line is the product's license).

---

## Execution order & risk summary

| Order | Tasks | MODEL_V | Risk | Payoff |
|---|---|---|---|---|
| 1 | 1–4 (Phase 0) | none | minimal, diagnostic/offline | 3–5× labels/day; the evidence base for everything else |
| 2 | 5–11 (Phase 1) | one bump m2→m3 | medium, but each step backtest-validated + one reset | shown probabilities finally converge to reality; honest fill odds |
| 3 | 12–17 (Phase 2) | none | new signals start gated/shadow-only | the app can profit from the regime that beat it |
| 4 | 18–22 (Phase 3) | m4 when earned | gated on measured evidence | per-card sharpness beyond pooled rates |

The through-line: **Phase 0 makes the app learn faster, Phase 1 makes what it shows true, Phase 2
makes it adapt to the market that actually exists, Phase 3 is earned sharpness.** Every task keeps
the founding rule intact: the app must be able to prove, from its own graded ledger, whether it
works.
