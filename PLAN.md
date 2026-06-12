# QUANT v1.0 — the self-grading trader (plan)

## What QUANT is, distilled
A read-only co-pilot for hand-trading PoE2 currency: poll public aggregators, find edges, pass them
through safety gates, and say in plain language what to do — or say NO TRADE. The constraints that
define it are features, not debts: no game interaction (bright ToS line), stdlib-only zero-setup,
small-bankroll realism, and honesty about what the numbers mean. v1.0 keeps every one of these.

## The diagnosis, in one sentence
v0.4 generates advice from **assumed constants** — 70% dip reversion, 6% capturable spread, 1% flat
fees, fixed ±% triggers — and **never checks whether its advice was right**, so it cannot improve,
and you cannot rationally decide to trust it.

## The organizing principle
A decision-support tool has exactly one quality metric: *does following the cards beat holding,
after costs?* So the defining design move of v1.0 is:

> **Every card is a falsifiable forecast. Record it, mark it to market, grade it, feed the grade
> back into the model.**

Everything below either serves that loop or makes it safe to build. The best version of this app is
the version that can *prove* whether it works — and that includes proudly reporting "no edge
detectable" if that's the truth. NO EDGE is a finding, the way NO TRADE is advice.

---

## A. Close the loop (the heart of v1.0)

