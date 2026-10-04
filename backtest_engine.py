import os
import sys
import json
from datetime import datetime, timedelta, timezone
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

start_date_str = os.getenv("START_DATE", "2026-09-01")
end_date_str = os.getenv("END_DATE", "2026-10-01")

print(f"--- Starting Selective Backtest ({start_date_str} to {end_date_str}) ---", flush=True)

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
# 3. Strategy Parameters
# -------------------------------------------------------------------
STOP_LOSS_PCT = 0.02         # 2% Stop Loss
TAKE_PROFIT_PCT = 0.04        # 4% Take Profit
MIN_PRICE = 10.00             # Require minimum $10 stock price
MIN_OPENING_VOLUME = 50000    # Minimum 5-min opening volume (filters out illiquid stocks)
MAX_TRADES_PER_DAY = 4        # Hard limit of 4 trades per calendar day

trade_logs = []

def run_backtest():
    start_dt = datetime.strptime(start_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    current_chunk_start = start_dt
    
    # 7-day chunks to keep API requests fast and prevent rate-limiting/timeouts
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
        except Exception as e:
            print(f"  -> Fetch error: {e}", flush=True)
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
            daily_trade_count = 0  # Reset daily trade counter
            
            for sym in universe:
                # Stop checking symbols for today if daily max trade limit is reached
                if daily_trade_count >= MAX_TRADES_PER_DAY:
                    break

                sym_data = day_data[day_data['symbol'] == sym].sort_values('timestamp').reset_index(drop=True)
                
                # Must have at least 60 minutes of intraday data
                if len(sym_data) < 60:
                    continue

                open_price = sym_data.iloc[0]['open']
                if open_price < MIN_PRICE:
                    continue

                # A. Opening 5-Minute Range & Liquidity Filter
                opening_5m = sym_data.iloc[:5]
                total_opening_vol = opening_5m['volume'].sum()
                
                if total_opening_vol < MIN_OPENING_VOLUME:
                    continue

                high_watermark = opening_5m['high'].max()
                avg_1m_vol = opening_5m['volume'].mean()

                # B. Restrict Breakout Window to 9:35 AM - 10:30 AM (Bars 5 to 60)
                morning_bars = sym_data.iloc[5:60]
                
                # Entry Condition: Price breaks watermark AND Volume is 2.5x opening average
                breakout_mask = (morning_bars['close'] > high_watermark) & (morning_bars['volume'] >= (avg_1m_vol * 2.5))
                breakout_indices = morning_bars[breakout_mask].index

                if len(breakout_indices) == 0:
                    continue  # No valid breakout for this symbol today

                # Take first valid entry bar
                entry_idx = breakout_indices[0]
                entry_bar = sym_data.loc[entry_idx]
                entry_price = entry_bar['close']

                # C. Manage Trade Exit (2% Stop-Loss / 4% Take-Profit / EOD)
                remaining_bars = sym_data.loc[entry_idx + 1:]
                exit_price = entry_price
                exit_reason = "EOD"

                for _, bar in remaining_bars.iterrows():
                    if bar['low'] <= entry_price * (1 - STOP_LOSS_PCT):
                        exit_price = entry_price * (1 - STOP_LOSS_PCT)
                        exit_reason = "SL"
                        break
                    if bar['high'] >= entry_price * (1 + TAKE_PROFIT_PCT):
                        exit_price = entry_price * (1 + TAKE_PROFIT_PCT)
                        exit_reason = "TP"
                        break
                else:
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

                daily_trade_count += 1  # Increment trade count for the day

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
    print(f"Total Trades Logged: {total_trades}")
    print(f"Win Rate:            {win_rate:.2f}% ({wins}W / {losses}L)")
    print(f"Total Net P&L:       ${total_pnl:,.2f}")
    print(f"Avg P&L per Trade:   ${avg_pnl:,.2f}")
    print("="*40, flush=True)

if __name__ == "__main__":
    run_backtest()
