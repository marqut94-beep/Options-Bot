#!/usr/bin/env python3
"""
Momentum Paper Desk - Lambda clone, "ratio" variant.

This is a near-identical clone of momentum_bot.py, with the ONE deliberate
difference being the methodology this whole build exists to compare: this
bot uses a simple RATIO relVol (today's volume / 30-day average), matching
the Momentum Paper Desk Claude routine's actual live formula, instead of
momentum_bot.py's volume Z-SCORE. Exit brackets also match the Momentum
Paper Desk exactly (10% remainder target, not momentum_bot.py's 12%).

Two structural limitations versus the real Momentum Paper Desk Claude
routine, both unavoidable in a plain Lambda function (no Robinhood MCP
access outside a Claude Code session):
  1. Candidate discovery is self-computed from Alpaca data over this
     repo's universe.json (same ~1,096-symbol S&P-based list momentum_bot.py
     already uses) - NOT Robinhood's live whole-market scan. This bot will
     never catch a non-universe name the real Momentum Paper Desk can (e.g.
     NVAX, confirmed missing from universe.json 2026-10-09).
  2. No Robinhood options-liquidity gate and no earnings-adjacency skip -
     both Robinhood-only checks. Matches momentum_bot.py's own current
     (gate-removed, earnings-filter-removed) live configuration instead.

Everything else - confirmation logic (5-min opening range, bidirectional,
STALE_CONFIRMATION_MINUTES re-pricing exactly as shipped to the Momentum
Paper Desk's Claude routine 2026-10-09), sizing cap/budget, two-stage exit
structure - mirrors the real Momentum Paper Desk as closely as a Lambda
function can get.

Env vars required (set as Lambda function configuration):
  ALPACA_API_KEY_ID
  ALPACA_API_SECRET_KEY
"""

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(os.environ.get("BOT_STATE_DIR", Path(__file__).parent))
STATE_PATH = HERE / "state.json"
UNIVERSE_PATH = HERE / "universe.json"
SIGNALS_PATH = HERE / "signals.json"

ALPACA_KEY = os.environ.get("ALPACA_API_KEY_ID")
ALPACA_SECRET = os.environ.get("ALPACA_API_SECRET_KEY")

ALPACA_DATA = "https://data.alpaca.markets/v2"

DAILY_BUDGET = 25000.0
MAX_TRADES_PER_DAY = 4
MIN_REMAINING_TO_ENTER = 5000.0
STOP_PCT = 0.03
FIRST_TARGET_PCT = 0.04
REMAINDER_FLOOR_PCT = 0.02
REMAINDER_TARGET_PCT = 0.10          # matches Momentum Paper Desk (NOT momentum_bot.py's 0.12)
PARTIAL_EXIT_PCT = 0.10
REL_VOL_MIN = 2.0                    # simple ratio threshold (today's vol / 30-day avg), matches Momentum Paper Desk
PCT_CHANGE_MIN = 3.0
PRICE_MIN, PRICE_MAX = 10.0, 500.0
STALE_CONFIRMATION_MINUTES = 15      # ported 2026-10-09 from the Momentum Paper Desk Claude routine fix

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
    trades = state.get("trades", {})
    sorted_trades = dict(
        sorted(trades.items(), key=lambda kv: kv[1].get("entryTime", ""), reverse=True)
    )
    out_state = dict(state)
    out_state["trades"] = sorted_trades
    STATE_PATH.write_text(json.dumps(out_state, indent=2, default=str))


def load_universe():
    return json.loads(UNIVERSE_PATH.read_text(encoding="utf-8-sig"))


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
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo("America/New_York"))


def today_str(et_dt):
    return et_dt.strftime("%Y-%m-%d")


# ---------- Alpaca fetch helpers ----------

