#!/usr/bin/env python3
"""
Momentum Paper Desk - external runner.

Runs the same strategy as the Claude Code routine (2x relVol / 3% move breakout,
two-stage -2%/+4%/floor+2%/target+6% exit, $15k daily conviction-weighted budget,
3 trades/day cap, earnings-adjacency skip with backfill) entirely outside Claude
Code, using only Alpaca market data. State persists in state.json, which the
GitHub Actions workflow commits back to the repo after every run.

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

ALPACA_KEY = os.environ.get("ALPACA_API_KEY_ID")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET_KEY")
FINNHUB_KEY = os.environ.get("FINNHUB_API_KEY")

ALPACA_DATA = "https://data.alpaca.markets/v2"
FINNHUB_BASE = "https://finnhub.io/api/v1"

DAILY_BUDGET = 15000.0
MAX_TRADES_PER_DAY = 3
MIN_REMAINING_TO_ENTER = 3000.0
STOP_PCT = 0.02
FIRST_TARGET_PCT = 0.04
REMAINDER_FLOOR_PCT = 0.02
REMAINDER_TARGET_PCT = 0.06
REL_VOL_MIN = 2.0
PCT_CHANGE_MIN = 3.0
PRICE_MIN, PRICE_MAX = 10.0, 500.0

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
    STATE_PATH.write_text(json.dumps(state, indent=2, default=str))


def load_universe():
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8-sig"))


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


def fetch_5min_bars(symbol, start, end):
    url = f"{ALPACA_DATA}/stocks/{symbol}/bars"
    params = {"timeframe": "5Min", "start": start, "end": end, "feed": "sip", "limit": 1000}
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    r.raise_for_status()
    return r.json().get("bars", [])


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
    return max(0.0, min(1.0, (rel_vol - 2.0) / 6.0))


def ideal_dollar(rel_vol):
    return 3000.0 + 4500.0 * conviction(rel_vol)


# ---------- Step 1: manage open positions ----------

def manage_open_positions(state, et_dt):
    changed = False
    for trade_id, t in list(state["trades"].items()):
        if t.get("status") != "open":
            continue

        direction = t["direction"]
        shares = t["shares"]
        entry_price = t["entryPrice"]

        if not t.get("partialTaken"):
            start = t["entryTime"]
            bars = fetch_5min_bars(t["symbol"], start, now_utc().isoformat())
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
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=stop_price,
                         exitReason="stop", returnPct=-STOP_PCT * 100,
                         dollarPnl=round(pnl, 2))
                changed = True
            elif triggered and triggered[0] == "target":
                shares_partial = max(1, shares // 2) if shares > 1 else 1
                shares_remainder = shares - shares_partial
                if shares_remainder == 0:
                    pnl = shares * (target_price - entry_price) if direction == "long" else shares * (entry_price - target_price)
                    t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=target_price,
                             exitReason="target", returnPct=FIRST_TARGET_PCT * 100, dollarPnl=round(pnl, 2))
                else:
                    partial_pnl = shares_partial * (target_price - entry_price) if direction == "long" else shares_partial * (entry_price - target_price)
                    floor = entry_price * (1 + REMAINDER_FLOOR_PCT) if direction == "long" else entry_price * (1 - REMAINDER_FLOOR_PCT)
                    rtarget = entry_price * (1 + REMAINDER_TARGET_PCT) if direction == "long" else entry_price * (1 - REMAINDER_TARGET_PCT)
                    t.update(partialTaken=True, partialExitPrice=target_price,
                             partialExitTime=triggered[1]["t"], partialDollarPnl=round(partial_pnl, 2),
                             sharesPartial=shares_partial, sharesRemainder=shares_remainder,
                             remainderFloor=floor, remainderTarget=rtarget)
                changed = True
            elif is_market_hours(et_dt) and et_dt.hour == 15 and et_dt.minute >= 55 or et_dt.hour >= 16:
                # EOD flatten - use last bar close as proxy for a live quote
                last_price = bars[-1]["c"] if bars else entry_price
                pnl = shares * (last_price - entry_price) if direction == "long" else shares * (entry_price - last_price)
                ret = (pnl / (shares * entry_price)) * 100
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=last_price,
                         exitReason="eod", returnPct=round(ret, 4), dollarPnl=round(pnl, 2))
                changed = True

        else:
            start = t["partialExitTime"]
            bars = fetch_5min_bars(t["symbol"], start, now_utc().isoformat())
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
            elif is_market_hours(et_dt) and (et_dt.hour == 15 and et_dt.minute >= 55 or et_dt.hour >= 16):
                resolved_price = bars[-1]["c"] if bars else t["partialExitPrice"]
                reason = "eod"

            if resolved_price is not None:
                remainder_pnl = shares_remainder * (resolved_price - entry_price) if direction == "long" else shares_remainder * (entry_price - resolved_price)
                blended_pnl = t["partialDollarPnl"] + remainder_pnl
                blended_ret = (blended_pnl / (shares * entry_price)) * 100
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=resolved_price,
                         exitReason=reason, returnPct=round(blended_ret, 4), dollarPnl=round(blended_pnl, 2))
                changed = True

    return changed


# ---------- Step 2: new entries ----------

def new_entries(state, et_dt):
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

    earnings_skip = fetch_earnings_skip_set(et_dt)

    universe = load_universe()
    end = now_utc()
    start = end - timedelta(days=45)
    daily = fetch_daily_bars_batch(universe, start.isoformat(), end.isoformat())

    candidates = []
    for sym, bars in daily.items():
        if len(bars) < 31:
            continue
        today_bar = bars[-1]
        avg_vol_30 = sum(b["v"] for b in bars[-31:-1]) / 30.0
        if avg_vol_30 <= 0:
            continue
        rel_vol = today_bar["v"] / avg_vol_30
        pct_change = (today_bar["c"] - today_bar["o"]) / today_bar["o"] * 100
        price = today_bar["c"]
        if rel_vol >= REL_VOL_MIN and abs(pct_change) >= PCT_CHANGE_MIN and PRICE_MIN <= price <= PRICE_MAX:
            candidates.append({"symbol": sym, "relVol": rel_vol, "pctChange": pct_change})

    candidates.sort(key=lambda c: c["relVol"], reverse=True)

    changed = False
    entries_made = 0
    skipped_for_earnings = []
    open_market = et_dt.replace(hour=9, minute=30, second=0, microsecond=0)

    for cand in candidates:
        if entries_made >= MAX_TRADES_PER_DAY - len(todays_trades):
            break
        if remaining < MIN_REMAINING_TO_ENTER:
            break
        sym = cand["symbol"]
        if sym in already_symbols:
            continue
        if sym in earnings_skip:
            skipped_for_earnings.append(sym)
            continue

        direction = "long" if cand["pctChange"] > 0 else "short"
        bars5 = fetch_5min_bars(sym, open_market.astimezone(timezone.utc).isoformat(), now_utc().isoformat())
        if len(bars5) < 2:
            continue
        ref_high, ref_low = bars5[0]["h"], bars5[0]["l"]
        confirming = None
        for b in bars5[1:]:
            if direction == "long" and b["c"] > ref_high:
                confirming = b
                break
            if direction == "short" and b["c"] < ref_low:
                confirming = b
                break
        if not confirming:
            continue

        entry_price = confirming["c"]
        entry_time = confirming["t"]
        conv = conviction(cand["relVol"])
        ideal = ideal_dollar(cand["relVol"])
        actual = min(ideal, remaining)
        shares = max(1, round(actual / entry_price))
        stop_price = entry_price * (1 - STOP_PCT) if direction == "long" else entry_price * (1 + STOP_PCT)
        target_price = entry_price * (1 + FIRST_TARGET_PCT) if direction == "long" else entry_price * (1 - FIRST_TARGET_PCT)

        trade_id = f"{sym}_{today}"
        state["trades"][trade_id] = {
            "symbol": sym, "date": today, "direction": direction,
            "entryTime": entry_time, "entryPrice": entry_price, "shares": shares,
            "convictionScore": round(conv, 4), "allocDollar": round(actual, 2),
            "stopPrice": stop_price, "targetPrice": target_price,
            "status": "open", "partialTaken": False,
            "relVol": round(cand["relVol"], 4), "pctChangeAtScreen": round(cand["pctChange"], 4),
            "dataSource": "alpaca-sip",
        }
        already_symbols.add(sym)
        remaining -= actual
        entries_made += 1
        changed = True

    if skipped_for_earnings:
        print(f"Skipped for earnings-adjacency: {skipped_for_earnings}")
    return changed


# ---------- main ----------

def main():
    et_dt = now_et()
    state = load_state()
    state["heartbeat"] = {"lastCheckAt": now_utc().isoformat(), "lastCheckEt": et_dt.isoformat(), "status": "ok"}

    if et_dt.weekday() >= 5:
        print("Weekend - no-op (heartbeat only).")
        save_state(state)
        return

    changed_positions = manage_open_positions(state, et_dt)
    changed_entries = new_entries(state, et_dt)

    save_state(state)
    print(f"Done. positions_changed={changed_positions} new_entries={changed_entries}")


if __name__ == "__main__":
    main()
