# QUANT — PoE2 currency day-trading decision-support

A **read-only** dashboard: it watches the market and tells you the move; you trade by hand in-game. Nothing here automates gameplay or trading (that would breach GGG's terms — price monitoring does not).

## 1. Local app — scanner + action cards (v0.3)
**Requirements:** Python 3.10+. No pip installs, stdlib only.

```
python quant.py --probe   # API health check + scanner dry-run (do this first)
python quant.py           # serves http://localhost:8377, polls every 5 min
```

First run writes `config.json` next to the script. `--once` does a single poll and prints the snapshot JSON. Config reloads on every request, so edits apply on the next refresh.

### DO THIS NOW — the card panel
Every poll the scanner sweeps **all ~600 exchange-traded currencies** (12 poe.ninja types), checks them against the safety gates, and renders at most `risk.max_cards` action cards in plain language: what to buy, at what ceiling, what to relist it at, the expected profit after estimated fees, and a one-line *why*. Open positions always get their own card (HOLD with the live exit target, SELL when the target is hit, ABANDON when the trend guard trips). When nothing qualifies it says **NO TRADE** — that is advice too, and most polls it's the correct call.

Three signal types feed the cards:
- **DIP** — mean reversion: price ≥`dip_trigger_pct` under its own 24h mean (from Quant's tick history; falls back to the 7d sparkline until ~6h of history accumulates), with a knife-guard so it won't catch collapsing items.
- **MAKE** — patient spread capture on deep books (≥`make_min_volume` div/day): bid under mid, relist over it.
- **ROUTE** — the same item trading at different effective prices via its exalted pair vs its divine pair on the in-game exchange (from poe2scout's hourly exchange snapshot, self-validated against ninja's ex/div before being trusted). Entry and exit prices on these cards come from the pair data itself.

### The safety gates ("safe" as numbers, in `config.json → risk`)
A card only appears if **all** of these pass: item volume ≥`min_volume_div_day` (150 div/d default), position ≤`max_pos_pct_volume` of daily volume **and** ≤`max_bankroll_pct` of bankroll **and** within what's left after `liquid_reserve_pct` is held back, round-trip edge after `2×fee_pct_per_side` ≥`min_edge_net_pct`, expected profit ≥`min_profit_ex` (no flips worth pocket lint), and open positions <`max_open_positions`. With a 4-div bankroll this typically means 1–1.5 div positions in highly liquid items only — by design.

### v0.4 additions
- **Trades table** (right column): every fill with edit/delete — misclicks are
  fixable. Edit loads the fill into the form; "Update fill" saves it.
- **Capital section**: enter your actual liquid currency (div / ex / chaos).
  It replaces the static `start_capital_div` for sizing and the ribbon
  baseline; fills logged after setting subtract from it. If a card's buy
  needs more exalted than you hold, it says how many div to convert first and
  charges the extra fee leg in the expected profit.
- **Visual feedback**: taking a card flashes a toast, marks the card ✓ logged,
  and greys it out; edits/deletes confirm via toast too.
- **League-long history**: daily average price + volume since league start
  (poe2scout `DailyStatsHistory`) is backfilled for scanner candidates and
  held items. Each candidate gets a 14-day z-score: DIPs that aren't actually
  cheap vs league history get downgraded to MED confidence, genuinely cheap
  ones (z ≤ −1.5) say so in the why-line.

### Paper mode (default ON)
`"paper_mode": true` keeps a separate practice ledger: card buttons become one-click **Take it (paper)** and the ribbon tracks paper net worth (real net worth shown small under Holdings). Let it run for a few days; if the paper curve beats holding, set `paper_mode` to `false` — buttons then prefill the fill form for your real, in-game-confirmed numbers instead. Paper and real fills never mix.

## 2. Claude artifact — zero setup
Open `poe2-quant-dashboard.jsx` as an artifact. **Refresh prices** has Claude fetch current data via web search (~30–60s, uses your Claude usage — this replaces background polling, which browser artifacts can't do). **What's my move?** sends your snapshot + positions + rules to Claude for ≤5 prioritized actions. Positions, fills, and snapshots persist privately to your account between sessions. Settings panel accepts the exact same JSON as `config.json`. (The artifact predates the v0.3 scanner — it covers the manual-watchlist workflow only.)

## Wiring in your Fable 5 playbook
The watchlist (`plays`) is now optional — the scanner finds opportunities on its own. Keep using plays for longer-horizon theses from your playbook (uniques, league-mechanic bets) that the currency scanner doesn't cover.
After the strategist prompt produces play cards, paste them back to Claude with:

> Convert each play card into this JSON schema, one object per play:
> `{"id":"short_id","label":"name","source":"exchange:<NinjaType>|unique:<category>|currency:<category>|auto","match":"item name (fuzzy ok)","entry_max_ex":N,"exit_target_ex":N,"abandon_drop_pct":N,"budget_div":N,"notes":""}`

Drop the result into `config.json` → `plays` (local) or Settings (artifact). `source` is ignored by the artifact (Claude finds the item by name); the local app uses it to route the right API. **When unsure, use `"source": "auto"`** — it searches poe2scout's full item index (~1,300 items across every category) by name.

### Source kinds (local app)
- `exchange:<Type>` — poe.ninja in-game Currency Exchange data. Working types (case-sensitive, verified by `--probe`): `Currency Fragments Essences Runes SoulCores LineageSupportGems Expedition Ritual Abyss Delirium UncutGems Idols`. Common aliases are accepted (`omens`→`Ritual`, `emotions`→`Delirium`). Gives price, 7-day sparkline trend, and daily traded volume in div.
- `unique:<category>` — poe2scout uniques: `armour weapon accessory jewel flask map sanctum`. Gives price (in ex), 7-day trend from daily price logs, and current listing count.
- `currency:<category>` — poe2scout currency-likes: `currency fragments runes essences ultimatum expedition ritual vaultkeys breach abyss uncutgems lineagesupportgems delirium incursion idol verisium vaal`. Covers things ninja doesn't carry (talismans, waystones live here too via `auto`).
- `auto` — name-only lookup through poe2scout's `/Items` index, then enriched with price logs + listing count from the right category endpoint. Slowest but finds anything.

Matching is fuzzy: exact > prefix > substring > all-words, case/apostrophe-insensitive. A play that matches nothing produces a dashboard signal with the three closest real item names, so typos are self-diagnosing.

## League
`"league": "auto"` (the default) resolves to the current softcore league via poe2scout's league list at every poll — league rollovers need no config edit. You can also pin an explicit name (`"Runes of Aldur"`) or a short name (`"runes"`); names are validated and a warning signal appears if the league isn't recognized.

## Data sources
- **poe.ninja PoE2 exchange API** — `https://poe.ninja/poe2/api/economy/exchange/current/overview?league=<name>&type=<Type>`. Used for the ex/div rate (from `core.rates`), exchange goods, sparkline trends, and volume. Note: item names live in the top-level `items` list; `core.items` only holds divine/exalted/chaos (this caused silent match failures in v0.1).
- **poe2scout API** — realm-scoped REST, OpenAPI at `/api/openapi.json` (swagger UI at `/api/swagger`). Endpoints used: `GET /api/poe2/Leagues` (league list + DivinePrice, also the ex/div fallback if ninja is down), `GET /api/poe2/Leagues/<league>/Uniques/ByCategory` and `…/Currencies/ByCategory` (paginated, `ReferenceCurrency=exalted` so prices arrive in ex), `GET …/Items` (full item index for `auto` lookups), `…/Items/Categories`, and `…/SnapshotPairs` (hourly snapshot of every in-game exchange pair — both directions, traded volume, stock; powers the ROUTE signal; fetched at most once an hour). Fields are PascalCase (`CurrentPrice`, `PriceLogs`, `CurrentQuantity`).
- **Quant's own tick history** — every poll appends changed prices to a `ticks` table in `quant.db` (deduped, 14-day retention). This is what the 24h mean-reversion math runs on; signals visibly sharpen over the first day as history accumulates (the scanner panel shows `intraday history Nh`).

Endpoint shapes were verified live against both APIs on 2026-06-12. Third-party APIs change — `--probe` sweeps every endpoint and dry-runs the scanner + each play with the resolved league, so run it whenever data looks off. The 5-min poll exists to catch aggregator refreshes early and build tick history; the sources themselves refresh every few minutes to an hour, and the dashboard shows data age in the header. The poller sends an identifying User-Agent and stays well under polite request rates.

## Liquidity (the Liq column)
Aggregator prices are listing medians, not fills. The Liq column shows poe.ninja daily traded volume (`N div/d`) for exchange goods, or poe2scout current listing count (`N listed`) for scout-sourced items. **A price with `1 listed` next to it is one troll listing, not a market** — during testing, a leveling unique "priced" at 887 div showed exactly this. Treat low-liq prices as noise.

## The math model (honest version)
- Bankroll is converted once: `start_capital_div × (ex/div at first snapshot)`. Liquid = that minus net ex spent on fills. Net worth = (liquid + positions marked to latest price) ÷ current ex/div.
- Benchmark is **holding your starting divines**, which is flat in div terms — the ribbon shows your net worth against it. A drift chip warns when ex/div moves ≥5% so you re-check ex-denominated targets.
- Card edges are *estimates*: DIP assumes 70% reversion to the 24h mean, MAKE assumes the configured spread is capturable, ROUTE takes the measured pair divergence minus a 2% slippage buffer; all are then haircut by `2×fee_pct_per_side`. Expected-profit numbers are decision aids, not promises.
- 7-day trend comes from poe.ninja sparklines / poe2scout price logs; the ABANDON rule fires on trend, the ENTRY/EXIT rules on price vs thresholds; an EXIT that stays live past `no_fill_hours` escalates to a reprice nudge.

## Limits
- **No API can see your own listings or exchange orders** (there is no public stash river for PoE2), so fills are logged manually — the card buttons make it one click (paper) or a prefilled form (real).
- Prices are aggregator estimates and pair data is up to an hour old: always sanity-check the live exchange before committing >1 div, exactly as the playbook's Day-0 checklist says. ROUTE divergences especially can close before you act — the why-line shows both route prices so the in-game check takes seconds.
- Exchange gold fees are modeled only as the flat `fee_pct_per_side` estimate; tune it once you've seen real fees, and remember gold is a separate resource the dashboard can't see.
- Entry cards are re-ranked every poll, so a marginal candidate can swap out between polls; cards for positions you hold never disappear. (Card persistence/hysteresis is a planned pass-2 item, along with browser notifications and the self-grading scoreboard.)

## Troubleshooting
- Start with `python quant.py --probe`: it lists leagues, sweeps every ninja type, checks every scout endpoint, and dry-runs each of your plays, printing OK/MISS per play with the matched name, price, trend, and liquidity.
- A play shows "no match for …" → the signal already lists the three closest real item names; copy the right one into `match`, or switch the play to `"source": "auto"`.
- `--probe` FAIL on poe.ninja/poe2scout → the API moved again: check `https://poe2scout.com/api/openapi.json` and update the fetch layer in `quant.py` (everything lives in the `poe.ninja` / `poe2scout` sections near the top).
- Artifact refresh returns junk → just hit it again; the parser rejects non-JSON replies rather than storing garbage.
