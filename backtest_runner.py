#!/usr/bin/env python3
"""Historical stock strategy replay. GET requests only; never places orders.

Modes compare legacy backdated entries with causal, next-bar entries.
Option coverage is an approximation using archived contracts and historical
OPRA trades, not a reconstruction of historical chains or the quote-mid fallback.
"""
from __future__ import annotations
import argparse, bisect, csv, gzip, hashlib, json, math, os, statistics, time
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
UTC = timezone.utc
DATA = 'https://data.alpaca.markets'
TRADING = 'https://paper-api.alpaca.markets'
SETTINGS = dict(daily_budget=25000, max_trades=4, minimum_allocation=5000,
                stop=.03, first_target=.04, remainder_floor=.02,
                remainder_target=.10, volume_zscore=2, move_pct=3,
                price_min=10, price_max=500, earnings_filter=False)


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))


def local(day, clock):
    return datetime.combine(date.fromisoformat(day), dtime.fromisoformat(clock), ET)


def iso(value):
    return value.astimezone(UTC).isoformat()


class Alpaca:
    """Retry transient failures; cache GET responses without credentials."""
    def __init__(self, cache):
        key, secret = os.getenv('ALPACA_API_KEY_ID'), os.getenv('ALPACA_API_SECRET_KEY')
        if not key or not secret:
            raise RuntimeError('Set ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY through environment secrets.')
        self.headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}
        self.cache = Path(cache)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.requests = 0

    def get(self, path, params=None, trading=False):
        url = (TRADING if trading else DATA) + path + '?' + urlencode(params or {})
        cached = self.cache / (hashlib.sha256(url.encode()).hexdigest() + '.json.gz')
        if cached.exists():
            with gzip.open(cached, 'rt') as f:
                return json.load(f)
        for attempt in range(6):
            try:
                with urlopen(Request(url, headers=self.headers), timeout=60) as r:
                    data = json.load(r)
                self.requests += 1
                with gzip.open(cached, 'wt') as f:
                    json.dump(data, f)
                return data
            except HTTPError as e:
                if e.code not in (429, 500, 502, 503, 504):
                    # Do not print headers, secrets, or URL query strings.
                    detail = ''
                    try:
                        message = str(json.loads(e.read()).get('message', ''))
                        for credential in self.headers.values():
                            message = message.replace(credential, '[redacted]')
                        detail = ': ' + ' '.join(message.split())[:300] if message else ''
                    except (ValueError, OSError):
                        pass
                    raise RuntimeError(f'Alpaca HTTP {e.code} at {path}{detail}; verify plan permissions and request parameters.') from None
                if attempt == 5:
                    raise RuntimeError(f'Alpaca transient HTTP {e.code} exhausted retries at {path}.') from None
                time.sleep(min(30, 2 ** attempt))
            except (URLError, TimeoutError) as e:
                if attempt == 5:
                    raise RuntimeError(f'Alpaca connection failed at {path}.') from None
                time.sleep(min(30, 2 ** attempt))
        raise RuntimeError('Unreachable retry state')

    def pages(self, path, params, field, trading=False):
        result = defaultdict(list)
        params = dict(params)
        seen = set()
        while True:
            data = self.get(path, params, trading)
            values = data.get(field) or {}
            if isinstance(values, dict):
                for symbol, rows in values.items():
                    result[symbol].extend(rows)
            else:
                result['_list'].extend(values)
            token = data.get('next_page_token')
            if not token:
                break
            if token in seen:
                raise RuntimeError('Repeated pagination token; refusing truncated data.')
            seen.add(token)
            params['page_token'] = token
        return result

    def stock_bars(self, symbols, start, end, timeframe):
        result = defaultdict(list)
        for i in range(0, len(symbols), 100):
            data = self.pages('/v2/stocks/bars', dict(
                symbols=','.join(symbols[i:i + 100]), timeframe=timeframe,
                start=iso(start), end=iso(end), feed='sip', adjustment='raw',
                sort='asc', limit=10000), 'bars')
            for symbol, rows in data.items():
                # Deduplicate timestamps before cumulative volume calculation.
                result[symbol].extend(rows)
        for symbol, rows in result.items():
            result[symbol] = sorted({b['t']: b for b in rows}.values(), key=lambda b: b['t'])
        return result

    def coverage(self, symbol, direction, price, day, decision):
        """Historical trade-supported option gate; no fabricated quote fallback."""
        params = dict(underlying_symbols=symbol, type='call' if direction == 'long' else 'put',
                      expiration_date_gte=day, expiration_date_lte=str(date.fromisoformat(day) + timedelta(days=7)),
                      limit=10000)
        contracts = {}
        for status in ('active', 'inactive'):
            rows = self.pages('/v2/options/contracts', dict(params, status=status),
                              'option_contracts', trading=True).get('_list', [])
            for c in rows:
                # Exclude nonstandard deliverables: this replay models standard contracts.
                if float(c.get('size') or 100) == 100:
                    contracts[c['symbol']] = c
        if not contracts:
            return None, 'no_archived_contract'
        contract = min(contracts.values(), key=lambda c: (abs(float(c['strike_price']) - price), c['expiration_date'], c['symbol']))
        payload = self.get('/v1beta1/options/trades', dict(
            symbols=contract['symbol'], start=iso(decision - timedelta(days=7)),
            end=iso(decision), sort='desc', limit=1))
        rows = (payload.get('trades') or {}).get(contract['symbol'], [])
        if not rows:
            return None, 'no_historical_option_trade'
        observation = rows[0]
        if timestamp(observation['t']) > decision or not math.isfinite(float(observation['p'])) or float(observation['p']) <= 0:
            return None, 'invalid_option_observation'
        return dict(optionSymbol=contract['symbol'], optionObservedPrice=observation['p'],
                    optionObservationAt=observation['t'],
                    optionTradeAgeSeconds=(decision - timestamp(observation['t'])).total_seconds()), None


