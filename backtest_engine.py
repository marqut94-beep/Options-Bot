import json
import os
import sys
from datetime import datetime, timedelta, time
import pandas as pd
import numpy as np
from pathlib import Path
import requests
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# Configuration & Parameters
ALPACA_KEY = os.environ.get("ALPACA_API_KEY_ID")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET_KEY")

DAILY_BUDGET = 25000.0
MAX_TRADES_PER_DAY = 4
MIN_REMAINING_TO_ENTER = 5000.0

# Scaled Risk Management Rules (-3% Stop, +4% Partial Target, +2% Floor, +10% Remainder Target)
STOP_PCT = 0.03            # -3% Stop Loss
FIRST_TARGET_PCT = 0.04    # +4% Partial Take Profit (50% position)
REMAINDER_FLOOR_PCT = 0.02 # +2% Trailing Profit Floor
REMAINDER_TARGET_PCT = 0.10# +10% Full Target

# Universe Filters
REL_VOL_MIN = 2.0
PCT_CHANGE_MIN = 3.0
PRICE_MIN, PRICE_MAX = 10.0, 500.0

HERE = Path(__file__).parent
UNIVERSE_PATH = HERE / "universe.json"

if not ALPACA_KEY or not ALPACA_SECRET:
    print("FATAL: ALPACA_API_KEY_ID / ALPACA_API_SECRET_KEY not set.")
    sys.exit(1)

client = StockHistoricalDataClient(ALPACA_KEY, ALPACA_SECRET)
HEADERS = {"APCA-API-KEY-ID": ALPACA_KEY, "APCA-API-SECRET-KEY": ALPACA_SECRET}
ALPACA_TRADING = "https://paper-api.alpaca.markets/v2"

def load_universe():
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8-sig"))

def conviction(rel_vol):
    return max(0.0, min(1.0, (rel_vol - 2.0) / 12.0))

def ideal_dollar(rel_vol):
    return 5000.0 + 7500.0 * conviction(rel_vol)

def find_option_contract(symbol, direction, ref_price, date_str):
    """Find active or historical option contract within 14 days expiration."""
    option_type = "call" if direction == "long" else "put"
    et_dt = datetime.strptime(date_str, "%Y-%m-%d")
    exp_lte = (et_dt + timedelta(days=14)).strftime("%Y-%m-%d")
    url = f"{ALPACA_TRADING}/options/contracts"
    
    for st in ["active", "inactive"]:
        params = {
            "underlying_symbols": symbol,
            "expiration_date_gte": date_str,
            "expiration_date_lte": exp_lte,
            "type": option_type,
            "status": st,
            "limit": 100,
        }
        try:
            r = requests.get(url, headers=HEADERS, params=params, timeout=10)
            if r.status_code == 200:
                contracts = r.json().get("option_contracts", [])
                if contracts:
                    def strike_dist(c):
                        try:
                            return abs(float(c["strike_price"]) - ref_price)
                        except Exception:
                            return float("inf")

                    contracts.sort(key=lambda c: (strike_dist(c), c.get("expiration_date", "")))
                    return contracts[0]
        except Exception:
            continue
            
    return None

