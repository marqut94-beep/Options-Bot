import unittest
from datetime import timedelta
from unittest.mock import patch
import backtest_runner as b

DAY = '2026-09-30'
OPEN = b.local(DAY, '09:30')
CLOSE = b.local(DAY, '16:00')


def candle(minute, op=100, high=101, low=99, close=100, volume=100):
    return dict(t=b.iso(OPEN + timedelta(minutes=minute)), o=op, h=high, l=low, c=close, v=volume)


class ReplayTests(unittest.TestCase):
    def test_long_short_sign(self):
        self.assertEqual(b.signed_pnl('long', 100, 101, 10), 10)
        self.assertEqual(b.signed_pnl('short', 100, 101, 10), -10)

    def test_no_missing_opening_range(self):
        rows = [candle(i) for i in (0, 1, 3, 4, 5)]
        self.assertIsNone(b.Series(rows, OPEN, CLOSE).high)

    def test_completed_bars_only(self):
        rows = [candle(i, close=104, volume=1000) for i in range(6)]
        rows += [candle(6, close=50, volume=100000)]
        s = b.Series(rows, OPEN, CLOSE)
        self.assertEqual(s.screen(OPEN + timedelta(minutes=6), 100, 10)[1], 5)

    def test_legacy_vs_causal_breakout(self):
        rows = [candle(i) for i in range(5)] + [candle(5, high=104, close=103), candle(6, close=100)]
        series = b.Series(rows, OPEN, CLOSE)
        self.assertEqual(b.breakout(series, 6, 'legacy')[1], 'long')
        self.assertIsNone(b.breakout(series, 6, 'causal'))

    def test_adverse_first(self):
        rows = [candle(6, high=105, low=96, close=100)]
        legs = b.exit_replay(rows, OPEN + timedelta(minutes=6), 100, 10, 'long',
                             [OPEN + timedelta(minutes=8)], CLOSE, 'causal', 0)
        self.assertEqual(legs[0]['reason'], 'stop')
        self.assertEqual(legs[0]['price'], 97)

    def test_stock_stop_gap(self):
        rows = [candle(6, op=94, high=95, low=93, close=94)]
        poll = [OPEN + timedelta(minutes=8)]
        args = (rows, OPEN + timedelta(minutes=6), 100, 10, 'long', poll, CLOSE)
        self.assertEqual(b.exit_replay(*args, 'legacy', 0)[0]['price'], 97)
        self.assertEqual(b.exit_replay(*args, 'causal', 0)[0]['price'], 94)

    def test_partial_floor(self):
        rows = [candle(6, high=105, low=103, close=104), candle(7, op=104, high=105, low=101, close=102)]
        legs = b.exit_replay(rows, OPEN + timedelta(minutes=6), 100, 10, 'long',
                             [OPEN + timedelta(minutes=8), OPEN + timedelta(minutes=11)], CLOSE, 'causal', 0)
        self.assertEqual([leg['quantity'] for leg in legs], [5, 5])
        self.assertEqual([leg['price'] for leg in legs], [104, 102])
        self.assertEqual(sum(b.signed_pnl('long', 100, leg['price'], leg['quantity']) for leg in legs), 30)

    def test_legacy_reuses_partial_bar(self):
        rows = [candle(6, high=105, low=101, close=104), candle(7, high=105, low=103, close=104)]
        polls = [OPEN + timedelta(minutes=8), OPEN + timedelta(minutes=11)]
        args = (rows, OPEN + timedelta(minutes=6), 100, 10, 'long', polls, CLOSE)
        self.assertEqual(b.exit_replay(*args, 'legacy', 0)[-1]['reason'], 'floor')
        self.assertIsNone(b.exit_replay(*args, 'causal', 0))

    def test_one_share_target_closes(self):
        legs = b.exit_replay([candle(6, high=105, low=103)], OPEN + timedelta(minutes=6),
                             100, 1, 'long', [OPEN + timedelta(minutes=8)], CLOSE, 'causal', 0)
        self.assertEqual(len(legs), 1)
        self.assertEqual(legs[0]['quantity'], 1)

    def test_short_partial_target(self):
        rows = [candle(6, op=97, high=97, low=95, close=96), candle(7, op=96, high=96, low=89, close=90)]
        legs = b.exit_replay(rows, OPEN + timedelta(minutes=6), 100, 10, 'short',
                             [OPEN + timedelta(minutes=8), OPEN + timedelta(minutes=11)], CLOSE, 'causal', 0)
        self.assertEqual([leg['price'] for leg in legs], [96, 90])

    def test_slippage_adverse_both_sides(self):
        rows = [candle(6, high=101, low=96)]
        legs = b.exit_replay(rows, OPEN + timedelta(minutes=6), 100, 10, 'long',
                             [OPEN + timedelta(minutes=8)], CLOSE, 'causal', 10)
        self.assertAlmostEqual(legs[0]['price'], 97 * .999)

    def test_zero_trade_sessions_in_mean(self):
        report = b.summarize([dict(date=DAY, stockPnl=100)], 5)
        self.assertEqual(report['stock_pnl_per_session'], 20)
        self.assertIsNone(report['stock_profit_factor'])

    def test_missing_exit_not_zero(self):
        report = b.summarize([dict(date=DAY, stockPnl=None)], 5)
        self.assertEqual(report['incomplete_stock_results'], 1)
        self.assertEqual(report['completed_stock_results'], 0)
        self.assertIsNone(report['stock_win_rate'])

    def test_pagination(self):
        api = object.__new__(b.Alpaca)
        responses = iter([{'bars': {'A': [1]}, 'next_page_token': 'page2'}, {'bars': {'B': [2]}}])
        api.get = lambda *a: next(responses)
        self.assertEqual(dict(api.pages('/fake', {}, 'bars')), {'A': [1], 'B': [2]})

    def test_repeated_pagination_rejected(self):
        api = object.__new__(b.Alpaca)
        api.get = lambda *a: {'bars': {}, 'next_page_token': 'same'}
        with self.assertRaises(RuntimeError):
            api.pages('/fake', {}, 'bars')

    def test_schedule_utc_dst(self):
        times = list(b.workflow_times(OPEN, CLOSE))
        self.assertEqual(times[0].strftime('%H:%M'), '09:31')
        winter = list(b.workflow_times(b.local('2026-12-01', '09:30'), b.local('2026-12-01', '16:00')))
        # The source workflows stop at 20:58 UTC (15:58 ET in winter).
        self.assertLess(winter[-1], b.local('2026-12-01', '16:00'))

if __name__ == '__main__':
    unittest.main()
