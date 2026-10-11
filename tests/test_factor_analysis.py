import copy
import math
import unittest
from statistics import stdev

from backtest import FACTORS, MINUTE_INDEX, minute_returns
from factor_analysis import FEATURES, analyze_factors, matrices, trading_features
from test_backtest import sample_panel


class CorrelationTests(unittest.TestCase):
    def test_pearson_spearman_symmetry_and_constant_column(self):
        rows = [[value, value ** 2, -value, 7, *[value * scale for scale in range(1, 7)]] for value in range(1, 7)]
        result = matrices(rows)
        pearson, spearman = result["pearson"]["values"], result["spearman"]["values"]
        self.assertAlmostEqual(spearman[0][1], 1)
        self.assertLess(pearson[0][1], 1)
        self.assertAlmostEqual(pearson[0][2], -1)
        self.assertIsNone(pearson[3][3])
        self.assertIn("常数", result["pearson"]["reasons"][3][0])
        for method in result.values():
            for left in range(10):
                for right in range(10):
                    self.assertEqual(method["values"][left][right], method["values"][right][left])
                    value = method["values"][left][right]
                    if value is not None:
                        self.assertLessEqual(abs(value), 1)
            pairs = method["top_pairs"]
            self.assertTrue(all(pair["left"] != pair["right"] for pair in pairs))
            self.assertEqual([abs(pair["value"]) for pair in pairs], sorted([abs(pair["value"]) for pair in pairs], reverse=True))

    def test_too_few_samples_are_not_forced_to_perfect_correlations(self):
        for rows in ([], [[1.0] * 10], [[1.0] * 10, [2.0] * 10]):
            result = matrices(rows)
            for method in result.values():
                self.assertTrue(all(value is None for row in method["values"] for value in row))
                self.assertIn("不足 3", method["reasons"][0][0])
                self.assertEqual(method["top_pairs"], [])

    def test_listwise_common_sample_and_per_factor_availability(self):
        panel = sample_panel()
        for bar in panel.stocks["000001"][-20:]:
            bar.buy_count = 0
        result = analyze_factors(panel, 20, 896)
        self.assertEqual(result["common_count"], 3)
        self.assertEqual(result["excluded_count"], 1)
        self.assertNotIn("000001", result["common_symbols"])
        self.assertEqual(result["available_counts"]["skew"], 4)
        self.assertEqual(result["available_counts"]["outflow_share"], 3)
        excluded = result["features"][0]
        self.assertFalse(excluded["in_common_sample"])
        self.assertEqual(excluded["missing_factors"], ["outflow_share"])
        valid_rows = [
            [stock["factors"][factor["id"]] for factor in FACTORS]
            for stock in result["features"] if stock["in_common_sample"]
        ]
        self.assertEqual(result["matrices"], matrices(valid_rows))

    def test_morning_has_features_but_no_ten_factor_common_sample(self):
        result = analyze_factors(sample_panel(), 20, 620)
        self.assertEqual(result["available_counts"]["tail_share"], 0)
        self.assertEqual(result["common_count"], 0)
        self.assertEqual(len(result["features"]), 4)
        self.assertIsNotNone(result["features"][0]["values"]["amount"])

    def test_default_searches_back_to_latest_valid_tail_minute(self):
        panel = sample_panel()
        for bars in panel.stocks.values():
            bars[-1].close = None
        result = analyze_factors(panel, 20)
        self.assertEqual(result["minute"], "14:55")
        self.assertEqual(result["common_count"], 4)
        self.assertTrue(result["automatic"])

    def test_future_data_cannot_change_matrix_or_features(self):
        panel = sample_panel()
        before = analyze_factors(panel, 20, 875)
        changed = copy.deepcopy(panel)
        for bars in changed.stocks.values():
            for bar in bars[MINUTE_INDEX[875] + 1:]:
                bar.close = bar.open = 999999
                bar.amount = bar.volume = bar.order_buy = bar.large_buy = 1e12
                bar.bid_price, bar.ask_price = 1000, 2000
        self.assertEqual(before, analyze_factors(changed, 20, 875))


class TradingFeatureTests(unittest.TestCase):
    def test_window_features_and_quote_formulas(self):
        bars = sample_panel().stocks["000002"]
        index = MINUTE_INDEX[896]
        window = 20
        for bar in bars[-window:]:
            bar.trade_count = 100
        bars[index].bid_price, bars[index].ask_price = 9.99, 10.01
        bars[index].bid_volume, bars[index].ask_volume = 600, 400
        returns = minute_returns(bars)
        result = trading_features(bars, returns, index, window)
        self.assertEqual(len(result), len(FEATURES))
        self.assertAlmostEqual(result["window_return"], math.prod(1 + value for value in returns[-window:]) - 1)
        self.assertAlmostEqual(result["volatility"], stdev(returns[-window:]))
        self.assertEqual(result["amount"], 200000)
        self.assertEqual(result["trade_count"], 2000)
        self.assertEqual(result["average_trade"], 100)
        self.assertAlmostEqual(result["active_buy_share"], 0.2)
        self.assertAlmostEqual(result["flow_imbalance"], -0.2)
        self.assertEqual(result["large_net_amount"], 10000)
        self.assertAlmostEqual(result["large_net_share"], 0.05)
        self.assertAlmostEqual(result["order_imbalance"], -1 / 3)
        self.assertAlmostEqual(result["spread"], 0.02)
        self.assertAlmostEqual(result["spread_bps"], 20)
        self.assertAlmostEqual(result["depth_imbalance"], 0.2)

    def test_missing_and_crossed_quotes_are_not_zero_or_stale_filled(self):
        bars = sample_panel().stocks["000001"]
        bars[-2].bid_price, bars[-2].ask_price = 9.99, 10.01
        for bid, ask in ((None, None), (0, 10), (11, 10), (float("nan"), 10)):
            bars[-1].bid_price, bars[-1].ask_price = bid, ask
            result = trading_features(bars, minute_returns(bars), len(bars) - 1, 20)
            self.assertIsNone(result["spread"])
            self.assertIsNone(result["spread_bps"])
            self.assertIsNone(result["depth_imbalance"])

    def test_lunch_and_short_window_missingness_is_explicit(self):
        bars = sample_panel().stocks["000001"]
        early = trading_features(bars, minute_returns(bars), 2, 20)
        self.assertIsNone(early["amount"])
        self.assertIsNotNone(early["price"])
        lunch = trading_features(bars, minute_returns(bars), MINUTE_INDEX[780], 20)
        self.assertIsNone(lunch["window_return"])
        self.assertIsNone(lunch["volatility"])
        self.assertIsNotNone(lunch["amount"])


if __name__ == "__main__":
    unittest.main()