class Series:
    def __init__(self, bars, market_open, market_close):
        self.all = bars
        self.ends = [timestamp(b['t']) + timedelta(minutes=1) for b in bars]
        self.volumes = []
        total = 0
        for b in bars:
            total += b['v']
            self.volumes.append(total)
        self.regular = [b for b in bars if market_open <= timestamp(b['t']) < market_close]
        self.regular_ends = [timestamp(b['t']) + timedelta(minutes=1) for b in self.regular]
        self.high = self.low = None
        # Missing opening minutes must not redefine a five-minute opening range.
        opening = {timestamp(b['t']): b for b in self.regular[:5]}
        expected = [market_open + timedelta(minutes=i) for i in range(5)]
        if all(t in opening for t in expected):
            self.high = max(opening[t]['h'] for t in expected)
            self.low = min(opening[t]['l'] for t in expected)

    def screen(self, decision, mean, stdev):
        i = bisect.bisect_right(self.ends, decision) - 1
        j = bisect.bisect_right(self.regular_ends, decision) - 1
        if i < 0 or j < 0 or self.high is None or stdev <= 0:
            return None
        # Approximate the intraday daily bar with completed minute bars only.
        opening = self.all[0]['o']
        close = self.all[i]['c']
        z = (self.volumes[i] - mean) / stdev
        move = (close / opening - 1) * 100
        if z < 2 or abs(move) < 3 or not 10 <= close <= 500:
            return None
        return z, j


def workflow_times(market_open, market_close):
    """Idealized A/B schedule; real queue/HTTP delays are not reconstructed."""
    t = market_open.replace(second=0, microsecond=0)
    while t <= market_close + timedelta(minutes=3):
        utc = t.astimezone(UTC)
        if 13 <= utc.hour <= 20 and utc.minute % 5 in (1, 3):
            yield t
        t += timedelta(minutes=1)


def breakout(series, upto, mode):
    choices = series.regular[5:upto + 1] if mode == 'legacy' else series.regular[upto:upto + 1]
    for b in choices:
        if b['c'] > series.high:
            return b, 'long'
        if b['c'] < series.low:
            return b, 'short'
    return None


def signed_pnl(direction, entry, exit, shares):
    return (exit - entry) * shares * (1 if direction == 'long' else -1)


