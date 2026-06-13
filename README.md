# QUANT v1.0 — PoE2 currency decision-support that grades itself

A **read-only** co-pilot: it watches the market and tells you the move; you trade by hand in-game.
Nothing here automates gameplay or trading (that would breach GGG's terms — price monitoring does
not). Python 3.10+, **stdlib only, zero pip installs, zero required configuration.**

```
python quant.py            # serves http://localhost:8377 — that's the whole setup,
                           #   incl. first-run calibration and update checks
python quant.py --doctor   # API + database + model health check
python quant.py --once     # one dry-run poll, printed as JSON
python quant.py --backtest # walk-forward replay of your own tick history
python quant.py --bootstrap  # force a re-pretrain from league history
python quant.py --update     # check GitHub and update in place
python quant.py --host 0.0.0.0   # LAN mode for a phone (token printed at startup)
```
Just `python quant.py` is enough every time — it bootstraps calibration on first run and checks
for updates itself. The other commands are for when you want to force one of those by hand.

## Bootstrap: don't wait two weeks to be calibrated (now automatic)
On first run for a league, QUANT pulls the league's **daily history since league start** (poe2scout
DailyStatsHistory, top ~150 items by volume, one polite request each) in the background, then
re-runs the DIP logic over that history walk-forward — same anchor, idio-vs-market gate, knife
guard, exit quantile as the live signal — and **measures** how often such dips actually recovered
and how much of the gap they closed. Those measurements replace the guessed priors (capped at ~30
pseudo-observations so live graded outcomes keep the final say, never applied over existing live
evidence), and the fetched history gives every item a league anchor so DIP is calibrated **from
poll #1**. Runs once per league; `--bootstrap` forces it again. Set `auto_bootstrap: false` in
config.json to disable.

What the bootstrap honestly cannot do: daily listing medians carry no intraday path, so it can't
prove your limit orders would have filled. That's the part the shadow book and the 2-week paper
graduation validate forward — bootstrap shortens the *calibration* ramp, not the *trust* bar.

## Staying current
QUANT checks its GitHub branch for a newer `VERSION` on startup and a few times a day. When one
exists, a banner offers **update & restart** — it downloads the branch via the GitHub API,
byte-compiles it before trusting it, backs up the old code, swaps it in (your `config*.json` and
`quant.db` are never touched), and restarts into the new version. Set `auto_update: true` in
config.json to apply on startup without asking, or `update_branch` to track a different branch.

**Private repo?** Then the updater needs a read-only token (a public repo needs none). Create a
fine-grained Personal Access Token on GitHub scoped to **Contents: Read-only** on just this repo,
then make it visible to QUANT one of two ways:
- environment: `export QUANT_GH_TOKEN=github_pat_…` (e.g. in the systemd unit or your shell), or
- config: add `"github_token": "github_pat_…"` to `config.advanced.json`.

The token is sent only to `api.github.com` for this one repo, never logged, never committed.

## Recording trades — price is *per unit*
When you log or take a fill, **price is per unit, in exalted** — the same "X ex each" the card
shows, not the order total. The form previews `qty × price = total` so it's unambiguous. Got one
wrong? Hit **edit** on any row in the Trades table (under *Record ▸*): it appends a correction
event (nothing is ever rewritten) and every position, benchmark and net-worth number re-folds
automatically. The card linkage and exit target are preserved across an edit.

## Why did the cards disappear? — the status line
Above the cards there's always a one-line status: items scanned, positions held, resting orders,
new ideas, and divines free to deploy. When there are no new buys it tells you the reason —
"all 3 position slots are in use", "no liquid capital free to deploy (set/raise holdings)",
"paused — the whole market is moving hard", or the closest near-miss. An empty board is never a
mystery: most of the time it just means sitting tight is correct, and now it says so.

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

## Your data is preserved for later analysis
QUANT keeps the DB small by holding only a rolling ~14-day window of 5-minute ticks — but it does
**not throw the older ones away**: just before pruning, it appends them to monthly CSV files under
`data_archive/` (kept forever, append-only). The permanent hourly bars, the full forecast ledger
(every prediction + its graded outcome), and the whole decision log stay in the DB indefinitely.
So even if the tool turns out not to be worth running, you're left with a complete, replayable,
*unbiased* record — what the market did, what was predicted, and what actually happened — which is
exactly what a future/better algorithm needs (replay it with `--backtest`). Set
`archive_ticks: false` in `config.advanced.json` to opt out.

`python quant.py --export [dir]` writes a portable research bundle (default `quant_export/`):
`predictions.jsonl` (the labeled forecast→outcome dataset), `bars.csv` (permanent hourly history),
and `ticks_live.csv` (the current high-res window; the rest is already in `data_archive/`). Both
`data_archive/` and `quant_export/` are gitignored — it's your private history. The one thing the
tool can't protect against is you deleting `quant.db` and `data_archive/`, so back those up if the
long-term record matters to you.

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
