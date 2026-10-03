import unittest
from unittest.mock import patch
import backtest_runner as b


class OptionProfitTests(unittest.TestCase):
    def trade(self, allocation=1000, direction='long'):
        return dict(optionSymbol='TEST260930C00100000', allocatedDollar=allocation,
                    decisionAt=b.iso(b.local('2026-09-30', '10:00')), shares=10,
                    direction=direction, exitLegs=[
                        dict(reason='target', quantity=5, decisionAt=b.iso(b.local('2026-09-30', '11:00'))),
                        dict(reason='floor', quantity=5, decisionAt=b.iso(b.local('2026-09-30', '12:00')))])

    def mark(self, price):
        return dict(price=price, observedAt='2026-09-30T14:00:00Z', ageSeconds=0)

    def test_partial_profit_uses_separate_contract_leg_prices(self):
        with patch.object(b, 'option_mark', side_effect=[self.mark(2), self.mark(3), self.mark(2.5)]):
            result = b.option_profit_estimate(None, self.trade())
        self.assertEqual(result['optionEstimatedContracts'], 5)
        self.assertEqual([x['contracts'] for x in result['optionEstimateLegs']], [2, 3])
        self.assertEqual(result['optionEstimatedPnl'], 350)
        self.assertEqual(result['optionEstimatedReturnPct'], 35)

    def test_one_contract_partial_closes_option(self):
        with patch.object(b, 'option_mark', side_effect=[self.mark(6), self.mark(7)]) as prices:
            result = b.option_profit_estimate(None, self.trade())
        self.assertEqual(prices.call_count, 2)
        self.assertEqual(result['optionEstimatedPnl'], 100)

    def test_put_profit_not_short_stock_sign(self):
        with patch.object(b, 'option_mark', side_effect=[self.mark(6), self.mark(7)]):
            result = b.option_profit_estimate(None, self.trade(direction='short'))
        self.assertEqual(result['optionEstimatedPnl'], 100)

    def test_missing_partial_price_does_not_become_zero(self):
        with patch.object(b, 'option_mark', side_effect=[self.mark(2), None, self.mark(3)]):
            result = b.option_profit_estimate(None, self.trade())
        self.assertIsNone(result['optionEstimatedPnl'])
        self.assertEqual(result['optionEstimateStatus'], 'missing_or_stale_exit')

    def test_missing_entry_excluded_from_total(self):
        with patch.object(b, 'option_mark', return_value=None):
            result = b.option_profit_estimate(None, self.trade())
        summary = b.summarize_options([result])
        self.assertIsNone(summary['estimated_pnl_priced_subset'])
        self.assertEqual(summary['unpriced_trades'], 1)

    def test_bot_one_contract_overspend_flag(self):
        with patch.object(b, 'option_mark', side_effect=[self.mark(20), self.mark(21)]):
            result = b.option_profit_estimate(None, self.trade())
        self.assertTrue(result['optionExceedsAllocation'])

    def test_stale_and_future_trade_marks_rejected(self):
        at = b.local('2026-09-30', '10:00')
        class API:
            time = '2026-09-30T13:54:59Z'
            def get(self, path, params):
                return {'trades': {'TEST': [{'p': 2, 't': self.time}]}}
        api = API()
        self.assertIsNone(b.option_mark(api, 'TEST', at))
        api.time = '2026-09-30T14:00:01Z'
        self.assertIsNone(b.option_mark(api, 'TEST', at))
        api.time = '2026-09-30T13:55:00Z'
        self.assertEqual(b.option_mark(api, 'TEST', at)['ageSeconds'], 300)


if __name__ == '__main__':
    unittest.main()
