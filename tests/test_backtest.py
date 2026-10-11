import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

import duckdb

from backtest import (
    FACTORS, MINUTES, MINUTE_INDEX, Bar, BacktestError, Panel, correlation,
    factor_value, load_panel, metrics, minute_returns, net_long_return, ranks,
    run_backtest,
)
from main import create_app


def sample_panel():
    stocks = {}
    for stock in range(1, 5):
        bars = []
        for index in range(len(MINUTES)):
            price = 10 + index * stock * 0.002 + math.sin(index * stock) * 0.02
            bars.append(Bar(
                open=price, close=price, amount=10000, volume=1000 + stock * index,
                buy=stock * 1000, sell=5000 - stock * 500,
                buy_count=10 + stock, sell_count=10,
                large_buy=stock * 500, large_sell=500,
                order_buy=stock * 2000, order_sell=10000 - stock * 1000,
            ))
        stocks[f"{stock:06d}"] = bars
    return Panel(stocks, {})


class FormulaTests(unittest.TestCase):
    def setUp(self):
        self.bars = [Bar(open=10, close=10, amount=100, volume=10, buy=60, sell=30,
                         buy_count=3, sell_count=3, large_buy=40, large_sell=10,
                         order_buy=80, order_sell=20) for _ in MINUTES]
        self.returns = [0.01, -0.02, 0.03, -0.01, 0.02] * 48

    def value(self, key, index=29):
        return factor_value(key, self.bars, self.returns, index, 5)

    def test_all_ten_factor_formulas(self):
        self.assertAlmostEqual(self.value("downside"), 0.0005 / 0.0019)
        self.assertAlmostEqual(self.value("open_intent_ratio"), 0.8)
        self.assertAlmostEqual(self.value("open_intent_strength"), 0.6)
        self.assertAlmostEqual(self.value("open_large_ratio"), 0.3)
        self.assertAlmostEqual(self.value("open_large_strength"), 0.6)
        self.assertAlmostEqual(self.value("reversal"), -0.03 * 0.7)
        self.assertAlmostEqual(self.value("outflow_share"), 1 / 3)
        self.assertAlmostEqual(self.value("large_push"), 0.03 * 0.3)
        tail_index = MINUTE_INDEX[870]
        self.assertAlmostEqual(self.value("tail_share", tail_index), 1 / (tail_index + 1))
        sample = self.returns[25:30]
        average = sum(sample) / 5
        sample_variance = sum((value - average) ** 2 for value in sample) / 4
        expected = 5 / 12 * sum(((value - average) / math.sqrt(sample_variance)) ** 3 for value in sample)
        self.assertAlmostEqual(self.value("skew"), expected)

    def test_open_and_tail_time_gates_and_zero_denominators(self):
        for factor in FACTORS:
            if factor["id"].startswith("open_"):
                self.assertIsNone(self.value(factor["id"], 28))
        self.assertIsNone(self.value("tail_share", MINUTE_INDEX[869]))
        empty = [Bar(open=10, close=10) for _ in MINUTES]
        for key in ("open_intent_ratio", "open_intent_strength", "open_large_ratio", "open_large_strength", "tail_share", "outflow_share"):
            with self.subTest(key=key):
                self.assertIsNone(factor_value(key, empty, self.returns, MINUTE_INDEX[875], 5))
        self.assertIsNone(factor_value("skew", empty, [0.0] * len(MINUTES), 29, 5))
        self.assertIsNone(factor_value("downside", empty, [0.0] * len(MINUTES), 29, 5))

    def test_future_values_cannot_change_past_signals(self):
        panel = sample_panel()
        original = panel.stocks["000001"]
        for cutoff in (20, 29, MINUTE_INDEX[871]):
            changed = copy.deepcopy(original)
            for bar in changed[cutoff + 1:]:
                bar.open = bar.close = 100000
                bar.amount = bar.buy = bar.volume = bar.order_buy = bar.large_buy = 1e12
            for factor in FACTORS:
                with self.subTest(cutoff=cutoff, factor=factor["id"]):
                    self.assertEqual(
                        factor_value(factor["id"], original, minute_returns(original), cutoff, 5),
                        factor_value(factor["id"], changed, minute_returns(changed), cutoff, 5),
                    )

    def test_lunch_return_is_not_one_minute_return(self):
        bars = sample_panel().stocks["000001"]
        values = minute_returns(bars)
        self.assertIsNone(values[MINUTE_INDEX[780]])
        self.assertIsNone(factor_value("skew", bars, values, MINUTE_INDEX[784], 5))
        self.assertIsNotNone(factor_value("skew", bars, values, MINUTE_INDEX[785], 5))

    def test_average_tie_ranks_and_correlation(self):
        self.assertEqual(ranks([10, 30, 10, 20]), [0.5, 3, 0.5, 2])
        self.assertAlmostEqual(correlation([1, 2, 3], [3, 2, 1]), -1)
        self.assertIsNone(correlation([1, 1], [2, 3]))


