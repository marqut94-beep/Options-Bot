import os
import sys
import json
from datetime import datetime, timedelta, timezone
import pandas as pd
import numpy as np

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.data.enums import DataFeed

# -------------------------------------------------------------------
# 1. Setup & Environment
# -------------------------------------------------------------------
API_KEY = os.getenv("ALPACA_API_KEY_ID")
SECRET_KEY = os.getenv("ALPACA_API_SECRET_KEY")

if not API_KEY or not SECRET_KEY:
    print("Error: ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY must be set.")
    sys.exit(1)

data_client = StockHistoricalDataClient(API_KEY, SECRET_KEY)

start_date_str = os.getenv("START_DATE", "2026-09-01")
end_date_str = os.getenv("END_DATE", "2026-10-01")

print(f"--- Starting Backtest ({start_date_str} to {end_date_str}) ---", flush=True)

# -------------------------------------------------------------------
# 2. Load Universe
# -------------------------------------------------------------------
UNIVERSE_FILE = "universe.json"
if os.path.exists(UNIVERSE_FILE):
    try:
        with open(UNIVERSE_FILE, "r", encoding="utf-8-sig") as f:
            universe = json.load(f)
    except Exception as e:
        print(f"Error reading {UNIVERSE_FILE}: {e}", flush=True)
        sys.exit(1)
else:
    universe = ["AAPL", "TSLA", "NVDA", "AMD", "SPY", "QQQ"]

print(f"Loaded {len(universe)} symbols from {UNIVERSE_FILE}", flush=True)

# -------------------------------------------------------------------
# 3. Filtered Backtest Engine
# -------------------------------------------------------------------
trade_logs = []

# Strategy Parameters
STOP_LOSS_PCT = 0.02    # 2% Stop Loss
TAKE_PROFIT_PCT = 0.04   # 4% Take Profit
MIN_PRICE = 5.00         # Filter out penny stocks under $5

def run_backtest():
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    current_chunk_start = start_dt
    
    while current_chunk_start < end_dt:
        current_chunk_end = min(current_chunk_start + timedelta(days=7), end_dt)
        print(f"Processing chunk: {current_chunk_start.strftime('%Y-%m-%d')} -> {current_chunk_end.strftime('%Y-%m-%d')}", flush=True)

        min_req = StockBarsRequest(
            symbol_or_symbols=universe,
            timeframe=TimeFrame.Minute,
            start=current_chunk_start,
            end=current_chunk_end,
            feed=DataFeed.IEX
        )

        try:
            bars = data_client.get_stock_bars(min_req)
            if not bars or not bars.data:
                current_chunk_start = current_chunk_end + timedelta(days=1)
                continue
            df = bars.df
        except Exception:
            current_chunk_start = current_chunk_end + timedelta(days=1)
            continue

        if df.empty:
            current_chunk_start = current_chunk_end + timedelta(days=1)
            continue

        df = df.reset_index()
        df['date'] = pd.to_datetime(df['timestamp']).dt.date
        unique_dates = df['date'].unique()

        for day in unique_dates:
            day_data = df[df['date'] == day]
            
            for sym in universe:
                sym_data = day_data[day_data['symbol'] == sym].sort_values('timestamp')
                
                # Require at least 30 minutes of intraday price data
                if len(sym_data) < 30:
                    continue

                first_price = sym_data.iloc[0]['close']
                if first_price < MIN_PRICE:
                    continue

                # 1. Establish Opening 5-Min High Watermark & Volume Baseline
                opening_5m = sym_data.head(5)
                high_watermark = opening_5m['high'].max()
                avg_opening_vol = opening_5m['volume'].mean()

                # 2. Only look for Breakout Entries between 9:35 AM and 10:30 AM (bars 5 to 60)
                morning_window = sym_data.iloc[5:60]
                
                # Entry condition: Price breaks watermark AND Volume is 1.8x opening volume
                breakout_candidates = morning_window[
                    (morning_window['close'] > high_watermark) & 
                    (morning_window['volume'] > (avg_opening_vol * 1.8))
                ]

                if breakout_candidates.empty:
                    continue  # Skip stock for today if no valid momentum breakout

                # Trigger Entry
                entry_bar = breakout_candidates.iloc[0]
                entry_price = entry_bar['close']
                entry_idx = entry_bar.name

                # 3. Simulate Trade Management (Stop-Loss / Take-Profit / EOD Exit)
                remaining_bars = sym_data.loc[entry_idx + 1:]
                
                exit_price = entry_price
                exit_reason = "EOD"

                for _, bar in remaining_bars.iterrows():
                    current_high = bar['high']
                    current_low = bar['low']

                    # Check Take Profit
                    if current_high >= entry_price * (1 + TAKE_PROFIT_PCT):
                        exit_price = entry_price * (1 + TAKE_PROFIT_PCT)
                        exit_reason = "TP"
                        break

                    # Check Stop Loss
                    if current_low <= entry_price * (1 - STOP_LOSS_PCT):
                        exit_price = entry_price * (1 - STOP_LOSS_PCT)
                        exit_reason = "SL"
                        break
                else:
                    # Exit at EOD close if neither TP nor SL triggered
                    if not remaining_bars.empty:
                        exit_price = remaining_bars.iloc[-1]['close']

                pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                pnl_dollars = (exit_price - entry_price) * 100

                trade_logs.append({
                    'date': str(day),
                    'symbol': sym,
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'pnl_pct': pnl_pct,
                    'pnl_dollars': pnl_dollars,
                    'win': pnl_dollars > 0
                })

        current_chunk_start = current_chunk_end + timedelta(days=1)

    # -------------------------------------------------------------------
    # 4. Summary Output
    # -------------------------------------------------------------------
    print("\n" + "="*40, flush=True)
    print("        BACKTEST SUMMARY RESULTS        ", flush=True)
    print("="*40, flush=True)
    
    if not trade_logs:
        print("No trades triggered matching strategy filters.", flush=True)
        print("="*40, flush=True)
        return

    df_trades = pd.DataFrame(trade_logs)
    total_trades = len(df_trades)
    wins = df_trades['win'].sum()
    losses = total_trades - wins
    win_rate = (wins / total_trades) * 100
    total_pnl = df_trades['pnl_dollars'].sum()
    avg_pnl = df_trades['pnl_dollars'].mean()

    print(f"Date Range:          {start_date_str} to {end_date_str}", flush=True)
    print(f"Total Trades Logged: {total_trades}", flush=True)
    print(f"Win Rate:            {win_rate:.2f}% ({wins}W / {losses}L)", flush=True)
    print(f"Total Net P&L:       ${total_pnl:,.2f}", flush=True)
    print(f"Avg P&L per Trade:   ${avg_pnl:,.2f}", flush=True)
    print("="*40, flush=True)

if __name__ == "__main__":
    run_backtest()