def exit_replay(bars, entry_time, entry_price, shares, direction, polls, close, mode, slippage_bps):
    """Poll-driven two-stage stock exits. Legacy intentionally reuses entry bar."""
    sign = 1 if direction == 'long' else -1
    stop = entry_price * (1 - sign * .03)
    target = entry_price * (1 + sign * .04)
    floor = entry_price * (1 + sign * .02)
    final_target = entry_price * (1 + sign * .10)
    legs, partial, remaining, start = [], False, shares, entry_time
    for poll in polls:
        if poll <= entry_time:
            continue
        available = [b for b in bars if timestamp(b['t']) >= start and timestamp(b['t']) + timedelta(minutes=1) <= poll]
        triggered = None
        for b in available:
            adverse = b['l'] <= (floor if partial else stop) if sign == 1 else b['h'] >= (floor if partial else stop)
            favorable = b['h'] >= (final_target if partial else target) if sign == 1 else b['l'] <= (final_target if partial else target)
            if adverse:
                price = floor if partial else stop
                if mode == 'causal':
                    # Stops cannot assume a fill at the trigger through an opening gap.
                    price = min(price, b['o']) if sign == 1 else max(price, b['o'])
                triggered = ('floor' if partial else 'stop', price, b)
                break
            if favorable:
                triggered = ('target', final_target if partial else target, b)
                break
        if triggered:
            reason, price, b = triggered
            quantity = remaining
            if reason == 'target' and not partial and shares > 1:
                quantity = max(1, shares // 2)
                partial, remaining = True, shares - quantity
                # Original code reuses the target bar. Causal replay excludes it.
                start = timestamp(b['t']) + (timedelta(minutes=1) if mode == 'causal' else timedelta())
            fill = price * (1 - sign * slippage_bps / 10000)
            legs.append(dict(quantity=quantity, price=fill, triggerAt=b['t'], decisionAt=iso(poll), reason=reason))
            if quantity == remaining and reason != 'target' or (reason == 'target' and (len(legs) > 1 or shares == 1)):
                return legs
            # A half-target at end of day is flattened on the next available check.
            continue
        if poll >= close - timedelta(minutes=5):
            if not available:
                continue
            price = available[-1]['c'] * (1 - sign * slippage_bps / 10000)
            legs.append(dict(quantity=remaining, price=price, triggerAt=available[-1]['t'], decisionAt=iso(poll), reason='eod'))
            return legs
    return None


def summarize(trades, session_count):
    complete = [t for t in trades if t['stockPnl'] is not None]
    pnls = [t['stockPnl'] for t in complete]
    positive = [p for p in pnls if p > 0]
    negative = [p for p in pnls if p < 0]
    daily = defaultdict(float)
    for t in complete:
        daily[t['date']] += t['stockPnl']
    equity = peak = drawdown = 0
    for day in sorted(daily):
        equity += daily[day]
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return dict(trades=len(trades), completed_stock_results=len(complete),
                incomplete_stock_results=len(trades) - len(complete),
                trading_sessions=session_count, days_with_trades=len({t['date'] for t in trades}),
                stock_net_pnl=round(sum(pnls), 2),
                stock_pnl_per_session=round(sum(pnls) / session_count, 2) if session_count else None,
                stock_win_rate=100 * len(positive) / len(pnls) if pnls else None,
                stock_average_win=statistics.mean(positive) if positive else None,
                stock_average_loss=statistics.mean(negative) if negative else None,
                stock_profit_factor=sum(positive) / -sum(negative) if negative else None,
                stock_profit_factor_note='No losses; ratio undefined' if not negative else None,
                stock_daily_close_drawdown=round(drawdown, 2))


def run(args):
    start_day, end_day = date.fromisoformat(args.start), date.fromisoformat(args.end)
    if end_day < start_day:
        raise ValueError('End date precedes start date.')
    if args.slippage_bps < 0:
        raise ValueError('Slippage cannot be negative.')
    if end_day >= datetime.now(ET).date():
        raise ValueError('Use completed sessions through yesterday; exclude incomplete/current sessions.')
    if args.option_gate == 'historical-trades' and start_day < date(2024, 2, 1):
        raise ValueError('Historical options mode requires dates from February 2024 onward.')
    universe = json.loads(Path(args.universe).read_text(encoding='utf-8-sig'))
    if not isinstance(universe, list) or not all(isinstance(s, str) for s in universe):
        raise ValueError('universe.json must be an array of ticker strings.')
    universe = sorted(set(universe))
    if args.symbols:
        universe = sorted(set(args.symbols.split(',')))
    api = Alpaca(args.cache)
    calendar = api.get('/v2/calendar', dict(start=args.start, end=args.end), trading=True)
    if not calendar:
        raise RuntimeError('No trading sessions in requested date range.')
    warmup = local(str(start_day - timedelta(days=60)), '00:00')
    daily = api.stock_bars(universe, warmup, local(args.end, '23:59'), '1Day')
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    results = {'legacy': [], 'causal': []}
    diagnostics = Counter()
    for session in calendar:
        day = session['date']
        market_open, market_close = local(day, session['open']), local(day, session['close'])
        bars = api.stock_bars(universe, local(day, '04:00'), market_close, '1Min')
        polls = list(workflow_times(market_open, market_close))
        series = {sym: Series(rows, market_open, market_close) for sym, rows in bars.items()}
        baseline = {}
        for sym, rows in daily.items():
            previous = [b['v'] for b in rows if 0 < (date.fromisoformat(day) - timestamp(b['t']).astimezone(ET).date()).days <= 45]
            if len(previous) >= 30:
                window = previous[-30:]
                mean, stdev = statistics.mean(window), statistics.pstdev(window)
                if stdev > 0:
                    baseline[sym] = mean, stdev
        accepted = {mode: set() for mode in results}
        remaining = {mode: 25000.0 for mode in results}
        for decision in polls:
            if decision > min(local(day, '15:30'), market_close - timedelta(minutes=5)):
                break
            candidates = []
            for sym, data in series.items():
                if sym not in baseline:
                    continue
                screen = data.screen(decision, *baseline[sym])
                if screen:
                    candidates.append((sym, *screen))
            candidates.sort(key=lambda x: (-x[1], x[0]))
            for mode in results:
                for sym, zscore, index in candidates:
                    if len(accepted[mode]) >= 4 or remaining[mode] < 5000:
                        break
                    if sym in accepted[mode]:
                        continue
                    data = series[sym]
                    signal = breakout(data, index, mode)
                    if not signal:
                        continue
                    b, direction = signal
                    if mode == 'causal':
                        next_bars = [r for r in data.regular if timestamp(r['t']) >= decision]
                        if not next_bars or timestamp(next_bars[0]['t']) != decision:
                            diagnostics['missing_next_bar'] += 1
                            continue
                        entry_time, entry_price = decision, next_bars[0]['o']
                    else:
                        entry_time, entry_price = timestamp(b['t']), b['c']
                    # Option gate is evaluated at the decision, never using future observations.
                    option = {}
                    if args.option_gate == 'historical-trades':
                        option, reason = api.coverage(sym, direction, b['c'], day, decision)
                        if reason:
                            diagnostics[mode + '_' + reason] += 1
                            continue
                    sign = 1 if direction == 'long' else -1
                    entry_price *= 1 + sign * args.slippage_bps / 10000
                    allocation = min(5000 + 7500 * max(0, min(1, (zscore - 2) / 12)), remaining[mode])
                    shares = max(1, round(allocation / entry_price))
                    legs = exit_replay(data.regular, entry_time, entry_price, shares,
                                       direction, [p for p in polls if p > decision], market_close,
                                       mode, args.slippage_bps)
                    pnl = None if legs is None else sum(signed_pnl(direction, entry_price, leg['price'], leg['quantity']) for leg in legs)
                    result = dict(mode=mode, date=day, symbol=sym, direction=direction,
                                  signalAt=b['t'], decisionAt=iso(decision), entryAt=iso(entry_time),
                                  entryPrice=entry_price, shares=shares, allocatedDollar=allocation,
                                  volumeZscore=zscore, stockPnl=round(pnl, 2) if pnl is not None else None,
                                  exitReason=legs[-1]['reason'] if legs else None,
                                  exitLegs=legs or [], **(option or {}))
                    results[mode].append(result)
                    accepted[mode].add(sym)
                    remaining[mode] -= allocation
        print(f'{day}: legacy {len(accepted["legacy"])} trades; causal {len(accepted["causal"])} trades', flush=True)
        # Save incremental results even if a subsequent data request fails.
        (output / 'checkpoint.json').write_text(json.dumps(results, indent=2, allow_nan=False))
    report = dict(settings=SETTINGS, start=args.start, end=args.end, universe_count=len(universe),
                  universe_sha256=hashlib.sha256(json.dumps(universe).encode()).hexdigest(),
                  option_gate=args.option_gate, stock_slippage_bps_per_side=args.slippage_bps,
                  earnings_filter=False, api_requests=api.requests, diagnostics=dict(diagnostics),
                  summaries={mode: summarize(trades, len(calendar)) for mode, trades in results.items()},
                  limitations=[
                      'Historical replay approximation, not an exact reproduction of GitHub run timestamps.',
                      'Legacy entries use later screening information to select past prices; NOT an investable performance result.',
                      'Causal entries use the last complete bar to signal and the next minute open to fill.',
                      'Current universe creates survivorship/selection bias; historical constituent lists are not supplied.',
                      'Intraday daily bars are reconstructed from minute bars including premarket; historical revisions may differ from live snapshots.',
                      'Raw bars are used for both minute prices and daily volumes. Split-adjusted production lookbacks are not reconstructed; inspect corporate-action dates.',
                      'Archived contract selection uses all pages/statuses, not the original first 100 currently-active contracts; historical chains are not reconstructed.',
                      'Historical-trades gate omits the quote-mid fallback; quote-only entries may be missed. Missing data is NOT proof of non-optionability.',
                      'No option dollar profit is calculated. Stock returns cannot establish option returns.',
                      'Zero-trade sessions are included. Fees, borrow availability, latency, and market impact remain unmodeled.',
                      'Configured slippage applies to stock fills, not option fills. Stops use adverse-first OHLC ordering; within-bar order is unknown.',
                      'Half-day sessions use their calendar close, unlike the live hard-coded 15:55 exit.',
                      'Option observations can be stale; their age is exported for inspection.',
                  ])
    (output / 'summary.json').write_text(json.dumps(report, indent=2, allow_nan=False))
    (output / 'trades.json').write_text(json.dumps(results, indent=2, allow_nan=False))
    columns = ['mode', 'date', 'symbol', 'direction', 'signalAt', 'decisionAt', 'entryAt',
               'entryPrice', 'shares', 'allocatedDollar', 'volumeZscore', 'stockPnl', 'exitReason',
               'optionSymbol', 'optionObservedPrice', 'optionObservationAt', 'optionTradeAgeSeconds']
    with (output / 'trades.csv').open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(t for trades in results.values() for t in trades)
    text = ['# Momentum bot historical replay', '', 'STOCK STRATEGY RESULTS; NOT OPTION PROFITS.', '',
            '| Mode | Trades | Net stock P&L | Win rate | Profit factor |', '|---|---:|---:|---:|---:|']
    for mode, values in report['summaries'].items():
        rate, factor = values['stock_win_rate'], values['stock_profit_factor']
        text.append(f"| {mode} | {values['trades']} | ${values['stock_net_pnl']:,.2f} | {f'{rate:.1f}%' if rate is not None else 'N/A'} | {f'{factor:.2f}' if factor is not None else 'N/A'} |")
    text.extend(['', '## Interpretation', '', 'Legacy results contain backdated entries and are diagnostic only. Causal results remove that timing bias under the stated fill assumptions.', '', '## Limitations', ''] + ['* ' + s for s in report['limitations']])
    (output / 'report.md').write_text('\n'.join(text) + '\n')
    print(json.dumps(report['summaries'], indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--start', required=True)
    p.add_argument('--end', required=True)
    p.add_argument('--universe', default='universe.json')
    p.add_argument('--symbols', help='Optional comma-separated subset; clearly changes the tested universe.')
    p.add_argument('--option-gate', choices=['historical-trades', 'off'], default='historical-trades')
    p.add_argument('--slippage-bps', type=float, default=5)
    p.add_argument('--cache', default='.backtest-cache')
    p.add_argument('--output', default='backtest-results')
    args = p.parse_args()
    try:
        run(args)
    except (RuntimeError, ValueError) as e:
        p.exit(1, str(e) + '\n')

if __name__ == '__main__':
    main()