def run_backtest(start_date, end_date):
    universe = load_universe()
    
    print(f"Fetching daily bars for backtest period: {start_date} to {end_date}...")
    req = StockBarsRequest(
        symbol_or_symbols=universe,
        timeframe=TimeFrame.Day,
        start=datetime.strptime(start_date, "%Y-%m-%d") - timedelta(days=60),
        end=datetime.strptime(end_date, "%Y-%m-%d")
    )
    daily_bars_df = client.get_stock_bars(req).df.reset_index()
    daily_bars_df['date'] = daily_bars_df['timestamp'].dt.date
    trading_days = sorted([d for d in daily_bars_df['date'].unique() if d >= datetime.strptime(start_date, "%Y-%m-%d").date()])
    
    all_trades = []
    
    for current_date in trading_days:
        date_str = current_date.strftime("%Y-%m-%d")
        print(f"\n--- Backtesting Date: {date_str} ---")
        
        # 1. Screen Universe using 30-Day Volume Z-Score
        candidates = []
        for sym in universe:
            sym_df = daily_bars_df[daily_bars_df['symbol'] == sym].sort_values('timestamp')
            past_df = sym_df[sym_df['date'] < current_date]
            today_df = sym_df[sym_df['date'] == current_date]
            
            if len(past_df) < 30 or today_df.empty:
                continue
                
            today_bar = today_df.iloc[0]
            window = past_df.tail(30)
            vols = window['volume'].values
            mean_vol = np.mean(vols)
            stdev_vol = np.std(vols, ddof=0)
            
            if stdev_vol <= 0:
                continue
                
            rel_vol = (today_bar['volume'] - mean_vol) / stdev_vol
            pct_change = (today_bar['close'] - today_bar['open']) / today_bar['open'] * 100.0
            price = today_bar['close']
            
            if rel_vol >= REL_VOL_MIN and abs(pct_change) >= PCT_CHANGE_MIN and PRICE_MIN <= price <= PRICE_MAX:
                candidates.append({
                    "symbol": sym,
                    "relVol": rel_vol,
                    "pctChange": pct_change
                })
                
        candidates.sort(key=lambda x: x["relVol"], reverse=True)
        if not candidates:
            continue
            
        # 2. Intraday Minute Bar Execution
        todays_trades = 0
        allocated = 0.0
        remaining = DAILY_BUDGET
        
        for cand in candidates:
            if todays_trades >= MAX_TRADES_PER_DAY or remaining < MIN_REMAINING_TO_ENTER:
                break
                
            sym = cand["symbol"]
            start_dt = datetime.combine(current_date, time(9, 30))
            end_dt = datetime.combine(current_date, time(16, 0))
            
            try:
                min_req = StockBarsRequest(
                    symbol_or_symbols=sym,
                    timeframe=TimeFrame.Minute,
                    start=start_dt,
                    end=end_dt
                )
                min_df = client.get_stock_bars(min_req).df.reset_index().sort_values('timestamp')
            except Exception:
                continue
                
            if len(min_df) < 6:
                continue
                
            ref_bars = min_df.iloc[:5]
            scan_bars = min_df.iloc[5:]
            
            ref_high = ref_bars['high'].max()
            ref_low = ref_bars['low'].min()
            
            confirming = None
            direction = None
            
            for idx, bar in scan_bars.iterrows():
                if bar['close'] > ref_high:
                    confirming = bar
                    direction = "long"
                    break
                elif bar['close'] < ref_low:
                    confirming = bar
                    direction = "short"
                    break
                    
            if confirming is None:
                continue
                
            entry_price = confirming['close']
            entry_time = confirming['timestamp']

            # Option Contract Verification
            opt_contract = find_option_contract(sym, direction, entry_price, date_str)
            if not opt_contract:
                print(f"  {sym}: SKIP - No option contract found for {date_str}.")
                continue

            actual_alloc = min(ideal_dollar(cand["relVol"]), remaining)
            shares = max(1, round(actual_alloc / entry_price))
            
            stop_price = entry_price * (1 - STOP_PCT) if direction == "long" else entry_price * (1 + STOP_PCT)
            target_price = entry_price * (1 + FIRST_TARGET_PCT) if direction == "long" else entry_price * (1 - FIRST_TARGET_PCT)
            
            # Simulate Scaled Trade Management
            post_entry_bars = min_df[min_df['timestamp'] > entry_time]
            
            partial_taken = False
            partial_pnl = 0.0
            shares_partial = max(1, shares // 2) if shares > 1 else 1
            shares_remainder = shares - shares_partial
            
            trade_closed = False
            final_pnl = 0.0
            exit_reason = ""
            
            remainder_floor = entry_price * (1 + REMAINDER_FLOOR_PCT) if direction == "long" else entry_price * (1 - REMAINDER_FLOOR_PCT)
            remainder_target = entry_price * (1 + REMAINDER_TARGET_PCT) if direction == "long" else entry_price * (1 - REMAINDER_TARGET_PCT)
            
            for _, b in post_entry_bars.iterrows():
                lo, hi = b['low'], b['high']
                
                # Phase 1: Prior to 4% Target
                if not partial_taken:
                    stop_hit = lo <= stop_price if direction == "long" else hi >= stop_price
                    target_hit = hi >= target_price if direction == "long" else lo <= target_price
                    
                    if stop_hit:
                        final_pnl = shares * (stop_price - entry_price) if direction == "long" else shares * (entry_price - stop_price)
                        exit_reason = "stop"
                        trade_closed = True
                        break
                    elif target_hit:
                        if shares_remainder == 0:
                            final_pnl = shares * (target_price - entry_price) if direction == "long" else shares * (entry_price - target_price)
                            exit_reason = "target"
                            trade_closed = True
                            break
                        else:
                            partial_pnl = shares_partial * (target_price - entry_price) if direction == "long" else shares_partial * (entry_price - target_price)
                            partial_taken = True
                
                # Phase 2: Post 4% Target (Managing Remainder)
                else:
                    floor_hit = lo <= remainder_floor if direction == "long" else hi >= remainder_floor
                    rtarget_hit = hi >= remainder_target if direction == "long" else lo <= remainder_target
                    
                    if floor_hit or rtarget_hit:
                        exit_p = remainder_floor if floor_hit else remainder_target
                        rem_pnl = shares_remainder * (exit_p - entry_price) if direction == "long" else shares_remainder * (entry_price - exit_p)
                        final_pnl = partial_pnl + rem_pnl
                        exit_reason = "remainder_floor" if floor_hit else "remainder_target"
                        trade_closed = True
                        break
                        
            # Phase 3: End of Day Flatten (EOD)
            if not trade_closed and not post_entry_bars.empty:
                last_bar = post_entry_bars.iloc[-1]
                last_price = last_bar['close']
                if not partial_taken:
                    final_pnl = shares * (last_price - entry_price) if direction == "long" else shares * (entry_price - last_price)
                else:
                    rem_pnl = shares_remainder * (last_price - entry_price) if direction == "long" else shares_remainder * (entry_price - last_price)
                    final_pnl = partial_pnl + rem_pnl
                exit_reason = "eod"

            ret_pct = (final_pnl / actual_alloc) * 100.0
            all_trades.append({
                "date": current_date,
                "symbol": sym,
                "direction": direction,
                "entryPrice": entry_price,
                "optionSymbol": opt_contract["symbol"],
                "allocDollar": actual_alloc,
                "dollarPnl": final_pnl,
                "returnPct": ret_pct,
                "exitReason": exit_reason
            })
            
            print(f"  {sym}: TRADED -> {direction.upper()} ({opt_contract['symbol']}) | P&L: ${final_pnl:.2f}")
            todays_trades += 1
            allocated += actual_alloc
            remaining -= actual_alloc

    # Backtest Summary Report
    tdf = pd.DataFrame(all_trades)
    if tdf.empty:
        print("\nNo trades triggered during backtest period.")
        return
        
    wins = tdf[tdf['dollarPnl'] > 0]
    win_rate = len(wins) / len(tdf) * 100.0
    total_pnl = tdf['dollarPnl'].sum()
    avg_trade_pnl = tdf['dollarPnl'].mean()
    
    print("\n" + "="*45)
    print("      ALIGNED BACKTEST SUMMARY RESULTS      ")
    print("="*45)
    print(f"Total Trades Logged: {len(tdf)}")
    print(f"Win Rate:             {win_rate:.2f}% ({len(wins)}W / {len(tdf)-len(wins)}L)")
    print(f"Total Net P&L:       ${total_pnl:,.2f}")
    print(f"Avg P&L per Trade:   ${avg_trade_pnl:,.2f}")
    print(f"Avg Return %:        {tdf['returnPct'].mean():.2f}%")
    print("="*45)

if __name__ == "__main__":
    run_backtest("2026-01-01", "2026-10-01")
