import os
import sys
import json
from datetime import datetime, timedelta
import pandas as pd
import numpy as np

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# -------------------------------------------------------------------
# 1. Environment & Parameter Setup
# -------------------------------------------------------------------
API_KEY = os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    print("Error: ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set.")
    sys.exit(1)

# Initialize Alpaca Client
data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

# Read dates from Environment Variables (set by GitHub Actions) with fallback defaults
start_date_str = os.getenv("START_DATE", "2026-08-01")
end_date_str = os.getenv("END_DATE", "2026-10-01")

print(f"Running Backtest from {start_date_str} to {end_date_str}...")

# -------------------------------------------------------------------
# 2. Load Stock Universe
# -------------------------------------------------------------------
UNIVERSE_FILE = "universe.json"
if os.path.exists(UNIVERSE_FILE):
    with open(UNIVERSE_FILE, "r") as f:
        universe = json.load(f)
else:
    # Default fallback list if universe.json is missing
    universe = ["AAPL", "TSLA", "NVDA", "AMD", "SPY", "QQQ"]

print(f"Loaded {len(universe)} symbols from {UNIVERSE_FILE}: {universe}")

# -------------------------------------------------------------------
# 3. Strategy & Execution Simulation
# -------------------------------------------------------------------
trade_logs = []

def run_backtest():
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")

    # Step A: Fetch Daily Bars for Initial Screen (IEX Feed)
    daily_req = StockBarsRequest(
        symbol_or_symbols=universe,
        timeframe=TimeFrame.Day,
        start=start_dt - timedelta(days=60),
        end=end_dt,
        feed=DataFeed.IEX  # Explicitly required for Paper Keys (PK...)
    )

    try:
        daily_bars = data_client.get_stock_bars(daily_req)
        daily_df = daily_bars.df
    except Exception as e:
        print(f"Error fetching daily bars: {e}")
        sys.exit(1)

    if daily_df.empty:
        print("No daily bar data retrieved.")
        return

    # Iterate through each trading day in date range
    trading_days = pd.date_range(start=start_dt, end=end_dt, freq='B')

    for current_day in trading_days:
        day_str = current_day.strftime("%Y-%m-%d")
        
        for sym in universe:
            if sym not in daily_df.index.get_level_values('symbol'):
                continue
            
            # Step B: Fetch Intraday 1-Min Bars for Target Ticker & Day (IEX Feed)
            day_start = datetime.combine(current_day.date(), datetime.min.time())
            day_end = datetime.combine(current_day.date(), datetime.max.time())
            
            min_req = StockBarsRequest(
                symbol_or_symbols=sym,
                timeframe=TimeFrame.Minute,
                start=day_start,
                end=day_end,
                feed=DataFeed.IEX  # Explicitly required for Paper Keys (PK...)
            )

            try:
                min_bars = data_client.get_stock_bars(min_req)
                min_df = min_bars.df
            except Exception:
                continue

            if min_df.empty:
                continue

            # Simulate Intraday Breakout Logic
            symbol_mins = min_df.xs(sym, level='symbol')
            
            # Simple 5-min Breakout Simulation
            high_watermark = symbol_mins['high'].head(5).max()
            breakout_bars = symbol_mins[symbol_mins['close'] > high_watermark]

            if not breakout_bars.empty:
                entry_time = breakout_bars.index[0]
                entry_price = breakout_bars.iloc[0]['close']
                
                # Evaluate Exit (End of Day or Target/Stop)
                exit_price = symbol_mins.iloc[-1]['close']
                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                pnl_dollars = (exit_price - entry_price) * 100  # Assume 100 shares

                trade_logs.append({
                    'date': day_str,
                    'symbol': sym,
                    'entry_time': entry_time,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl_pct': pnl_pct,
                    'pnl_dollars': pnl_dollars,
                    'win': pnl_dollars > 0
                })

    # -------------------------------------------------------------------
    # 4. Summary Reporting
    # -------------------------------------------------------------------
    print("\n" + "="*40)
    print("        BACKTEST SUMMARY RESULTS        ")
    print("="*40)
    
    if not trade_logs:
        print("No trades triggered for the selected parameters.")
        print("="*40)
        return

    df_trades = pd.DataFrame(trade_logs)
    total_trades = len(df_trades)
    wins = df_trades['win'].sum()
    losses = total_trades - wins
    win_rate = (wins / total_trades) * 100
    total_pnl = df_trades['pnl_dollars'].sum()
    avg_pnl = df_trades['pnl_dollars'].mean()
    avg_return_pct = df_trades['pnl_pct'].mean()

    print(f"Date Range:          {start_date_str} to {end_date_str}")
    print(f"Total Trades Logged: {total_trades}")
    print(f"Win Rate:            {win_rate:.2f}% ({wins}W / {losses}L)")
    print(f"Total Net P&L:       ${total_pnl:,.2f}")
    print(f"Avg P&L per Trade:   ${avg_pnl:,.2f}")
    print(f"Avg Return %:        {avg_return_pct:.2f}%")
    print("="*40)

if __name__ == "__main__":
    run_backtest()