def fetch_daily_bars_batch(symbols, start, end):
    out = {}
    CHUNK = 200
    for i in range(0, len(symbols), CHUNK):
        chunk = symbols[i:i + CHUNK]
        url = f"{ALPACA_DATA}/stocks/bars"
        params = {
            "symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": end,
            "feed": "sip", "adjustment": "split", "limit": 10000,
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


# ---------- sizing (ratio-based - matches Momentum Paper Desk) ----------

def conviction(rel_vol):
    return max(0.0, min(1.0, (rel_vol - 2.0) / 6.0))


def ideal_dollar(rel_vol):
    return 5000.0 + 7500.0 * conviction(rel_vol)


# ---------- Step 1: manage open positions ----------

def manage_open_positions(state, et_dt, signals):
    changed = False
    for trade_id, t in list(state["trades"].items()):
        if t.get("status") != "open":
            continue

        direction = t["direction"]
        shares = t["shares"]
        entry_price = t["entryPrice"]

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
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=stop_price,
                         exitReason="stop", returnPct=-STOP_PCT * 100, dollarPnl=round(pnl, 2))
                append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"],
                              stockEntryPrice=entry_price, reason="stop")
                changed = True
            elif triggered and triggered[0] == "target":
                shares_partial = max(1, round(shares * PARTIAL_EXIT_PCT)) if shares > 1 else 1
                shares_remainder = shares - shares_partial
                if shares_remainder == 0:
                    pnl = shares * (target_price - entry_price) if direction == "long" else shares * (entry_price - target_price)
                    t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=target_price,
                             exitReason="target", returnPct=FIRST_TARGET_PCT * 100, dollarPnl=round(pnl, 2))
                    append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"], reason="target")
                else:
                    partial_pnl = shares_partial * (target_price - entry_price) if direction == "long" else shares_partial * (entry_price - target_price)
                    floor = entry_price * (1 + REMAINDER_FLOOR_PCT) if direction == "long" else entry_price * (1 - REMAINDER_FLOOR_PCT)
                    rtarget = entry_price * (1 + REMAINDER_TARGET_PCT) if direction == "long" else entry_price * (1 - REMAINDER_TARGET_PCT)
                    t.update(partialTaken=True, partialExitPrice=target_price,
                             partialExitTime=triggered[1]["t"], partialDollarPnl=round(partial_pnl, 2),
                             sharesPartial=shares_partial, sharesRemainder=shares_remainder,
                             remainderFloor=floor, remainderTarget=rtarget)
                    append_signal(signals, trade_id, "exit_close_partial", symbol=t["symbol"],
                                  partialExitPct=PARTIAL_EXIT_PCT, reason="target")
                changed = True
            elif (et_dt.hour == 15 and et_dt.minute >= 55) or et_dt.hour >= 16:
                last_price = bars[-1]["c"] if bars else entry_price
                pnl = shares * (last_price - entry_price) if direction == "long" else shares * (entry_price - last_price)
                ret = (pnl / (shares * entry_price)) * 100
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=last_price,
                         exitReason="eod", returnPct=round(ret, 4), dollarPnl=round(pnl, 2))
                append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"], reason="eod")
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
                t.update(status="closed", exitTime=now_utc().isoformat(), exitPrice=resolved_price,
                         exitReason=reason, returnPct=round(blended_ret, 4), dollarPnl=round(blended_pnl, 2))
                append_signal(signals, trade_id, "exit_close_all", symbol=t["symbol"], reason=reason)
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
        # Simple ratio relVol - matches Momentum Paper Desk exactly (NOT the
        # z-score momentum_bot.py switched to 2026-10-02 - that's the one
        # deliberate variable this whole build exists to isolate).
        rel_vol = today_bar["v"] / mean_vol_30
        pct_change = (today_bar["c"] - today_bar["o"]) / today_bar["o"] * 100
        price = today_bar["c"]
        if rel_vol >= REL_VOL_MIN and abs(pct_change) >= PCT_CHANGE_MIN and PRICE_MIN <= price <= PRICE_MAX:
            candidates.append({"symbol": sym, "relVol": rel_vol, "pctChange": pct_change})

    candidates.sort(key=lambda c: c["relVol"], reverse=True)
    print(f"Candidates this run: {[(c['symbol'], round(c['relVol'],2), round(c['pctChange'],2)) for c in candidates]}")

    open_market = et_dt.replace(hour=9, minute=30, second=0, microsecond=0)

    eligible = []
    for cand in candidates:
        sym = cand["symbol"]
        if sym in already_symbols:
            continue

        bars1 = fetch_minute_bars(sym, open_market.astimezone(timezone.utc).isoformat(), now_utc().isoformat())
        if len(bars1) < 6:
            continue
        ref_bars, scan_bars = bars1[:5], bars1[5:]
        ref_high = max(b["h"] for b in ref_bars)
        ref_low = min(b["l"] for b in ref_bars)
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
            continue

        # Staleness re-pricing - ported 2026-10-09 from the Momentum Paper
        # Desk Claude routine fix: a confirming bar found well after the
        # fact (lag between when price actually broke the range and when
        # this run's screening first caught it) gets re-priced off the
        # LATEST bar if the breakout is still live, instead of crediting the
        # trade with a stale historical price a real order could never have
        # captured. Skipped entirely (not stale-priced, not entered) if
        # price has since reverted back inside the opening range.
        confirm_dt = datetime.fromisoformat(confirming["t"].replace("Z", "+00:00"))
        age_minutes = (now_utc() - confirm_dt).total_seconds() / 60.0
        if age_minutes > STALE_CONFIRMATION_MINUTES:
            latest = bars1[-1]
            still_beyond = (latest["c"] > ref_high) if direction == "long" else (latest["c"] < ref_low)
            if not still_beyond:
                print(f"  {sym}: SKIP - stale, reverted inside opening range")
                continue
            confirming = latest
            print(f"  {sym}: original confirmation stale ({age_minutes:.0f}min old) but still beyond range - re-priced to latest bar")

        entry_price = confirming["c"]
        entry_time = confirming["t"]
        print(f"  {sym}: confirmed {direction} at {entry_price} ({entry_time})")

        eligible.append({
            "cand": cand, "sym": sym, "direction": direction,
            "entry_price": entry_price, "entry_time": entry_time,
        })

    changed = False
    entries_made = 0

    def enter(ev):
        nonlocal remaining, entries_made, changed
        cand, sym, direction = ev["cand"], ev["sym"], ev["direction"]
        entry_price, entry_time = ev["entry_price"], ev["entry_time"]

        conv = conviction(cand["relVol"])
        ideal = ideal_dollar(cand["relVol"])
        actual = min(ideal, remaining)
        shares = max(1, round(actual / entry_price))
        stop_price = entry_price * (1 - STOP_PCT) if direction == "long" else entry_price * (1 + STOP_PCT)
        target_price = entry_price * (1 + FIRST_TARGET_PCT) if direction == "long" else entry_price * (1 - FIRST_TARGET_PCT)

        trade_id = f"{sym}_{today}"
        new_trade = {
            "symbol": sym, "date": today, "direction": direction,
            "entryTime": entry_time, "entryPrice": entry_price, "shares": shares,
            "convictionScore": round(conv, 4), "allocDollar": round(actual, 2),
            "stopPrice": stop_price, "targetPrice": target_price,
            "status": "open", "partialTaken": False,
            "relVol": round(cand["relVol"], 4), "pctChangeAtScreen": round(cand["pctChange"], 4),
            "dataSource": "alpaca-sip", "sizingMethod": "ratio-relvol",
        }
        state["trades"][trade_id] = new_trade
        append_signal(signals, trade_id, "entry", symbol=sym, direction=direction,
                      stockEntryPrice=entry_price, allocDollar=round(actual, 2))
        already_symbols.add(sym)
        remaining -= actual
        entries_made += 1
        changed = True

    for ev in eligible:
        if entries_made >= MAX_TRADES_PER_DAY - len(todays_trades):
            break
        if remaining < MIN_REMAINING_TO_ENTER:
            break
        enter(ev)

    return changed


def print_summary(state):
    trades = list(state["trades"].values())
    closed = [t for t in trades if t.get("status") == "closed"]
    open_ = [t for t in trades if t.get("status") == "open"]
    print(f"--- SUMMARY: {len(trades)} trades logged ({len(open_)} open, {len(closed)} closed) ---")
    if not closed:
        return
    wins = [t for t in closed if (t.get("returnPct") or 0) > 0]
    print(f"Win rate: {len(wins)/len(closed)*100:.1f}% ({len(wins)}W / {len(closed)-len(wins)}L)")
    print(f"Total $ P&L: ${sum(t.get('dollarPnl') or 0 for t in closed):+.2f}")


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