**A1. Card lifecycle state machine.** Cards get persistent identity and explicit states:
`CANDIDATE → ACTIVE → TAKEN | EXPIRED(reason) | STOPPED`. Hysteresis (enter at the gate, drop only
2% below it) ends the current flicker where marginal candidates swap out every poll. Every
transition is an event in the ledger — expiry reasons included ("price reverted before entry — a
missed trade costs nothing"). This solves the README's pass-2 item and is the substrate for A2.

**A2. The shadow book.** Every card that goes ACTIVE opens a *virtual position*, whether or not you
take it. Virtual positions are marked and closed by the same exit logic as real ones. A human takes
maybe 2 trades a day; the shadow book grades 20+ forecasts a week from day one. This is the quant
move: you don't need to trade to measure your alpha — you need to record your forecasts and mark
them honestly.

**A3. Realistic fills (shadow *and* paper).** Today paper fills execute instantly at the card price
— optimistic fiction. v1.0: a paper/shadow order rests until the tick history shows the market
*traded through* the price, plus a latency penalty drawn from a fitted time-to-fill model
(empirical distribution of "minutes until level crossed" conditioned on distance-from-mid and
div/day volume — fittable from the tick DB we already collect). Result: paper P&L becomes a
conservative estimate of achievable P&L, and "paper curve beats holding → go real" becomes a
decision you can actually trust.

**A4. Self-calibration.** A weekly job replaces the magic constants with fitted values, per
liquidity tier, using hierarchical shrinkage toward the current constants as priors (tiny samples
must move you off the prior slowly, not whipsaw):
- realized reversion fraction → replaces the hardcoded `0.7` in DIP;
- realized spread capture → replaces `spread_capture_pct`;
- realized vs predicted ROUTE convergence;
- time-to-fill curve (feeds A3 and B5).
Confidence labels get graded too (Brier-style): if HIGH cards don't beat MED cards, the dashboard
says the labels are noise. No ML anywhere — at these sample sizes, shrinkage estimators with honest
uncertainty are the *correct* amount of statistics.

**A5. The scoreboard, front and center.** The headline panel becomes: cards shown / taken / hit
rate; shadow-book alpha vs benchmarks (see E1) with uncertainty and sample sizes; per-signal
performance. Signals whose calibrated edge ≤ fees get **auto-gated off**, visibly, with the
evidence. A tool that displays its own batting average is a tool you can rationally trust — for a
decision-support product, this *is* the product.

## B. Sharper signals

**B1. Volatility-normalize everything.** Fixed-% triggers treat a placid item and a noisy item
identically — wrong in both directions. DIP triggers on z-score (deviation ÷ EWMA σ from tick
history), ABANDON triggers on a vol-scaled drawdown instead of a flat −12%. The 14-day league
z-score (already built in v0.4) stays as the longer-horizon check.

**B2. ROUTE generalizes to exchange-graph cycle search.** `SnapshotPairs` already contains *every*
in-game pair with volume. Instead of the hand-coded "ex-pair vs div-pair" shape, run negative-cycle
detection (Bellman–Ford on −log price) over the volume-filtered pair graph, ≤3 hops, fees charged
per hop, the existing sanity checks retained. Same data, strict superset of today's ROUTE, finds
chains a human staring at the exchange won't.

**B3. New signal class: conversion parity (true arbitrage).** PoE2 has deterministic conversions
(3:1 distilled-emotion instilling, essence tier-ups, etc. — the recipe list is *config data*,
verified in-game once). Where exchange prices violate a conversion identity by more than fees, the
edge carries no price risk, only execution risk. True arb structurally outranks statistical edge;
it becomes signal #1 in priority. The scanner holds a small conversion graph and monitors parity.

**B4. Regime awareness.** League age becomes a feature — a day-3 z-score and a day-60 z-score mean
different things, and the daily-history table already spans the league. Weekend/weekday seasonality
from the same table. A market-wide dispersion circuit breaker: when cross-sectional volatility
spikes (patch day, league event), entries switch off and a banner says why. Mean reversion assumes
a regime; the tool should know when the assumption is off.

**B5. Rank by profit *rate*, not edge.** Capital is the scarce resource: a +5% edge that fills in
2 hours beats a +8% edge that takes 3 days. Ranking key becomes expected profit ÷ expected
time-to-fill (denominator from the A3/A4 latency model), replacing today's `edge × volume` proxy.

## C. Honest execution model

**C1. Real fee curve.** Replace the flat `1%/side` guess with the exchange's actual gold-fee
structure as a measured config curve (one in-game calibration session). Gold is a real budget; the
dashboard at least prices it correctly even if it can't see your balance.

**C2. Ratio discreteness.** The in-game exchange takes integer ratios, so achievable prices are
rationals — for cheap items the effective tick is enormous, which is exactly the lot-size
distortion the current ±40% ROUTE hack papers over. v1.0 computes the nearest representable ratio,
runs the edge math on *that*, and prints the literal order to type in-game: **"set 2 → 7 ex
(3.50 ex each)"**. Phantom edges on sub-exalt items die principled deaths, and every card becomes
copy-paste executable.

**C3. Exits anchored to the market, not your cost.** Today's SELL target is
`avg_cost × (1 + edge + fees)` — the textbook disposition effect, encoded. v1.0 exits when the
*thesis completes* (DIP: price back at mean; MAKE: second leg; ROUTE/parity: convergence) or when a
strictly better card needs the slot (opportunity-cost rotation). Cards explain the anchor: "target
= 24h mean, where the dip thesis is done." Your entry price is sunk; the market doesn't know it.

## D. Risk as one rule instead of fifteen knobs

**D1. Fractional Kelly sizing.** Position size = ¼-Kelly on the *calibrated* edge distribution,
hard-capped by the existing guardrails (% of daily volume, liquid reserve). One principled rule
replaces `max_bankroll_pct` + `min_profit_ex` + friends, and each card explains its own size:
"¼-Kelly at your bankroll → 1.2 div, capped by 2% of daily volume."

**D2. Correlation groups.** All essences dip together: three essence DIPs are one bet, not three.
Candidates sharing a group (type-level to start, correlation-measured from tick history later)
share one budget and one position slot.

**D3. Config collapses to three user-facing settings** — bankroll, risk preset
(conservative / standard / aggressive), paper/real — because the target user said it themselves:
they aren't qualified to tune 15 interacting thresholds, and after A4 most knobs are *outputs* of
calibration, not inputs. The full knob file remains as an advanced override.

## E. Product surface

**E1. Three benchmarks, not one.** "Hold divines" is flat in div terms by definition while div
itself drifts against the market — today's ribbon can book denomination drift as alpha. v1.0 tracks
net worth vs hold-div, hold-ex, and a liquidity-weighted basket of the top-20 traded items (a PoE2
"CPI", which also feeds B4's regime detection). Alpha is only alpha if it beats all three.

**E2. Every number explains itself.** Card expansion reveals the full reasoning trace: price
source and age, σ and sample size, calibrated coefficient (with how much data backs it), fee curve
applied, ratio rounding applied. Trust comes from auditability, not confidence theater.

**E3. Notifications with a point of view.** Browser notifications for SELL/ABANDON only — exits
are time-sensitive. Entries never notify, and the UI says why: the next dip always comes;
anti-FOMO is a feature. A session debrief on return: what changed, what expired (with reasons),
what the shadow book did while you were away.

**E4. One product, not two.** The JSX artifact predates the scanner and splits the maintenance
budget; it freezes (header comment pointing here). For couch/phone use the local server gains
`--host` LAN binding behind a random token printed at startup. The existing visual identity (the
parchment-terminal look, the tone of the copy) is good product design — it stays.

## F. Substrate (what makes all of the above safe to build)

- **Package split, stdlib-only stays sacred.** `quant/` package with `python -m quant` and a
  `quant.py` shim so the run command never changes. Layout: `sources/` (one adapter per upstream,
  validated against schema contracts, with recorded JSON fixtures), `store` (SQLite: WAL +
  busy_timeout — today's setup can hit "database is locked" between poller and UI threads),
  `features`, `signals/` (registry: a Signal = `propose(market, history) → [Proposal]`, so signal
  #4 is additive and the scoreboard grades per-signal), `risk`, `cards` (state machine), `shadow`,
  `calibrate`, `server` + `ui/`, `probe`, `backtest`.
- **Event-sourced ledger.** Fills, edits, deletes, holdings updates, card transitions: append-only
  events; positions and baselines are folds over the log. Today's mutable rows under derived state
  are fragile — deleting a buy whose sell remains drives quantities negative and silently poisons
  cost math. With events, corrections are new events and history is replayable.
- **Tests that match the stakes.** Golden fixtures per endpoint (recorded from `--probe`); property
  tests on ledger folds (no event sequence yields negative qty or NaN net worth); snapshot tests on
  card generation. A tool that sizes positions deserves a test suite.
- **Backtest mode.** `--backtest` replays the tick DB through any signal configuration. Plus live
  shadow A/B: run the old and new variant of a signal side by side in the shadow book. Every
  strategy tweak gets evidence before it ships — tuning by vibes is the exact failure mode this
  app exists to prevent.
- **Ops hygiene.** Single-instance lock; ticks downsample to hourly bars after 48h (DB stays small
  forever); per-source health chips with cross-source quarantine (ninja vs scout disagree >x% on an
  item → price quarantined instead of carded); `--probe` grows into `--doctor` (APIs + DB integrity
  + config sanity + calibration freshness).

## Non-goals (considered and rejected, on purpose)
- **Automation of any kind** — the read-only line is the product's license to exist.
- **Official trade-site polling** — ToS-gray and impolite; two public aggregators suffice.
- **Machine learning** — the data volume earns shrinkage estimators, nothing more. Revisit only if
  the shadow book someday disagrees.
- **Full unique-item scanning** — listing counts aren't traded volume, so the safety gates can't do
  their job there. Uniques remain manual-thesis territory: `plays` survives, but unified into the
  card pipeline as "pinned candidates with manual thresholds" (one pipeline, two candidate sources,
  ~150 lines and a class of mismatch bugs deleted).

## Build order (each phase ships usable)
1. **P0 — Substrate.** Package split, event ledger, WAL, fixtures + tests, backtest skeleton.
   No behavior change. Rationale: everything after this gets measured, and nothing before P1 can be.
2. **P1 — Truth.** Card state machine, shadow book, realistic fills, calibration job, scoreboard.
   After P1 the tool grades itself; this is the moment v1.0 exists in spirit.
3. **P2 — Alpha.** Vol-normalized triggers, cycle search, conversion parity, regimes, profit-rate
   ranking — each validated by backtest + shadow A/B before it earns default-on.
4. **P3 — Risk & polish.** Kelly sizing, correlation groups, 3-knob config, explanations,
   notifications, debrief, LAN mode.

## Acceptance: "best version" means
1. The dashboard answers *"should I trust you?"* with calibrated per-signal hit rates and
   shadow-book alpha vs three benchmarks — uncertainty and sample sizes included.
2. Paper P&L is demonstrably conservative relative to realized outcomes on taken cards.
3. A signal idea goes from sketch → backtest → live shadow A/B → ship in one evening, with zero
   real divines at risk along the way.
4. Setup is still: clone, `python quant.py`. Nothing else. Ever.
5. If the edge isn't there, the front page says so, plainly, with the data that proves it.
