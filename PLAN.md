# QUANT v1.0 — plan, v2 (supersedes v1; git history has it)

Two pinnacles, jointly optimized, in tension everywhere, resolved explicitly:

> **Prediction:** every number the app shows is a *calibrated probabilistic forecast*, extracted at
> the limit of what this data can support, scored against outcomes, and never sharper than the data
> allows.
>
> **UX-simplicity:** open the app → read one sentence → act (or close it). Zero required
> configuration, zero mental math, zero jargon on the surface.

The tension resolves by layering: a plain sentence on top, the full distribution one tap below.
The synthesis of both goals is the **graduation rule** (§13): the app itself tells you, from its
own scored forecasts, when it has earned real divines — and until then it says it hasn't.

---

## Part I — Pinnacle of prediction

### 1. Model the truth, not the feed (latent-price filter)
Aggregator numbers are noisy, lagged observations of listing medians — not trades, not the price.
v1.0 treats them that way: per item, a scalar Kalman filter over log-price with the two sources
(poe.ninja, poe2scout pair-implied) as separate observations, each with its own fitted noise and
staleness. Output: a latent price *with uncertainty* and a drift (velocity) estimate.
- The knife-guard becomes principled: don't buy when latent drift is significantly negative —
  replacing the fixed "7d > −12%" rule.
- Cross-source disagreement stops being a quarantine hack; it just widens the posterior, and wide
  posteriors fail the entry test on their own.
- **Schema consequence (P0):** ticks store each source's raw value separately
  (`ts, item, source, price, vol`), or the measurement model can never be fitted. 5-min ticks kept
  14 days for fill/latency modeling; hourly bars kept forever for the slow models. Irregular
  sampling (laptop sleep) is handled natively — every estimator is Δt-aware, no gap patching.

### 2. Mean reversion as a real model (OU), not a folk constant
DIP currently assumes "70% of the gap closes." v1.0 fits an Ornstein–Uhlenbeck process per item
(reversion speed κ, level θ, vol σ — discrete AR(1) form, method-of-moments, shrunk across items;
~50 lines of stdlib math). That buys, in closed form:
- the full distribution of price at horizon H — so "edge" becomes E[profit] *with* a credible
  interval, and P(target reached by H) is computed from the marginal at H, which **under**-states
  first-passage probability — the conservative direction, on purpose;
- a principled horizon: items with slow κ aren't dips, they're drifts, and get rejected;
- ABANDON thresholds from each item's own σ instead of a flat −12%.

### 3. Separate "this is cheap" from "everything is cheap" (factor structure)
Items co-move: families (essences together) and a market-wide factor. A one-factor model — a
liquidity-weighted index of the top-20 traded items (the "PoE2 CPI") plus shrunk per-item beta —
splits every deviation into market, family, and idiosyncratic parts. Only the **idiosyncratic**
dip is a mean-reversion trade; a market-wide dip is a regime event and trips the circuit breaker
instead. The same index becomes the third benchmark (§12) and the league-inflation deflator.

### 4. Hierarchical Bayesian calibration, conjugate only
Every learned quantity lives in a shrinkage hierarchy: global → signal class → liquidity tier →
item. Conjugate forms only (Beta-Binomial for fill/hit probabilities, Normal–Inverse-Gamma for
edges), so updates are closed-form, transparent, and overfitting-resistant at these sample sizes.
Today's magic constants (0.7 reversion, 6% spread capture, 1% fee) are demoted to *priors*; data
moves you off them at the rate the data earns. No ML anywhere — at this volume, shrinkage
estimators with honest uncertainty are not the compromise, they're the optimum.

### 5. Forecast time, not just direction
Capital is the scarce resource, so time-to-fill is a first-class forecast: P(order at price p
fills within T), fitted from tick history ("minutes until level traded through" vs distance-from-
mid and volume), Beta-Binomial calibrated. Cards rank by **expected profit per unit of
capital-time**, not raw edge — a +5% that fills in 2 hours beats a +8% that takes 3 days.

### 6. True arbitrage outranks statistics
Two signal classes carry no price-model risk and therefore sit above everything probabilistic:
- **Conversion parity:** PoE2's deterministic recipes (3:1 emotion instilling, essence tier-ups —
  a config list, verified in-game once). Exchange price violating a conversion identity beyond
  fees = arbitrage with execution risk only.
- **Exchange-graph cycles:** generalize ROUTE via negative-cycle search (Bellman–Ford on −log
  price) over the full SnapshotPairs graph, ≤3 hops, fees per hop. Strict superset of the current
  hand-coded ex-vs-div shape, same data.
Prediction still applies to their *execution* (fill-time forecasts, stale-data discount), not
their edge.

