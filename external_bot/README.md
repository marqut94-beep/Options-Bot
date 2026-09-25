# Momentum Paper Desk - external runner

Runs the momentum breakout paper-trading strategy entirely outside Claude Code,
on GitHub Actions' free scheduler, so ongoing operation costs no Claude usage.
This replaces the two Claude Code routines' Step 1 / Step 2 logic; the tracker
dashboard stays where it is, fed by a separate, much cheaper periodic sync
(not built yet - see "Still to do" below).

## What's here

- `momentum_bot.py` - the strategy itself: manages open positions (stop/target/
  floor/EOD), scans for new entries (relVol/%change/price filters against the
  S&P 500+400+600 universe, breakout confirmation, earnings-adjacency skip with
  backfill, conviction-weighted $15k/day sizing). Reads/writes `state.json`.
- `state.json` - all trade state (open + closed). Starts empty. The workflow
  commits this back to the repo after every run, so state persists across runs
  without needing a database.
- `universe.json` - the S&P 500+400+600 ticker list (~931 symbols), copied from
  the backtesting work. **This will drift out of date** as index membership
  changes - worth refreshing every few months.
- `.github/workflows/momentum-bot.yml` - runs the script every 5 minutes during
  market hours (13:30-20:30 UTC, weekdays).

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

## Known gaps vs. the Claude Code routine (read before relying on this)

- **Earnings filter uses a different, better data source than the backtest
  did** (Finnhub's real calendar vs. the backtest's news-headline proxy) - the
  live behavior should be at least as good as what was tested, but hasn't been
  cross-checked against it.
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

## Still to do (not built yet)

- **The tracker dashboard doesn't see this bot's trades yet.** `state.json`
  and the Claude Artifact tracker are two separate stores right now. Next step
  is a small, infrequent Claude Code routine (e.g. every few hours) that reads
  `state.json` from this repo and syncs new/updated trades into the tracker's
  database - cheap because it's just a data sync, not per-trade decision-
  making, and doesn't need to run anywhere near every 5 minutes.
- Not yet tested against a real market day - recommend watching it in parallel
  with the existing Claude Code routines for a few days before trusting it
  solo, and comparing their entries/exits for the same days.
