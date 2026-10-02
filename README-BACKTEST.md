# Momentum bot backtest runner

This package adds a manual historical replay without changing the live bot,
state.json, scheduled trading workflows, or execution signals. The runner uses
GET requests only and never submits orders.

## Install in Options-Bot

Add these three files, preserving their paths:

1. `backtest_runner.py` at the repository root.
2. `test_backtest_runner.py` at the repository root.
3. `.github/workflows/backtest.yml` under `.github/workflows/`.

Your existing `universe.json` stays in place. No dependency changes are needed;
the runner and tests use Python's standard library.

Commit the files to the default branch so the manual workflow becomes available.
Then open **Actions → Momentum Backtest → Run workflow**.

The starting test window is September 1 through October 1, 2026. You can change
both dates in the run form. Use completed sessions ending before today's date.
Start with a small ticker subset to verify API permissions, then leave the ticker
field blank to test the full universe. A subset is a smoke test, not evidence
about the full strategy. Longer periods and full-universe requests may take time
and consume GitHub Actions minutes and Alpaca API quota.

## Secrets

The workflow uses the existing repository secrets:

* `ALPACA_API_KEY_ID`
* `ALPACA_API_SECRET_KEY`

It does not print, export, or write either secret. SIP stock data is required.
Historical option coverage mode additionally requires permission for OPRA option
trades and the paper Trading API's archived contract metadata. Authentication or
subscription failures stop the run; they are not silently turned into empty data.

## What is tested

The implemented settings match the posted active constants:

| Setting | Value |
|---|---:|
| Daily budget | $25,000 |
| Maximum trades per day | 4 |
| Minimum remaining budget | $5,000 |
| Initial stock stop | 3% |
| First stock target | 4% |
| Remainder stock floor | 2% |
| Remainder stock target | 10% |
| Volume z-score threshold | 2 |
| Absolute open-to-current move | 3% |
| Stock price range | $10–$500 |
| Earnings filter | Disabled |

**Legacy mode** preserves the central backdating defect: it screens with data
available at the current decision, then selects the earliest prior opening-range
breakout. It also reuses entry and partial-target candles in exit scans. Its
results are diagnostic, not an achievable live return.

**Causal mode** evaluates the most recent completed minute against the opening
range, buys or shorts at the next minute open, and ignores pre-entry candle
movement. It starts remainder evaluation after the partial-target candle and
allows stops to fill worse than the trigger when a minute opens through it.
This is an explicit proposed correction, not a claim that the live code already
behaves this way. It changes which trades qualify as well as their timing.

Both modes use the existing A/B workflows' idealized UTC check times. Historical
GitHub queue delays, HTTP processing delays, live bar revisions, and the
separate downstream executor are not reconstructed. End-of-day handling follows
market-calendar early closes rather than treating every session as a full day.

A stock slippage assumption is adjustable. The initial value is **5 basis points
per side (0.05%)**, a scenario input rather than an empirically measured cost.
Compare 0, 5, and 10 basis points to inspect sensitivity. Fees, borrow constraints,
and order-size-dependent market impact are not included.

## Option coverage modes

* **historical-trades** (default): finds archived standard option contracts
  expiring from the test date through seven days later, selects by strike
  distance then expiration, and requires an OPRA trade at or before the decision.
  It records that observation and its age. Missing contract/trade observations
  are counted in diagnostics and excluded from this gate.
* **off**: tests an explicitly ungated stock baseline. It does not reproduce
  the running bot's option eligibility filter.

Historical archived chains are not point-in-time listing snapshots. Pagination
and active/inactive contract statuses are handled completely, unlike the live
code's first-100-contract selection. The historical-trades mode cannot reproduce
the live quote-mid fallback for contracts without a recent trade, and it does
not prove whether a contract could be traded in the desired size. Do not label
this as an exact options-filtered reproduction of the running bot.

**Neither mode calculates option dollar profit.** The available stock prices and
historical option prints do not establish executable option bid/ask fills.

## Outputs

The Actions run summary contains the comparison table. Download the
`momentum-backtest-<run id>` artifact from the completed run for:

* `summary.json`: settings, data limitations, diagnostics, and performance.
* `trades.csv`: each entry, decision time, allocation, and stock result.
* `trades.json`: the same trades with complete stock exit legs.
* `report.md`: readable comparison and limitations.
* `checkpoint.json`: incremental results, including if a later request fails.

Read diagnostics and limitations before interpreting the profit totals. Zero-trade
sessions remain in average daily profit. Missing exits are incomplete, not zero
profit. Drawdown is measured on daily closing realized P&L, not intraday account
equity. Raw prices and volumes are used; corporate-action dates need inspection
because the production daily lookback uses split adjustment.

The current universe is reused for all historical dates. This produces selection
and survivorship bias. A robust longer test requires historical universe membership
and corporate-action treatment, as well as an untouched out-of-sample period.

## Local execution

```bash
python -m unittest -v test_backtest_runner.py
python backtest_runner.py --start 2026-09-01 --end 2026-10-01
```

Supply credentials through the environment; do not place them in source files.
Cache files contain market data only and remain local to the ephemeral runner.
They are not committed or uploaded as result artifacts.

## Verification status

Sixteen offline behavioral checks pass. A mocked end-to-end run also verifies
report generation. These are software checks, not historical profitability
results. A live Alpaca run must verify endpoint permissions, archived coverage,
full-universe runtime, and the resulting records before drawing conclusions.

Official documentation:

* https://docs.alpaca.markets/us/reference/stockbars
* https://docs.alpaca.markets/us/reference/get-options-contracts
* https://docs.alpaca.markets/us/reference/optiontrades
* https://docs.alpaca.markets/us/docs/historical-option-data
* https://docs.alpaca.markets/us/v1.4.2/reference/getcalendar-1