### 7. The execution model is part of the forecast
A forecast of profit is a forecast of *executed* profit:
- real gold-fee curve (measured in-game once, config data) instead of flat 1%/side;
- integer-ratio discreteness: edge math runs on the nearest representable in-game ratio, and the
  card prints the literal order ("set 2 → 7 ex = 3.50 each") — phantom edges on cheap items die
  principled deaths;
- exits anchored to the thesis (OU mean, route convergence), never to your cost basis — the
  current `avg_cost + fees + edge` target is the disposition effect, encoded.

### 8. Scoring: proper or it didn't happen
Every card writes a **prediction record** before the outcome is knowable: feature vector, model
version, full predictive distribution. Outcomes are graded with proper scoring rules (Brier for
binaries, CRPS for distributions — closed form for Gaussians), decomposed into calibration and
sharpness. Hit rate alone is gameable; proper scores are not. The shadow book (§9) supplies the
sample size; the prediction ledger makes every forecast reproducible and auditable after the fact.

### 9. The shadow book and the validation ladder
Every card that goes ACTIVE opens a virtual position regardless of whether you take it, filled
only when the market *trades through* the price plus modeled latency (paper mode uses the same
realistic fill engine — no more instant-fill fiction). A human takes 2 trades a day; the shadow
book grades 20+ forecasts a week. Changes climb a fixed ladder: **walk-forward replay** on the
tick DB (embargoed, no peeking) → **live shadow A/B** (old and new variant side by side) →
default-on. Nothing ships on vibes; vibes are the failure mode this app exists to prevent.

