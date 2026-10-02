# Momentum Paper Desk - external runner

Runs the momentum breakout paper-trading strategy entirely outside Claude Code,
on GitHub Actions' free scheduler, so ongoing operation costs no Claude usage.
This replaces the two Claude Code routines' Step 1 / Step 2 logic; the tracker
dashboard (https://claude.ai/artifact/C55SfW4UphkkJZGyAWFWFZ, "External Bot
Desk") is kept in sync by a separate Claude routine ("External Bot Desk -
GitHub state sync", runs a few times/day) that reads `state.json` from this
repo - built and running, not a gap.

Also produces `signals.json` (real-time entry/exit signals, only created once
a real trade actually happens - see `append_signal`/`SIGNALS_PATH` in
`momentum_bot.py`) for a separate Claude routine ("Robinhood Live Execution
(DRY-RUN)") that resolves the real Robinhood option contract and logs what it
would order - dry-run only (`armed: false` in the tracker's `executionConfig`)
until explicitly armed for real money. Also built and running, not a gap -
if a routine run ever reports `signals.json` as "not implemented," that's
wrong; it just means no real signal has fired yet (check `momentum_bot.py`
directly for the `append_signal` calls, not just this README).

## What's here

- `momentum_bot.py` - the strategy itself: manages open positions (stop/target/
  floor/EOD), scans for new entries (relVol/%change/price filters against the
  universe, bidirectional breakout confirmation, options-liquidity gate,
  conviction-weighted $15k/day sizing). Reads/writes `state.json` and
  `signals.json`. No earnings-adjacency filter (removed 2026-09-30, see below).
- `state.json` - all trade state (open + closed). Starts empty. The workflow
  commits this back to the repo after every run, so state persists across runs
  without needing a database.
- `universe.json` - the S&P 500+400+600 ticker list (~931 symbols, the specific
  one confirmed via testing to outperform more "complete" alternatives),
  **UPDATED 2026-09-30** with a 75-symbol curated foreign ADR pool added
  (TSM, BABA, ZIM, FNV-style large, liquid non-US-domiciled names - excluded
  from S&P indices purely on domicile grounds, not quality). Backtested: a
  real but modest +3.2% ($182.51->$188.33/day) improvement via competitive
  displacement of weaker stock candidates on busy days, not via rescuing quiet
  days (ADR signals correlate with the same broad-market volatility that
  drives stock signals, so they don't show up on days the rest of the
  universe is dead). 1,006 symbols total now. **Will drift out of date** as
  index membership changes - worth refreshing every few months.
- `.github/workflows/momentum-bot.yml` and `momentum-bot-b.yml` - two offset
  workflows (GitHub's own floor for a single scheduled workflow is 5 minutes,
  so two staggered ones are used the same way the Claude routines are) giving
  a combined effective check cadence of ~2-3 minutes during market hours
  (9:00am-4:55pm ET, weekdays). Both share a `concurrency` group so they queue
  instead of racing to commit `state.json` when they land close together.

## Setup steps (manual - I don't have GitHub write access in this environment)

1. **Create a repo.** Can be private. Put these files at the repo root (so
   `.github/workflows/` lands where GitHub expects it).
2. **Add secrets** (repo Settings -> Secrets and variables -> Actions):
   - `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` - your existing Alpaca paper
     credentials.
   - `FINNHUB_API_KEY` - optional. Get a free key at finnhub.io. Without this,
     the earnings-adjacency filter is skipped (logged, not a failure) and the
     bot runs the same as before that filter existed.
3. **Push.** The workflow starts running on its schedule automatically. You can
   also trigger it manually from the Actions tab (`workflow_dispatch`).
4. **Watch the first few runs** under the Actions tab to confirm it's picking
   up candidates and not erroring - especially check that `state.json` is
   actually getting committed back each run.

## Options-liquidity pre-filter + real options quote logging

A backtest found that only ~53-55% of otherwise-qualifying candidates (2024-
2026, S&P 500+400+600 universe) actually have a real, tradeable option at any
nearby expiration - coverage rises with underlying price and has improved
over time, but roughly half of setups simply aren't optionable. Since this
strategy is meant to be traded via options, `new_entries()` GATES on this:
for each candidate (in relVol-descending order), it looks up a real, nearest-
to-the-money, nearest-expiration (<=7 days out) option contract via Alpaca's
options data API (contract metadata from
`paper-api.alpaca.markets/v2/options/contracts`) - no listed contract means
the candidate is skipped entirely and the next-highest-relVol candidate is
tried instead.

**LOOSENED 2026-09-30** (paper-testing phase, wanted more activity): pricing
now falls back to the live bid/ask midpoint (`v1beta1/options/quotes/latest`)
when the contract hasn't printed an actual trade yet, instead of requiring a
real trade print to pass the gate. Live evidence from 2026-09-29 showed this
matters: CBOE and AIG both had confirmed breakouts and real listed contracts
that day but got rejected purely because neither had traded at the exact
moment the bot checked - the trade-print requirement was catching real,
optionable setups, not just genuinely illiquid ones. Each trade now logs
`optionPricedVia` ("trade" or "quote-mid") so this can be analyzed separately
later. Because of this change, the originally backtested "options-filtered"
result (75.81% win rate / ~$176.91/day, 2024-2026) is no longer a clean
apples-to-apples match for what this looser gate will produce going forward -
expect somewhat more trades than that backtest, with pricing on some of them
coming from a quote midpoint rather than a real fill.

Every surviving trade logs its real option entry price (`optionSymbol`,
`optionStrike`, `optionExpiration`, `optionEntryPrice`), and `log_option_exit`
captures the matching real exit price (`optionExitPrice`, `optionReturnPct`)
at every close (stop/target/floor/EOD). `print_summary()` prints a running
real-options comparison (win rate, avg return, observed multiple) once enough
closed trades have both prices captured - this keeps validating the "2-4x the
underlying's move" assumption live (a small sample already suggests the real
multiple is much higher, ~41x, where a contract exists at all).

## Earnings-adjacency filter: REMOVED 2026-09-30

Used to skip any candidate reporting earnings within +/-1 day (via Finnhub's
calendar). A backtest found this was a bad trade: earnings-adjacent setups
ARE individually weaker (53.05% win rate / +1.06% avg return vs. 65.81% /
+1.759% non-earnings), but the net effect of actually skipping them cost
~29.5% of daily $ (*$183.69/day -> $129.57/day*) and was the single biggest
cause of dead days - **15% of all trading days (100 of 666) had their only
real candidates wiped out entirely by this filter**. Removed - `new_entries()`
no longer calls `fetch_earnings_skip_set()` (function kept, just unused, in
case this needs to come back). No FINNHUB_API_KEY dependency anymore for
this reason, though the secret can stay configured harmlessly.

## Volume screening metric: CHANGED to z-score 2026-10-02

The relVol screening/ranking/sizing metric used to be a simple ratio
(today's volume / 30-day average volume), matching the live Claude routine.
**Changed to a volume z-score** ((today's volume - 30-day mean) / 30-day
stdev) instead - a reconciled backtest (trusted candidate pool, real
options-coverage gate via Alpaca, same validated exit structure) found this
beats the simple ratio by **+3.2%** ($308.26/day vs $298.61/day, 71.34% vs
70.73% win rate, fewer but higher-quality trades: 1,270 vs 1,336 over the
same 666-day window). A z-score adapts to each stock's own volume
variability - a 2x spike means something very different for a stock with
naturally choppy volume vs. one with a tight, consistent range - whereas the
simple ratio treats both the same. The field is still called `relVol` in the
trade schema/dashboard for compatibility, but it's now a z-score, not a
ratio - **this is a deliberate external-bot-only change** (the Claude
routine still uses Robinhood's live scan, which reports relVol as a simple
ratio and can't easily be swapped to a self-computed z-score), so comparing
the two live trackers going forward is also an implicit A/B test of this
specific change. `conviction()`'s clamp range was recalibrated from
`(x-2)/6` to `(x-2)/12` to match the z-score distribution's real spread
(median ~5.3, p75 ~9.4 on qualifying candidates) instead of the old
ratio-based 2-8 range.

## Known gaps vs. the Claude Code routine (read before relying on this)

- **No options-volume liquidity filter.** The live Claude routine's Robinhood
  scan checks avg options volume >= 1000; Alpaca has no equivalent, so this
  script relies solely on S&P-index membership as the liquidity proxy. That's
  the combination we backtested best with (70.9% WR), so this isn't a
  downgrade, just a different mechanism.
- **`universe.json` is a snapshot**, not point-in-time historical membership
  and not auto-updating. Refresh it periodically by hand.
- **DST handling** uses `zoneinfo` (`America/New_York`), which is correct and
  auto-adjusting - more robust than the Claude routine's manual UTC-offset math
  actually.

## Still to do

- **Live real-money execution**: the dry-run pipeline is built and running
  (see above), but `armed` is still `false` - no real order has ever been
  placed. Flip `meta/executionConfig.armed` to `true` on the tracker artifact
  only when actually ready to risk real money.
- Ongoing: keep comparing entries/exits against the Claude Code routines
  running in parallel - they use a different universe/discovery mechanism
  (Robinhood live scan vs. this bot's fixed S&P~1500+ADR list), so they won't
  always agree, by design.
