# QUANT v1.0 — PoE2 currency decision-support that grades itself

A **read-only** co-pilot: it watches the market and tells you the move; you trade by hand in-game.
Nothing here automates gameplay or trading (that would breach GGG's terms — price monitoring does
not). Python 3.10+, **stdlib only, zero pip installs, zero required configuration.**

```
python quant.py            # serves http://localhost:8377 — that's the whole setup
python quant.py --doctor   # API + database + model health check (run this first)
python quant.py --once     # one dry-run poll, printed as JSON
python quant.py --backtest # walk-forward replay of your own tick history
python quant.py --host 0.0.0.0   # LAN mode for a phone (token printed at startup)
```

## The whole app is one question
The page is a single column: a header strip (net worth vs the **worst** of three benchmarks,
one freshness dot), a one-sentence **trust line** ("Last 30d: 41 closed calls · 29 hit target ·
avg +2.1% per trade"), and the **NOW list** — at most 3 cards, exits first. Everything else lives
behind *Record ▸* and *Engine room ▸*. Cards are literal: **"BUY 8× Test Orb — set 351 ex → 4
(87.8 ex each)"** is the exact ratio you type into the exchange, odds are plain ("works about
7 in 10"), and *details ▸* opens the full reasoning trace. When nothing qualifies you get the
flagship state: **"Nothing worth your divines right now"** plus the closest miss and why — most
polls, that is the correct call.

## Every card is a forecast, and forecasts get graded
This is the core design. Each card writes a prediction record (fill odds, hit odds, expected
return, model version, features) **before** the outcome is knowable. A **shadow book** then takes
every card whether you do or not — resting orders fill only when the market actually trades
through the price — so the system grades ~20 forecasts a week even if you trade twice. Outcomes
are scored with proper scoring rules (Brier, CRPS), feed conjugate posteriors that replace the
magic constants (reversion fraction, spread capture, hit rates), and signals whose measured edge
can't clear zero are **auto-gated off, visibly,** while they keep shadow-trading to earn their way
back. *NO EDGE is a finding, the way NO TRADE is advice.*

**Paper mode is the default** and uses the same honest fill engine (no instant-fill fiction). The
settings panel shows a **graduation rule**: the app recommends real mode only when ≥14 days of
paper alpha clear a t-test against the worst benchmark. It will tell you when it has earned it.

## How it predicts (honest version)
- **Latent price filter** — poe.ninja and poe2scout numbers are treated as what they are: noisy,
  lagged observations. A per-item Kalman filter (level + drift) fuses them; disagreement widens
  uncertainty, and wide uncertainty fails the entry gates on its own. The drift estimate is the
  knife-guard.
- **Mean reversion as a model, not a constant** — per-item OU/AR(1) fit on hourly bars (anchor =
  median of the window; the AR intercept extrapolates trends and is not trusted). The exit target
  is placed where the model gives ≥65% odds of reaching by the horizon, never above the mean. An
  item must have *demonstrated* reversion in its own history before DIP will risk money on it.
- **Factor structure** — deviations decompose into market (liquidity-weighted index of the top-20,
  also the third benchmark), family, and idiosyncratic parts. Only idiosyncratic dips are trades;
  market-wide moves trip a circuit breaker that pauses entries and says so.
- **Execution is part of the forecast** — fees come from a measurable curve
  (`config.advanced.json → fee_curve`), entries/exits are quantized to real in-game integer ratios
  (items too cheap to express cleanly are rejected — that's the lot-size guard, principled), and
  exits anchor to the thesis, never to your cost basis.
- **Sizing** — fractional Kelly on the conservative tail of the calibrated hit posterior, capped
  by % of daily volume, liquid reserve, and a per-family budget (three essence dips are one bet).
  Each card explains its own size.

## Signals
- **DIP** — idiosyncratic dip in a reverting item (OU + factor gates + knife/freefall guards).
- **MAKE** — patient spread capture on deep books (≥600 div/day), spread never quoted inside the noise.
- **ROUTE** — the same item priced differently via its exalted/divine/chaos books
  (poe2scout SnapshotPairs, self-validated against ninja's ex/div). True arbitrage; both books
  named on the card; verify in-game first, pair data is up to an hour old.
- **PARITY** — deterministic conversion recipes (e.g. 3:1 distilled-emotion instilling) priced
  against the market. Recipes are config data in `config.advanced.json → recipes`: do each once
  with one unit, then set `"verified": true` — unverified recipes stay humble and say so.
- **Pins** — your manual theses (`config.json → pins`): matched by name, card fires at your entry
  ceiling. For uniques and league bets the scanner can't see.

## Configuration: 0 required, 2 optional
First run writes `config.json` with `league: auto` (resolves the current softcore league at every
poll), `mode: paper`, `risk: conservative`, `pins: []`. The risk dial and paper/real toggle are in
the UI. Everything else — gates, horizons, priors, fee curve, recipes, poll cadence — lives in
`config.advanced.json` (absent by default; every key documented in `quant/config.py`).

**Migrating from v0.4** is automatic: old `config.json` is folded in (backup written next to it),
old fills/ticks/holdings import into the event ledger on first run.

## Architecture (for the next reader)
```
quant.py            thin shim — the run command never changes
quant/
  config.py         surface + advanced config, presets, recipes
  sources.py        poe.ninja / poe2scout adapters + contract checks (LiveIO)
  store.py          SQLite WAL: append-only event ledger (fills/orders/voids are
                    events, state is a fold — corrections never rewrite history),
                    per-source ticks, hourly bars, prediction ledger
  models.py         Kalman latent filter, OU fit, fill-time model, ratio
                    quantizer, fee curve
  signals.py        DIP / MAKE / ROUTE / PARITY → proposals (forecasts)
  score.py          Brier/CRPS, conjugate calibration, auto-gates, trust line,
                    graduation rule
  engine.py         the poll pipeline + card lifecycle + shadow book + sizing
  server.py, ui.py  JSON API + the one-question page
  backtest.py       walk-forward replay (no peeking) vs persistence baseline
  main.py           CLI + doctor
tests/              46 tests, fixture-driven; python -m unittest discover -s tests
```
The validation ladder for any strategy change: backtest on your tick history → live shadow A/B →
only then default-on. Tuning by vibes is the failure mode this app exists to prevent.

## Data sources
- **poe.ninja PoE2 exchange** — `/poe2/api/economy/exchange/current/overview?league=<L>&type=<T>`;
  types: `Currency Fragments Essences Runes SoulCores LineageSupportGems Expedition Ritual Abyss
  Delirium UncutGems Idols`. Prices, ex/div rate (`core.rates`), daily traded volume.
- **poe2scout** — `/api/poe2/Leagues` (league resolve + DivinePrice fallback) and
  `…/SnapshotPairs` (hourly snapshot of every in-game exchange pair → ROUTE legs + extra latent
  observations). OpenAPI at `/api/openapi.json` if an endpoint moves; `--doctor` sweeps everything.
- Polling is polite (5-min default, identifying User-Agent, pairs at most hourly). Endpoint shapes
  verified 2026-06-12; recorded fixtures in `tests/fixtures/` pin the contracts.

## Limits (still honest)
- No API can see your own orders — fills are confirmed by you (one tap in paper, two prefilled
  fields in real). The exchange's gold fee is an estimate until you measure it once and set
  `fee_curve`; gold itself is a resource the dashboard can't see.
- Aggregator prices are listing medians and pair data is up to an hour old: sanity-check the live
  exchange before committing >1 div, especially ROUTE divergences — the card names both books so
  the check takes seconds.
- Uniques aren't scanned (listing counts aren't traded volume, so the safety gates can't do their
  job) — pin them instead.
- `poe2-quant-dashboard.jsx` (the Claude-artifact edition) is **frozen**: superseded by the local
  app; kept for reference.