### 10. Know the floor
The pinnacle of prediction includes refusing to predict. Per item: a noise floor (listing-median
bounce variance) below which "edge" is indistinguishable from noise — entries require posterior
P(edge > costs) ≥ a threshold, not point-estimate edge > costs. Per signal: minimum detectable
edge at current sample size, shown honestly ("DIP needs ~30 more graded calls before its edge is
distinguishable from zero"). Every model must beat persistence (last price) *and* the v0.4
heuristics out of sample, or it auto-gates off, visibly. **NO EDGE is a finding, the way NO TRADE
is advice.**

### 11. Sizing is a forecast consumer
Fractional Kelly computed on the posterior predictive (parameter uncertainty shrinks the fraction
automatically — under-betting is the only safe error), hard-capped by % of daily volume and the
liquid reserve. Correlated candidates (family/factor groups) share one budget and one slot: three
essence dips are one bet. The card explains its own size in one clause.

---

## Part II — Pinnacle of UX-simplicity

### 12. The whole app is one question
Default screen, top to bottom, and nothing else:
1. **Header strip:** net worth + delta vs the honest benchmark basket (hold-div / hold-ex /
   market index — worst of the three is the headline), one freshness dot (green/amber/red; errors
   live behind it), paper/real chip.
2. **Trust line (one sentence):** "Advice beat holding by +6.2% over 30d · 41 graded calls ·
   details ▸". The full scoreboard — per-signal proper scores, reliability curves, auto-gated
   signals — is one tap deeper. "Should I believe you?" is answerable in two seconds.
3. **The NOW list:** at most 3 cards, priority-ordered, top one visually dominant. Exits always
   outrank entries. When empty, the flagship state (below).
Everything else — scan table, trades, capital, diagnostics, settings — lives behind two collapsed
disclosures: **Record ▸** and **Engine room ▸**. The default view is phone-perfect by construction
(one column), served over LAN with a startup token.

### 13. Zero required configuration, and graduation the app announces
First run: paper mode, conservative preset, league auto-resolved, a 5-div notional bankroll — the
app is fully functional in under 60 seconds with **no questions asked**. One optional prompt strip:
"Tell me what you actually hold and I'll size for you." The only other controls that exist: a
risk dial (conservative/standard/aggressive) and the paper/real toggle — and the toggle carries
its own criterion, computed from scored forecasts: *"Paper: proving itself — 9 days in, +6.2% vs
holding; recommends real when ≥14 days and the credible interval clears zero."* The app never asks
a question it can answer itself, and never offers real-money mode it hasn't statistically earned.
Power users get `config.advanced.json`; nobody else ever sees a knob. (15 risk knobs → 0 required,
2 optional.)

### 14. Anatomy of a card: complete, literal, two-layered
- **Line 1 — the action, fully executable:** "BUY 14× Greater Essence of Haste — set **7 ex → 2**
  (3.5 ex each, ≈0.04 div)". Exact in-game ratio, both denominations, aggressive rounding. Zero
  mental math, ever.
- **Line 2 — the plan and the odds, in plain odds:** "then sell at 4.1 ex — trades like this
  worked **7 in 10** times; expected **+9 ex**, usually done within a day."
- **Line 3 — why, one clause:** "unusually cheap vs its own normal range, market steady" +
  **details ▸** opening the full trace: latent price ± σ, OU parameters, calibrated coefficient
  with sample size, fee curve, ratio rounding, model version. Statistics live one tap down,
  always available, never in the way.
- **One button.** Paper: logs the modeled fill, toast with undo. Real: two prefilled fields
  (qty, price), confirm. The edit/delete trades table survives under Record ▸ for corrections.

### 15. NO TRADE is the flagship state
It's the most common screen, so it's designed, not apologized for: "**Nothing worth your divines
right now.** 612 items checked 3 min ago. Closest miss: Greater Essence of Haste — needs to dip
2% more." The closest-miss line is honest, teaches what the tool looks for, and kills the itch to
go find action elsewhere. Entries never notify — the next dip always comes; anti-FOMO is a
feature. SELL/ABANDON *do* notify (browser notification), because exits are the only
time-sensitive thing the app ever says. Returning after hours away shows a one-paragraph debrief:
what fired, what expired (with reasons), what the shadow book did.

### 16. Language policy
Surface text contains no z-scores, no sigmas, no acronyms, no 4-decimal prices. Probabilities are
"7 in 10", times are "usually within a day", magnitudes carry both currencies. Numbers sharper
than the model's own uncertainty are forbidden by lint — the UI is not allowed to imply precision
the posterior doesn't have. (UX-simplicity and prediction honesty are the same rule here.)

---

## Part III — Substrate and discipline (unchanged in spirit, re-scoped)

- **Stdlib-only stays sacred**; `python quant.py` forever (package + shim). Layout: `sources/`
  (validated adapters + recorded fixtures), `store` (WAL, busy_timeout, append-only event ledger —
  fills/edits/card transitions as events, state as folds; mutable rows can currently corrupt
  derived positions), `filter` (Kalman/OU), `features` (factors, index), `signals/` (registry),
  `risk`, `cards` (state machine: CANDIDATE→ACTIVE→TAKEN|EXPIRED(reason)|STOPPED, with
  hysteresis), `shadow`, `score` (prediction ledger + proper scoring), `server` + `ui/`,
  `doctor`, `backtest`.
- **Tests match the stakes:** golden fixtures per endpoint, property tests on ledger folds (no
  event sequence yields negative qty or NaN), snapshot tests on card text, calibration tests on
  the scorers themselves.
- **Ops:** single-instance lock, per-source health behind the freshness dot, `--doctor` (APIs, DB
  integrity, calibration freshness), polite request rates and identifying UA unchanged.
- **Non-goals, still rejected on purpose:** automation of any kind (the read-only line is the
  license to exist); official trade-site polling; ML; full unique scanning (no traded-volume data
  → gates can't work; uniques remain pinned manual theses, unified into the card pipeline).

## Build order (each phase ships usable)
- **P0 — Substrate:** package split, event + prediction ledgers, per-source tick schema, WAL,
  fixtures, walk-forward harness skeleton. No behavior change; everything after this is
  measurable.
- **P1 — Truth loop + the NOW interface:** card state machine, shadow book, realistic fills,
  proper scoring, trust line + scoreboard, NO TRADE state, one-tap logging, zero-config first
  run. (Cards' lifecycle and the one-list UI are one feature; they land together.) After P1 the
  app grades itself and reads like the final product.
- **P2 — The prediction engine:** latent filter, OU, factor model, hierarchical calibration,
  fill-time forecasts, profit-rate ranking; parity + cycle arbitrage. Each climbs the validation
  ladder before default-on.
- **P3 — Sizing & polish:** posterior Kelly + correlation budgets, graduation rule, risk dial,
  notifications, debrief, LAN token mode, language lint.

## Acceptance — "pinnacle" means
**Prediction:** every surfaced number traces to a stored, reproducible forecast; reliability
curves are flat within their credible bands; every live model beats persistence and the v0.4
heuristics walk-forward or is auto-gated off; probabilities on cards match realized frequencies
("7 in 10" means 7 in 10); the app states its own detectability limits.
**UX:** first run to useful screen in <60s with zero questions; any action executable from one
card with zero mental math; "what should I do and why should I believe you" answerable from the
top screen in <10 seconds; ≤3 interactive decisions visible by default; the most common screen
(NO TRADE) is the best-designed one.
**Both at once:** real-money mode is recommended by the app only when its own scored forecasts
clear the graduation bar — and if the edge never materializes, the front page says so plainly,
with the evidence. That sentence is the product.