class PortfolioTests(unittest.TestCase):
    def test_exact_fee_and_drawdown_formulas(self):
        self.assertAlmostEqual(net_long_return(0.05, 0.001), 1.05 * 0.999 / 1.001 - 1)
        values = metrics([0.1, -0.2, 0.05], 5)
        self.assertAlmostEqual(values["total_return"], -0.076)
        self.assertAlmostEqual(values["max_drawdown"], 0.2)
        self.assertAlmostEqual(metrics([-0.1], 5)["max_drawdown"], 0.1)
        self.assertIsNone(metrics([0.1], 5)["sharpe_period"])
        self.assertIsNone(metrics([0, 0], 5)["sharpe_annual_reference"])

    def test_lagged_prices_membership_and_round_trip_costs(self):
        panel = sample_panel()
        free = run_backtest(panel, 5, 5, 2, 0, "high")
        paid = run_backtest(panel, 5, 5, 2, 0.001, "high")
        for original, net in zip(free, paid):
            if original["status"] != "ok":
                continue
            self.assertLess(net["metrics"]["total_return"], original["metrics"]["total_return"])
            self.assertEqual(len(net["periods"]), len(original["periods"]))
            for period in net["periods"]:
                self.assertLess(period["signal_through"], period["entry"] + ":00.000")
                self.assertTrue(period["exit"] <= "11:29" or period["entry"] >= "13:00")
                entry = int(period["entry"][:2]) * 60 + int(period["entry"][3:])
                exit_minute = int(period["exit"][:2]) * 60 + int(period["exit"][3:])
                gross = sum(panel.stocks[symbol][MINUTE_INDEX[exit_minute]].open / panel.stocks[symbol][MINUTE_INDEX[entry]].open - 1 for symbol in period["members"]) / len(period["members"])
                self.assertAlmostEqual(period["gross_return"], gross)
                self.assertAlmostEqual(period["net_return"], net_long_return(gross, 0.001))
            self.assertAlmostEqual(net["metrics"]["total_return"], net["curve"][-1]["equity"] - 1)
            self.assertAlmostEqual(net["metrics"]["max_drawdown"], -min(point["drawdown"] for point in net["curve"]))

    def test_missing_execution_price_raises_not_future_filters(self):
        panel = sample_panel()
        panel.stocks["000001"][MINUTE_INDEX[580]].open = None
        with self.assertRaisesRegex(BacktestError, "未来可用性"):
            run_backtest(panel, 5, 5, 2, 0, "high")

    def test_low_direction_and_equal_group_benchmark(self):
        panel = sample_panel()
        high = run_backtest(panel, 5, 5, 2, 0, "high")[2]
        low = run_backtest(panel, 5, 5, 2, 0, "low")[2]
        self.assertEqual(high["periods"][0]["members"], ["000003", "000004"])
        self.assertEqual(low["periods"][0]["members"], ["000001", "000002"])
        for a, b in zip(high["periods"], low["periods"]):
            self.assertAlmostEqual(a["benchmark_return"], (a["gross_return"] + b["gross_return"]) / 2)
        self.assertEqual(high["periods"][0]["entry"], "10:00")

    def test_insufficient_distinct_values_has_no_fake_metrics(self):
        bars = [Bar(open=10, close=10) for _ in MINUTES]
        panel = Panel({"000001": bars, "000002": copy.deepcopy(bars)}, {})
        results = run_backtest(panel, 5, 5, 2, 0, "high")
        for result in results:
            self.assertEqual(result["status"], "unavailable")
            self.assertIsNone(result["metrics"])
            self.assertEqual(result["curve"], [])

    def test_future_mutation_preserves_earlier_backtest_periods(self):
        panel = sample_panel()
        before = run_backtest(panel, 5, 5, 2, 0, "high")
        changed = copy.deepcopy(panel)
        for bars in changed.stocks.values():
            for index in range(MINUTE_INDEX[630], len(MINUTES)):
                bars[index].open *= 2
                bars[index].close *= 3
                bars[index].buy *= 5
        after = run_backtest(changed, 5, 5, 2, 0, "high")
        for original, mutated in zip(before, after):
            early = lambda result: [period for period in result["periods"] if period["exit"] < "10:30"]
            self.assertEqual(early(original), early(mutated))


class BacktestApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = {kind: self.root / f"{kind}_20260923.parquet" for kind in ("snp", "exe", "ord")}
        with duckdb.connect() as db:
            db.execute("""
                CREATE TABLE quotes AS SELECT s AS Symbol,
                    (m//60)*10000000+(m%60)*100000+1000 AS "Time",
                    10.0 + (m-570)*s*0.001 + sin(m*s)*0.01 AS Price,
                    100*s AS Volume
                FROM range(1,5) symbols(s), range(570,897) minutes(m)
                WHERE m<690 OR m>=780
            """)
            db.execute("COPY quotes TO ? (FORMAT PARQUET)", [str(self.paths["snp"])])
            db.execute("COPY (SELECT *, CASE WHEN Symbol%2=0 THEN 66 ELSE 83 END AS BSFlag FROM quotes) TO ? (FORMAT PARQUET)", [str(self.paths["exe"])])
            db.execute("COPY (SELECT *, 65 AS OrderKind, CASE WHEN Symbol%2=0 THEN 66 ELSE 83 END AS FunctionCode FROM quotes) TO ? (FORMAT PARQUET)", [str(self.paths["ord"])])
        self.app = create_app(self.root)
        self.app.testing = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def test_api_shapes_finite_metrics_and_static_assets(self):
        catalog = self.client.get("/api/backtest/catalog").get_json()
        self.assertEqual(len(catalog["factors"]), 10)
        self.assertTrue(catalog["datasets"][0]["complete"])
        response = self.client.post("/api/backtest/run", json={"date": "20260923", "window": 5, "groups": 2})
        self.assertEqual(response.status_code, 200, response.get_json())
        payload = json.loads(response.get_data(as_text=True), parse_constant=lambda value: self.fail(f"Non-finite JSON: {value}"))
        self.assertEqual(len(payload["results"]), 10)
        self.assertEqual(payload["quality"]["symbols"], 4)
        self.assertTrue(any(result["status"] == "ok" for result in payload["results"]))
        for path in ("/backtest", "/static/backtest.js", "/static/backtest.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_invalid_parameters_and_incomplete_day(self):
        for update in (
            {"date": "..\\20260923"}, {"window": 2}, {"window": True}, {"holding": 0},
            {"groups": 6}, {"fee_bps": -1}, {"fee_bps": "3"}, {"slippage_bps": 101},
            {"large_threshold": 0}, {"direction": "best"},
        ):
            with self.subTest(update=update):
                response = self.client.post("/api/backtest/run", json={"date": "20260923", **update})
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
        self.paths["ord"].unlink()
        self.assertFalse(self.client.get("/api/backtest/catalog").get_json()["datasets"][0]["complete"])
        self.assertEqual(self.client.post("/api/backtest/run", json={"date": "20260923"}).status_code, 400)

    def test_minute_loader_direction_amount_and_threshold(self):
        with duckdb.connect() as db:
            db.execute("""
                COPY (SELECT * FROM (VALUES
                    (2, 93001000, 10.0, 100, 66),
                    (2, 93002000, 10.0, 50, 83),
                    (2, 93003000, 10.0, 25, 0)
                ) t(Symbol, "Time", Price, Volume, BSFlag)) TO ? (FORMAT PARQUET)
            """, [str(self.paths["exe"])])
        panel = load_panel(self.paths, 1000, duckdb.connect)
        bar = panel.stocks["000002"][0]
        self.assertEqual((bar.amount, bar.volume, bar.buy, bar.sell), (1750, 175, 1000, 500))
        self.assertEqual((bar.large_buy, bar.large_sell), (1000, 0))
        self.assertEqual(panel.quality["unknown_direction_rows"], 1)
        self.assertEqual(bar.trade_count, 3)

    def test_analysis_api_times_missing_quotes_and_validation(self):
        catalog = self.client.get("/api/backtest/catalog").get_json()
        self.assertEqual(len(catalog["analysis_minutes"]), 237)
        self.assertNotIn("12:00", catalog["analysis_minutes"])
        response = self.client.post("/api/backtest/analysis", json={"date": "20260923", "minute": "10:00"})
        self.assertEqual(response.status_code, 200, response.get_json())
        analysis = response.get_json()["analysis"]
        self.assertEqual(analysis["signal_through"], "10:00:59.999")
        self.assertEqual(analysis["common_count"], 0)
        self.assertEqual(len(analysis["matrices"]["pearson"]["values"]), 10)
        self.assertIsNone(analysis["features"][0]["values"]["spread"])
        self.assertIn("spread", analysis["features"][0]["missing_features"])
        for minute in ("12:00", "14:57", "25:00", "09:60", "9:30", 896, []):
            with self.subTest(minute=minute):
                response = self.client.post("/api/backtest/analysis", json={"date": "20260923", "minute": minute})
                self.assertEqual(response.status_code, 400)
        response = self.client.get("/static/factor_analysis.js")
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_quote_loader_uses_same_last_snapshot_without_backfill(self):
        from factor_analysis import trading_features
        with duckdb.connect() as db:
            db.execute("""
                COPY (SELECT * FROM (VALUES
                    (2, 93001000, 10.0, 9.99, 10.01, 600.0, 400.0),
                    (2, 93059000, 10.0, 0.0, 10.02, 10.0, 20.0),
                    (2, 93101000, 10.0, 10.0, 10.02, 300.0, 100.0)
                ) t(Symbol, "Time", Price, BidPrice1, AskPrice1, BidVolume1, AskVolume1))
                TO ? (FORMAT PARQUET)
            """, [str(self.paths["snp"])])
        panel = load_panel(self.paths, 1000, duckdb.connect)
        bars = panel.stocks["000002"]
        self.assertEqual(panel.quality["missing_quote_fields"], [])
        self.assertEqual(bars[0].bid_price, 0)
        self.assertIsNone(trading_features(bars, minute_returns(bars), 0, 5)["spread"])
        current = trading_features(bars, minute_returns(bars), 1, 5)
        self.assertAlmostEqual(current["spread"], 0.02)
        self.assertAlmostEqual(current["depth_imbalance"], 0.5)


if __name__ == "__main__":
    unittest.main()
