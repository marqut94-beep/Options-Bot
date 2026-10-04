import os
import sys
import json
from datetime import datetime, timedelta
import pandas as pd

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

start_date_str = os.getenv("START_DATE", "2026-08-01")
end_date_str = os.getenv("END_DATE", "2026-10-01")

print(f"--- Starting Backtest ({start_date_str} to {end_date_str}) ---")

# -------------------------------------------------------------------
# 2. Load Universe (UTF-8 BOM Handling)
# -------------------------------------------------------------------
UNIVERSE_FILE = "universe.json"
if os.path.exists(UNIVERSE_FILE):
    try:
        with open(UNIVERSE_FILE, "r", encoding="utf-8-sig") as f:
            universe = json.load(f)
    except Exception as e:
        print(f"Error reading {UNIVERSE_FILE}: {e}")
        sys.exit(1)
else:
    universe = ["AAPL", "TSLA", "NVDA", "AMD", "SPY", "QQQ"]

print(f"Loaded {len(universe)} symbols from {UNIVERSE_FILE}: {universe}")

# -------------------------------------------------------------------
# 3. Monthly Chunked Backtest Engine
# -------------------------------------------------------------------
trade_logs = []

def run_backtest():
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d")

    current_chunk_start = start_dt
    
    # Process range in 30-day blocks to prevent API timeouts
    while current_chunk_start < end_dt:
        current_chunk_end = min(current_chunk_start + timedelta(days=30), end_dt)
        print(f"\nProcessing chunk: {current_chunk_start.strftime('%Y-%m-%d')} -> {current_chunk_end.strftime('%Y-%m-%d')}")

        min_req = StockBarsRequest(
            symbol_or_symbols=universe,
            timeframe=TimeFrame.Minute,
            start=current_chunk_start,
            end=current_chunk_end,
            feed=DataFeed.IEX  # Explicitly required for Paper Keys (PK...)
        )

        try:
            bars = data_client.get_stock_bars(min_req)
            df = bars.df
        except Exception as e:
            print(f"Error fetching chunk: {e}")
            current_chunk_start = current_chunk_end + timedelta(days=1)
            continue

        if df.empty:
            print("No data returned for this window.")
            current_chunk_start = current_chunk_end + timedelta(days=1)
            continue

        # Process dates locally in Pandas
        df = df.reset_index()
        df['date'] = pd.to_datetime(df['timestamp']).dt.date
        unique_dates = df['date'].unique()

        for day in unique_dates:
            day_data = df[df['date'] == day]
            
            for sym in universe:
                sym_data = day_data[day_data['symbol'] == sym]
                if sym_data.empty:
                    continue

                # 5-min High Watermark Breakout
                high_watermark = sym_data['high'].head(5).max()
                breakouts = sym_data[sym_data['close'] > high_watermark]

                if not breakouts.empty:
                    entry_price = breakouts.iloc[0]['close']
                    exit_price = sym_data.iloc[-1]['close']
                    pnl_pct = ((exit_price - entry_price) / entry_price) * 100
                    pnl_dollars = (exit_price - entry_price) * 100

                    trade_logs.append({
                        'date': str(day),
                        'symbol': sym,
                        'entry_price': entry_price,
                        'exit_price': exit_price,
                        'pnl_pct': pnl_pct,
                        'pnl_dollars': pnl_dollars,
                        'win': pnl_dollars > 0
                    })

        current_chunk_start = current_chunk_end + timedelta(days=1)

    # -------------------------------------------------------------------
    # 4. Summary Output
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

    print(f"Date Range:          {start_date_str} to {end_date_str}")
    print(f"Total Trades Logged: {total_trades}")
    print(f"Win Rate:            {win_rate:.2f}% ({wins}W / {losses}L)")
    print(f"Total Net P&L:       ${total_pnl:,.2f}")
    print(f"Avg P&L per Trade:   ${avg_pnl:,.2f}")
    print("="*40)

if __name__ == "__main__":
    run_backtest()
