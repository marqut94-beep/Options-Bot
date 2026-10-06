#!/usr/bin/env python3
"""
Momentum Paper Desk - external runner.

Runs the same strategy as the Claude Code routine (2x relVol / 3% move breakout,
two-stage -2%/+4%/floor+2%/target+6% exit, $15k daily conviction-weighted budget,
3 trades/day cap, earnings-adjacency skip with backfill) entirely outside Claude
Code, using only Alpaca market data (1-minute bars, real-time full SIP feed via
the Algo Trader Plus data plan). State persists in state.json, which the GitHub
Actions workflow commits back to the repo after every run.

Env vars required (set as GitHub Actions secrets):
  ALPACA_API_KEY_ID
  ALPACA_API_SECRET_KEY
Optional:
  FINNHUB_API_KEY   - enables the earnings-adjacency filter. Without it, the
                       filter is skipped entirely (logged clearly below) rather
                       than failing the run.
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).parent
STATE_PATH = HERE / "state.json"
UNIVERSE_PATH = HERE / "universe.json"
SIGNALS_PATH = HERE / "signals.json"

ALPACA_KEY = os.environ.get("ALPACA_API_KEY_ID")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET_KEY")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")

ALPACA_DATA = "https://data.alpaca.markets/v2"
ALPACA_OPTIONS_DATA = "https://data.alpaca.markets/v1beta1/options"
ALPACA_TRADING = "https://paper-api.alpaca.markets/v2"
FINNHUB_BASE = "https://finnhub.io/api/v1"

DAILY_BUDGET = 25000.0
MAX_TRADES_PER_DAY = 4
MIN_REMAINING_TO_ENTER = 5000.0
STOP_PCT = 0.03
FIRST_TARGET_PCT = 0.04
REMAINDER_FLOOR_PCT = 0.02
REMAINDER_TARGET_PCT = 0.10
PARTIAL_EXIT_PCT = 0.10  # fraction of the position banked at the first target (backtest 2026-10-06: fine sweep found 10% beats 50% by +12.6% $/day, near-optimal while avoiding the rounding-to-0-shares risk of going smaller)
REL_VOL_MIN = 2.0
PCT_CHANGE_MIN = 3.0
PRICE_MIN, PRICE_MAX = 10.0, 500.0
STALE_CONFIRMATION_MINUTES = 15

if not ALPACA_KEY or not ALPACA_SECRET:
    print("FATAL: ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set.", file=sys.stderr)
    sys.exit(1)

HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}


# ---------- state ----------

def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"trades": {}, "heartbeat": None}


def save_state(state):
    # Write trades newest-first (by entryTime) so the raw file reads with the
    # most recent activity at the top - purely cosmetic, doesn't affect logic.
    trades = state.get("trades", {})
    sorted_trades = dict(
        sorted(trades.items(), key=lambda kv: kv[1].get("entryTime", ""), reverse=True)
    )
    out_state = dict(state)
    out_state["trades"] = sorted_trades
    STATE_PATH.write_text(json.dumps(out_state, indent=2, default=str))


def load_universe():
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8-sig"))


# ---------- real-execution signals ----------
#
# A "signal" is a real-time-only record of an entry or exit this specific run
# just decided on - never backfilled/historical - so a downstream consumer
# (a Claude routine watching this repo) can act on it as "happening now" and
# construct a real order. This file is purely additive: writing to it never
# changes any paper-trading logic above, and a consumer failing to read it
# never blocks a future run from continuing to write new ones. Each signal
# carries a stable `id` so a consumer can track which ones it has already
# acted on without this script needing to know anything about consumers.

def load_signals():
    if SIGNALS_PATH.exists():
        return json.loads(SIGNALS_PATH.read_text())
    return []


def save_signals(signals):
    SIGNALS_PATH.write_text(json.dumps(signals, indent=2, default=str))


def append_signal(signals, trade_id, action, **fields):
    sig = {
        "id": f"{trade_id}_{action}_{now_utc().isoformat()}",
        "tradeId": trade_id,
        "action": action,
        "createdAt": now_utc().isoformat(),
    }
    sig.update(fields)
    signals.append(sig)


# ---------- time helpers ----------

def now_utc():
    return datetime.now(timezone.utc)


def now_et():
    # Fixed -4/-5 offset would drift on DST; for a real deploy pin zoneinfo
    # (Python 3.9+: from zoneinfo import ZoneInfo; datetime.now(ZoneInfo("America/New_York")))
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def is_market_hours(et_dt):
    if et_dt.weekday() >= 5:
        return False
    minutes = et_dt.hour * 60 + et_dt.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60


def today_str(et_dt):
    return et_dt.strftime("%Y-%m-%d")


# ---------- Alpaca fetch helpers ----------

def fetch_daily_bars_batch(symbols, start, end):
    """Batched multi-symbol daily bars. Returns {symbol: [bars]}."""
    out = {}
    CHUNK = 200
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        url = f"{ALPACA_DATA}/stocks/bars"
        params = {
            "symbols": ",".join(chunk),
            "timeframe": "1Day",
            "start": start,
            "end": end,
            "feed": "sip",
            "adjustment": "split",
            "limit": 10000,
        }
        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        r.raise_for_status()
        data = r.json().get("bars", {})
        for sym, bars in data.items():
            out[sym] = bars
    return out


def fetch_minute_bars(symbol, start, end):
    url = f"{ALPACA_DATA}/stocks/{symbol}/bars"
    params = {"timeframe": "1Min", "start": start, "end": end, "feed": "sip", "limit": 1000}
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("bars", [])


# ---------- options quote logging (observational only - no real orders) ----------
#
# This does NOT change position sizing, entries, or exits - it exists purely to
# collect real option price data alongside each paper trade's underlying-based
# entry/exit, so the "long call/put sees ~2-4x the underlying move" assumption
# on the tracker dashboard can be checked against reality over time instead of
# staying an unvalidated guess. Every call here is best-effort: if a contract
# can't be found or a quote fetch fails, the paper trade still logs normally
# with the option fields left null - this must never block real strategy logic.

def find_option_contract(symbol, direction, ref_price, et_dt):
    """Nearest-to-the-money, nearest-expiration (<=7 days out) call/put for
    SYMBOL, for observational quote logging only. Returns the contract dict
    (has 'symbol', 'strike_price', 'expiration_date') or None."""
    option_type = "call" if direction == "long" else "put"
    today = today_str(et_dt)
    exp_lte = (et_dt + timedelta(days=7)).strftime("%Y-%m-%d")
    url = f"{ALPACA_TRADING}/options/contracts"
    params = {
        "underlying_symbols": symbol,
        "expiration_date_gte": today,
        "expiration_date_lte": exp_lte,
        "type": option_type,
        "status": "active",
        "limit": 100,
    }
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        contracts = r.json().get("option_contracts", [])
    except Exception as e:
        print(f"WARN: option contract lookup failed for {symbol}: {e}")
        return None
    if not contracts:
        return None

    def strike_dist(c):
        try:
            return abs(float(c["strike_price"]) - ref_price)
        except Exception:
            return float("inf")

    contracts.sort(key=lambda c: (strike_dist(c), c.get("expiration_date", "")))
    return contracts[0]


def fetch_option_latest_trade(option_symbol):
    """Latest real trade price for an option contract symbol, or None."""
    url = f"{ALPACA_OPTIONS_DATA}/trades/latest"
    params = {"symbols": option_symbol}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        trades = r.json().get("trades", {})
        t = trades.get(option_symbol)
        return t["p"] if t else None
    except Exception as e:
        print(f"WARN: option quote fetch failed for {option_symbol}: {e}")
        return None


def fetch_option_latest_quote_mid(option_symbol):
    """Bid/ask midpoint for an option contract symbol, or None. Quotes are
    typically live even when a contract hasn't printed a trade yet - used as
    a fallback price so a real, listed-but-not-yet-traded contract doesn't
    block an entry the way requiring an actual trade print does."""
    url = f"{ALPACA_OPTIONS_DATA}/quotes/latest"
    params = {"symbols": option_symbol}
    try:
        r = requests.get(url, headers=HEADERS, params=params, timeout=15)
        r.raise_for_status()
        quotes = r.json().get("quotes", {})
        q = quotes.get(option_symbol)
        if not q:
            return None
        bid, ask = q.get("bp"), q.get("ap")
        if bid and ask and bid > 0 and ask > 0:
            return (bid + ask) / 2.0
        return None
    except Exception as e:
        print(f"WARN: option quote-mid fetch failed for {option_symbol}: {e}")
        return None


# ---------- earnings filter ----------

def fetch_earnings_skip_set(et_dt):
    """Symbols reporting earnings within +/-1 day of today. Empty set (with a
    warning) if FINNHUB_API_KEY isn't set - the filter degrades gracefully
    rather than blocking the whole run."""
    if not FINNHUB_KEY:
        print("WARN: FINNHUB_API_KEY not set - earnings-adjacency filter disabled this run.")
        return set()
    d0 = (et_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    d1 = (et_dt + timedelta(days=1)).strftime("%Y-%m-%d")
    url = f"{FINNHUB_BASE}/calendar/earnings"
    params = {"from": d0, "to": d1, "token": FINNHUB_KEY}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        rows = r.json().get("earningsCalendar", [])
        return {row["symbol"] for row in rows if row.get("symbol")}
    except Exception as e:
        print(f"WARN: earnings calendar fetch failed ({e}) - treating as empty this run.")
        return set()


# ---------- sizing ----------

def conviction(rel_vol):
    # Recalibrated 2026-10-02 for the volume-zscore screening metric (see
    # new_entries) - z-score distribution on real qualifying candidates:
    # median ~5.3, p75 ~9.4, so floor=2 (the screening threshold)/ceiling=14
    # gives a sensible spread, replacing the old simple-ratio 2-8 range.
    return max(0.0, min(1.0, (rel_vol - 2.0) / 12.0))


def ideal_dollar(rel_vol):
    return 5000.0 + 7500.0 * conviction(rel_vol)


def log_option_exit(t):
    """Best-effort: attach a real option exit price/return alongside the
    underlying-based close, for the observational options-P&L check. Never
    raises or blocks the real (underlying-based) exit logic."""
    opt_symbol = t.get("optionSymbol")
    if not opt_symbol:
        return
    try:
        price = fetch_option_latest_trade(opt_symbol)
        if price is None:
            return
        t["optionExitPrice"] = price
        t["optionExitTime"] = now_utc().isoformat()
        entry = t.get("optionEntryPrice")
        if entry:
            t["optionReturnPct"] = round((price - entry) / entry * 100, 4)
    except Exception as e:
        print(f"WARN: log_option_exit failed for {opt_symbol}: {e}")


# ---------- Step 1: manage open positions ----------

def manage_open_positions(state, et_dt, signals):
    changed = False
    for trade_id, t in list(state["trades"].items()):
        if t.get("status") != "open":
            continue

        direction = t["direction"]
        shares = t["shares"]
        entry_price = t["entryPrice"]
        opt_contracts = t.get("optionContracts")

        if not t.get("partialTaken"):
            start = t["entryTime"]
            bars = fetch_minute_bars(t["symbol"], start, now_utc().isoformat())
            stop_price, target_price = t["stopPrice"], t["targetPrice"]
            triggered = None
            for b in bars:
                lo, hi = b["l"], b["h"]
                if direction == "long":
                    stop_hit, target_hit = lo <= stop_price, hi >= target_price
                else:
                    stop_hit, target_hit = hi >= stop_price, lo <= target_price
                if stop_hit:
                    triggered = ("stop", b)
                    break
                if target_hit:
                    triggered = ("target", b)
                    break

            if triggered and triggered[0] == "stop":
                pnl = shares * (stop_price - entry_price) if direction == "long" else shares * (entry_price - stop_price)
                log_option_exit(t)
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=stop_price,
                         exitReason="stop", returnPct=-STOP_PCT * 100,
                         dollarPnl=round(pnl, 2))
                if opt_contracts:
                    append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"],
                                  optionSymbol=t.get("optionSymbol"), contracts=opt_contracts,
                                  reason="stop")
                changed = True
            elif triggered and triggered[0] == "target":
                shares_partial = max(1, round(shares * PARTIAL_EXIT_PCT)) if shares > 1 else 1
                shares_remainder = shares - shares_partial
                if shares_remainder == 0:
                    pnl = shares * (target_price - entry_price) if direction == "long" else shares * (entry_price - target_price)
                    log_option_exit(t)
                    t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=target_price,
                             exitReason="target", returnPct=FIRST_TARGET_PCT * 100, dollarPnl=round(pnl, 2))
                    if opt_contracts:
                        append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"],
                                      optionSymbol=t.get("optionSymbol"), contracts=opt_contracts,
                                      reason="target")
                else:
                    partial_pnl = shares_partial * (target_price - entry_price) if direction == "long" else shares_partial * (entry_price - target_price)
                    floor = entry_price * (1 + REMAINDER_FLOOR_PCT) if direction == "long" else entry_price * (1 - REMAINDER_FLOOR_PCT)
                    rtarget = entry_price * (1 + REMAINDER_TARGET_PCT) if direction == "long" else entry_price * (1 - REMAINDER_TARGET_PCT)
                    if opt_contracts:
                        contracts_partial = max(1, round(opt_contracts * PARTIAL_EXIT_PCT)) if opt_contracts > 1 else 1
                        contracts_remainder = opt_contracts - contracts_partial
                    else:
                        contracts_partial = contracts_remainder = None
                    t.update(partialTaken=True, partialExitPrice=target_price,
                             partialExitTime=triggered[1]["t"], partialDollarPnl=round(partial_pnl, 2),
                             sharesPartial=shares_partial, sharesRemainder=shares_remainder,
                             remainderFloor=floor, remainderTarget=rtarget,
                             optionContractsPartial=contracts_partial,
                             optionContractsRemainder=contracts_remainder)
                    if contracts_partial:
                        append_signal(signals, trade_id, "exit_close_partial", symbol=t["symbol"],
                                      optionSymbol=t.get("optionSymbol"), contracts=contracts_partial,
                                      reason="target")
                changed = True
            elif (et_dt.hour == 15 and et_dt.minute >= 55) or et_dt.hour >= 16:
                # EOD flatten - use last bar close as proxy for a live quote
                last_price = bars[-1]["c"] if bars else entry_price
                pnl = shares * (last_price - entry_price) if direction == "long" else shares * (entry_price - last_price)
                ret = (pnl / (shares * entry_price)) * 100
                log_option_exit(t)
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=last_price,
                         exitReason="eod", returnPct=round(ret, 4), dollarPnl=round(pnl, 2))
                if opt_contracts:
                    append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"],
                                  optionSymbol=t.get("optionSymbol"), contracts=opt_contracts,
                                  reason="eod")
                changed = True
            else:
                if bars:
                    last_price = bars[-1]["c"]
                    pnl = shares * (last_price - entry_price) if direction == "long" else shares * (entry_price - last_price)
                    t["lastPrice"] = last_price
                    t["lastPriceAt"] = bars[-1]["t"]
                    t["unrealizedDollar"] = round(pnl, 2)
                    t["unrealizedPct"] = round((pnl / (shares * entry_price)) * 100, 4)
                    changed = True

        else:
            start = t["partialExitTime"]
            bars = fetch_minute_bars(t["symbol"], start, now_utc().isoformat())
            floor, rtarget = t["remainderFloor"], t["remainderTarget"]
            shares_remainder = t["sharesRemainder"]
            triggered = None
            for b in bars:
                lo, hi = b["l"], b["h"]
                if direction == "long":
                    floor_hit, target_hit = lo <= floor, hi >= rtarget
                else:
                    floor_hit, target_hit = hi >= floor, lo <= rtarget
                if floor_hit:
                    triggered = ("floor", floor)
                    break
                if target_hit:
                    triggered = ("target", rtarget)
                    break

            resolved_price, reason = None, None
            if triggered:
                resolved_price, reason = triggered[1], triggered[0]
            elif (et_dt.hour == 15 and et_dt.minute >= 55) or et_dt.hour >= 16:
                resolved_price = bars[-1]["c"] if bars else t["partialExitPrice"]
                reason = "eod"

            if resolved_price is not None:
                remainder_pnl = shares_remainder * (resolved_price - entry_price) if direction == "long" else shares_remainder * (entry_price - resolved_price)
                blended_pnl = t["partialDollarPnl"] + remainder_pnl
                blended_ret = (blended_pnl / (shares * entry_price)) * 100
                log_option_exit(t)
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=resolved_price,
                         exitReason=reason, returnPct=round(blended_ret, 4), dollarPnl=round(blended_pnl, 2))
                contracts_remainder = t.get("optionContractsRemainder")
                if contracts_remainder:
                    append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"],
                                  optionSymbol=t.get("optionSymbol"), contracts=contracts_remainder,
                                  reason=reason)
                changed = True
            else:
                if bars:
                    last_price = bars[-1]["c"]
                    remainder_pnl = shares_remainder * (last_price - entry_price) if direction == "long" else shares_remainder * (entry_price - last_price)
                    total_unrealized = t["partialDollarPnl"] + remainder_pnl
                    t["lastPrice"] = last_price
                    t["lastPriceAt"] = bars[-1]["t"]
                    t["unrealizedDollar"] = round(total_unrealized, 2)
                    t["unrealizedPct"] = round((total_unrealized / (shares * entry_price)) * 100, 4)
                    changed = True

    return changed


# ---------- Step 2: new entries ----------

def new_entries(state, et_dt, signals):
    today = today_str(et_dt)
    todays_trades = [t for t in state["trades"].values() if t["date"] == today]
    if len(todays_trades) >= MAX_TRADES_PER_DAY:
        return False
    if not (9 * 60 + 30 <= et_dt.hour * 60 + et_dt.minute <= 15 * 60 + 30):
        return False

    already_symbols = {t["symbol"] for t in todays_trades}
    allocated = sum(t["allocDollar"] for t in todays_trades)
    remaining = DAILY_BUDGET - allocated
    if remaining < MIN_REMAINING_TO_ENTER:
        return False

    # Earnings-skip filter REMOVED 2026-09-30 - a backtest found it was a bad
    # trade: it does improve per-trade win rate (earnings-adjacent setups are
    # individually weaker, 53.05% WR vs 65.81% non-earnings), but the net
    # effect of actually skipping them costs ~29.5% of daily $ ($183.69/day ->
    # $129.57/day) and is the single biggest cause of dead days - 15% of all
    # trading days (100 of 666) had their only real candidates wiped out
    # entirely by this filter. Not worth it. See fetch_earnings_skip_set
    # above if this ever needs to come back.
    earnings_skip = set()

    universe = load_universe()
    end = now_utc()
    start = end - timedelta(days=45)
    daily = fetch_daily_bars_batch(universe, start.isoformat(), end.isoformat())

    candidates = []
    for sym, bars in daily.items():
        if len(bars) < 31:
            continue
        today_bar = bars[-1]
        window = bars[-31:-1]
        vols = [b["v"] for b in window]
        mean_vol_30 = sum(vols) / 30.0
        if mean_vol_30 <= 0:
            continue
        # Volume z-score, not a simple ratio-to-average - CHANGED 2026-10-02.
        # A reconciled backtest (trusted candidate pool, real options-coverage
        # gate, same exit structure) found z-score ranking beats simple-ratio
        # relVol by +3.2% ($308.26/day vs $298.61/day, 71.34% vs 70.73% WR,
        # fewer but higher-quality trades: 1,270 vs 1,336). A z-score adapts to
        # each stock's own volume variability instead of treating a 2x spike
        # the same regardless of how choppy that stock's normal volume is.
        variance = sum((v - mean_vol_30) ** 2 for v in vols) / 30.0
        stdev_vol_30 = variance ** 0.5
        if stdev_vol_30 <= 0:
            continue
        rel_vol = (today_bar["v"] - mean_vol_30) / stdev_vol_30
        pct_change = (today_bar["c"] - today_bar["o"]) / today_bar["o"] * 100
        price = today_bar["c"]
        if rel_vol >= REL_VOL_MIN and abs(pct_change) >= PCT_CHANGE_MIN and PRICE_MIN <= price <= PRICE_MAX:
            candidates.append({"symbol": sym, "relVol": rel_vol, "pctChange": pct_change})

    candidates.sort(key=lambda c: c["relVol"], reverse=True)
    print(f"Candidates this run: {[(c['symbol'], round(c['relVol'],2), round(c['pctChange'],2)) for c in candidates]}")

    changed = False
    entries_made = 0
    skipped_for_earnings = []
    open_market = et_dt.replace(hour=9, minute=30, second=0, microsecond=0)

    for cand in candidates:
        if entries_made >= MAX_TRADES_PER_DAY - len(todays_trades):
            print(f"  {cand['symbol']}: stopping - {MAX_TRADES_PER_DAY - len(todays_trades)} slot(s) already filled this run")
            break
        if remaining < MIN_REMAINING_TO_ENTER:
            print(f"  {cand['symbol']}: stopping - remaining budget ${remaining:.0f} < ${MIN_REMAINING_TO_ENTER:.0f}")
            break
        sym = cand["symbol"]
        if sym in already_symbols:
            print(f"  {sym}: SKIP - already traded today")
            continue
        if sym in earnings_skip:
            skipped_for_earnings.append(sym)
            print(f"  {sym}: SKIP - earnings-adjacent")
            continue

        bars1 = fetch_minute_bars(sym, open_market.astimezone(timezone.utc).isoformat(), now_utc().isoformat())
        # Reference (opening) range stays the first 5 minutes - same definition
        # the backtest was run against. Shrinking it to the first 1-minute bar
        # would make it far more sensitive to ordinary noise (false breakouts)
        # and change the entry criteria from what was actually validated.
        if len(bars1) < 6:
            print(f"  {sym}: SKIP - only {len(bars1)} 1-min bars available, need >=6")
            continue
        ref_bars, scan_bars = bars1[:5], bars1[5:]
        ref_high = max(b["h"] for b in ref_bars)
        ref_low = min(b["l"] for b in ref_bars)
        # Check BOTH directions against actual bar history - don't predetermine
        # direction from cand["pctChange"], which reflects only the instant the
        # daily scan ran and can have reverted from the real intraday move by
        # the time this confirmation check runs. Scanning 1-minute bars here
        # (instead of 5-minute) only shortens how fast a confirming close is
        # noticed - the range it's being checked against is unchanged.
        confirming = None
        direction = None
        for b in scan_bars:
            if b["c"] > ref_high:
                confirming = b
                direction = "long"
                break
            if b["c"] < ref_low:
                confirming = b
                direction = "short"
                break
        if not confirming:
            print(f"  {sym}: SKIP - no breakout confirmation yet (ref range {ref_low}-{ref_high})")
            continue

        # Staleness guard - a candidate only reaches this point once its DAILY
        # z-score/pctChange crosses the screening threshold, which can happen
        # well after the actual intraday breakout bar if the move builds up
        # slowly over the day. Without this check, the full-day bar rescan
        # above would "confirm" on a long-past bar and price the trade at that
        # stale historical close - a price that was never actually fillable by
        # the time this run gets around to acting on it. Real incident 2026-
        # 10-02: WDC confirmed long at 13:43 ($424.86) but wasn't screened as
        # a candidate until ~15:02 (once its daily move finally crossed -3%),
        # by which point the real price was ~$399 - the stale $424.86 "entry"
        # had its stop already breached hours earlier in the bar history, so
        # the very next run immediately closed it. Skip instead of acting on
        # a confirmation this old; the next run will naturally pick up a
        # fresh, current breakout against the same opening range if one forms.
        confirm_dt = datetime.fromisoformat(confirming["t"].replace("Z", "+00:00"))
        age_minutes = (now_utc() - confirm_dt).total_seconds() / 60.0
        if age_minutes > STALE_CONFIRMATION_MINUTES:
            print(f"  {sym}: SKIP - confirmation at {confirming['t']} is {age_minutes:.0f} min old "
                  f"(> {STALE_CONFIRMATION_MINUTES} min staleness limit), not acting on a stale price")
            continue

        entry_price = confirming["c"]
        entry_time = confirming["t"]
        print(f"  {sym}: confirmed {direction} at {entry_price} ({entry_time})")

        # Options-liquidity pre-filter: skip candidates with no real, listed
        # option contract at all. LOOSENED 2026-09-30 (paper-testing phase,
        # want more activity) - previously also required a trade to have
        # already printed on the contract at this exact moment, which proved
        # too razor-thin live (CBOE/AIG on 2026-09-29 both had real breakouts
        # and real contracts but got rejected purely on timing). Now: a real
        # listed contract is still required (rejects genuinely non-optionable
        # names), but pricing falls back to the live bid/ask midpoint when no
        # trade has printed yet, instead of blocking the entry outright.
        contract = find_option_contract(sym, direction, entry_price, et_dt)
        if not contract:
            print(f"  {sym}: SKIP - no listed option contract found")
            continue
        opt_entry_price = fetch_option_latest_trade(contract["symbol"])
        priced_via = "trade"
        if opt_entry_price is None:
            opt_entry_price = fetch_option_latest_quote_mid(contract["symbol"])
            priced_via = "quote-mid"
        if opt_entry_price is None:
            print(f"  {sym}: SKIP - option {contract['symbol']} has no trade or quote available")
            continue
        print(f"  {sym}: options gate PASSED - {contract['symbol']} @ {opt_entry_price} (via {priced_via})")

        conv = conviction(cand["relVol"])
        ideal = ideal_dollar(cand["relVol"])
        actual = min(ideal, remaining)
        shares = max(1, round(actual / entry_price))
        stop_price = entry_price * (1 - STOP_PCT) if direction == "long" else entry_price * (1 + STOP_PCT)
        target_price = entry_price * (1 + FIRST_TARGET_PCT) if direction == "long" else entry_price * (1 - FIRST_TARGET_PCT)
        # Real options position sizing: contracts, not shares - each contract
        # controls 100 shares equivalent, so cost = premium * 100.
        opt_contracts = max(1, int(actual // (opt_entry_price * 100)))

        trade_id = f"{sym}_{today}"
        new_trade = {
            "symbol": sym, "date": today, "direction": direction,
            "entryTime": entry_time, "entryPrice": entry_price, "shares": shares,
            "convictionScore": round(conv, 4), "allocDollar": round(actual, 2),
            "stopPrice": stop_price, "targetPrice": target_price,
            "status": "open", "partialTaken": False,
            "relVol": round(cand["relVol"], 4), "pctChangeAtScreen": round(cand["pctChange"], 4),
            "dataSource": "alpaca-sip",
            "optionSymbol": contract["symbol"],
            "optionStrike": contract.get("strike_price"),
            "optionExpiration": contract.get("expiration_date"),
            "optionEntryPrice": opt_entry_price,
            "optionEntryTime": now_utc().isoformat(),
            "optionContracts": opt_contracts,
            "optionPricedVia": priced_via,
        }
        state["trades"][trade_id] = new_trade
        append_signal(signals, trade_id, "entry", symbol=sym, direction=direction,
                      optionSymbol=contract["symbol"], contracts=opt_contracts,
                      optionEntryPriceAtScreen=opt_entry_price)
        already_symbols.add(sym)
        remaining -= actual
        entries_made += 1
        changed = True

    if skipped_for_earnings:
        print(f"Skipped for earnings-adjacency: {skipped_for_earnings}")
    return changed


def print_summary(state):
    trades = list(state["trades"].values())
    closed = [t for t in trades if t.get("status") == "closed"]
    open_ = [t for t in trades if t.get("status") == "open"]

    print(f"--- SUMMARY: {len(trades)} trades logged ({len(open_)} open, {len(closed)} closed) ---")
    if not closed:
        print("No closed trades yet.")
        return

    wins = [t for t in closed if (t.get("returnPct") or 0) > 0]
    win_rate = len(wins) / len(closed) * 100
    sum_return = sum(t.get("returnPct") or 0 for t in closed)
    avg_return = sum_return / len(closed)
    sum_dollar = sum(t.get("dollarPnl") or 0 for t in closed)

    print(f"Win rate: {win_rate:.1f}% ({len(wins)}W / {len(closed) - len(wins)}L)")
    print(f"Avg return/trade: {avg_return:+.2f}%")
    print(f"Summed return: {sum_return:+.2f}%")
    print(f"Total $ P&L: ${sum_dollar:+.2f}")

    # Real options-return comparison (observational, see log_option_exit) -
    # only counts trades where a real option entry AND exit price was
    # actually captured, since the whole point is comparing real numbers,
    # not filling gaps with guesses.
    with_opts = [
        t for t in closed
        if t.get("optionEntryPrice") and t.get("optionExitPrice") is not None
        and t.get("optionReturnPct") is not None
    ]
    if with_opts:
        opt_win_rate = len([t for t in with_opts if t["optionReturnPct"] > 0]) / len(with_opts) * 100
        avg_opt_return = sum(t["optionReturnPct"] for t in with_opts) / len(with_opts)
        multiples = [
            t["optionReturnPct"] / t["returnPct"]
            for t in with_opts if t.get("returnPct")
        ]
        avg_multiple = sum(multiples) / len(multiples) if multiples else None
        print(f"--- REAL OPTIONS DATA: {len(with_opts)}/{len(closed)} closed trades with a captured option quote ---")
        print(f"Option win rate: {opt_win_rate:.1f}%")
        print(f"Avg option return/trade: {avg_opt_return:+.2f}%")
        if avg_multiple is not None:
            print(f"Avg observed multiple (option return / underlying return): {avg_multiple:.2f}x")
    else:
        print("--- REAL OPTIONS DATA: none captured yet ---")


# ---------- main ----------

def main():
    et_dt = now_et()
    state = load_state()
    state["heartbeat"] = {"lastCheckAt": now_utc().isoformat(), "lastCheckEt": et_dt.isoformat(), "status": "ok"}

    if et_dt.weekday() >= 5:
        print("Weekend - no-op (heartbeat only).")
        save_state(state)
        return

    signals = load_signals()
    signals_before = len(signals)

    changed_positions = manage_open_positions(state, et_dt, signals)
    changed_entries = new_entries(state, et_dt, signals)

    save_state(state)
    if len(signals) > signals_before:
        save_signals(signals)
        print(f"Wrote {len(signals) - signals_before} new execution signal(s).")
    print(f"Done. positions_changed={changed_positions} new_entries={changed_entries}")
    print_summary(state)


if __name__ == "__main__":
    main()
