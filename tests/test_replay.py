import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import duckdb

from main import create_app
from replay import MatchingEngine, OrderEvent, ReplayError, ReplaySession, price_ticks, raw_time_ms
from replay_api import load_history


def event(identifier, side, price, volume, time=0, source="history", kind="NEW"):
    return OrderEvent(time, kind, identifier, side, price, volume, source)


class MatchingTests(unittest.TestCase):
    def test_price_priority_fifo_and_passive_execution_price(self):
        engine = MatchingEngine()
        for version, order in enumerate((
            event("a", "S", 1010, 100), event("b", "S", 1000, 50),
            event("c", "S", 1000, 70), event("buy", "B", 1020, 150),
        ), 1):
            engine.submit(order, version)
        self.assertEqual([trade["sell_id"] for trade in engine.trades], ["b", "c", "a"])
        self.assertEqual([trade["price"] for trade in engine.trades], [10, 10, 10.1])
        self.assertEqual([trade["volume"] for trade in engine.trades], [50, 70, 30])
        self.assertEqual(engine.orders["a"].remaining, 70)
        self.assertEqual(engine.orders["buy"].public()["status"], "FILLED")
        self.assertEqual(engine.depth()["asks"][0]["volume"], 70)

    def test_sell_aggressor_and_full_book_cleanup(self):
        engine = MatchingEngine()
        engine.submit(event("b", "B", 1050, 100), 1)
        engine.submit(event("s", "S", 1000, 120, source="sim"), 2)
        trade = engine.trades[-1]
        self.assertEqual((trade["side"], trade["price"], trade["buy_left"], trade["sell_left"]), ("S", 10.5, 0, 20))
        self.assertTrue(trade["simulated"])
        self.assertEqual(engine.depth()["bids"], [])
        self.assertEqual(engine.active, 1)
        for order in engine.orders.values():
            self.assertEqual(order.volume, order.remaining + order.filled + order.canceled)

    def test_cancel_reduces_without_losing_fifo_and_reports_divergence(self):
        engine = MatchingEngine()
        engine.submit(event("a", "B", 1000, 100), 1)
        engine.submit(event("b", "B", 1000, 100), 2)
        self.assertEqual(engine.cancel("a", 30, 3), "")
        engine.submit(event("s", "S", 1000, 50), 4)
        self.assertEqual(engine.trades[-1]["buy_id"], "a")
        self.assertTrue(engine.cancel("a", 100, 5))
        self.assertEqual(engine.orders["a"].canceled, 50)
        self.assertEqual(engine.orders["a"].filled, 50)
        self.assertTrue(engine.cancel("missing", 10, 6))
        self.assertEqual(engine.active, 1)

    def test_ten_levels_and_duplicate_rejection(self):
        engine = MatchingEngine()
        for index in range(12):
            engine.submit(event(f"b{index}", "B", 1000 - index, 100), index)
            engine.submit(event(f"s{index}", "S", 1010 + index, 100), index)
        book = engine.depth()
        self.assertEqual((len(book["bids"]), len(book["asks"])), (10, 10))
        self.assertEqual(book["bids"][0]["price"], 10)
        self.assertEqual(book["asks"][-1]["price"], 10.19)
        with self.assertRaises(ReplayError):
            engine.submit(event("b0", "B", 1000, 999), 20)
        self.assertEqual(engine.orders["b0"].volume, 100)

    def test_prices_and_time(self):
        self.assertEqual(price_ticks(19.399999618530273, historical=True), 1940)
        self.assertEqual(price_ticks("9.77"), 977)
        for value in ("NaN", "Infinity", "-1", "0", "9.771", True, None):
            with self.subTest(value=value), self.assertRaises(ReplayError):
                price_ticks(value)
        self.assertEqual(raw_time_ms(100000000) - raw_time_ms(95959999), 1)


class SchedulerTests(unittest.TestCase):
    def session(self, events=None):
        self.now = 0.0
        return ReplaySession(events or [], 0, 10000, "__demo__", "000001", lambda: self.now)

    def test_history_ties_precede_sim_and_injection_uses_market_clock(self):
        session = self.session([event("h", "S", 1000, 100, time=100)])
        identifier = session.inject({"side": "B", "price": "10", "volume": 50, "delay_ms": 100})
        self.assertEqual(session.now, 0)
        self.assertNotIn(identifier, session.engine.orders)
        session.control("step")
        self.assertIn("h", session.engine.orders)
        session.control("step")
        self.assertEqual(session.engine.trades[-1]["buy_id"], identifier)
        self.assertEqual(session.now, 100)

    def test_clock_speed_pause_resume_and_end(self):
        session = self.session([event("a", "B", 1000, 10, 100), event("b", "B", 1000, 10, 200)])
        session.control("play", 5)
        self.now = 0.02
        session.pump()
        self.assertEqual(session.history_processed, 1)
        session.control("pause")
        self.now = 10
        session.pump()
        self.assertEqual(session.now, 100)
        session.control("play", 1)
        self.now += 0.2
        session.pump()
        self.assertEqual(session.history_processed, 2)
        self.assertFalse(session.running)
        self.assertTrue(session.snapshot()["finished"])

    def test_split_schedule_and_cancel_parent(self):
        session = self.session()
        parent = session.inject({"side": "B", "price": "10", "volume": 250, "mode": "split", "clip": 100, "interval_ms": 100})
        self.assertEqual(session.engine.active, 1)
        session.control("step")
        self.assertEqual(session.now, 100)
        self.assertEqual(session.engine.active, 2)
        self.assertEqual(session.snapshot()["plans"][0]["waiting"], 50)
        session.cancel_simulated(parent)
        self.assertEqual(session.engine.active, 0)
        self.assertEqual(len(session.queue), 0)
        self.assertEqual(session.snapshot()["plans"][0]["canceled"], 250)

    def test_split_last_child_uses_remainder(self):
        session = self.session()
        session.inject({"side": "B", "price": "10", "volume": 250, "mode": "split", "clip": 100, "interval_ms": 100})
        session.advance_to(200)
        self.assertEqual([order.volume for order in session.engine.orders.values()], [100, 100, 50])
        self.assertEqual(session.snapshot()["plans"][0]["resting"], 250)

    def test_iceberg_hides_reserve_and_replenishes_with_new_priority(self):
        session = self.session()
        parent = session.inject({"side": "S", "price": "10", "volume": 250, "mode": "iceberg", "clip": 100})
        self.assertEqual(session.engine.depth()["asks"][0]["volume"], 100)
        second = session.inject({"side": "S", "price": "10", "volume": 20})
        session.inject({"side": "B", "price": "10", "volume": 100})
        self.assertEqual(session.engine.depth()["asks"][0]["volume"], 120)
        session.inject({"side": "B", "price": "10", "volume": 20})
        self.assertEqual(session.engine.trades[-1]["sell_id"], second)
        self.assertEqual(session.snapshot()["plans"][0]["waiting"], 50)
        session.cancel_simulated(parent)
        self.assertEqual(session.snapshot()["plans"][0]["filled"], 100)
        self.assertEqual(session.snapshot()["plans"][0]["canceled"], 150)
        self.assertEqual(session.engine.depth()["asks"], [])

    def test_iceberg_can_fully_fill_and_conserves_quantity(self):
        session = self.session()
        session.inject({"side": "B", "price": "10", "volume": 300})
        session.inject({"side": "S", "price": "10", "volume": 250, "mode": "iceberg", "clip": 100})
        self.assertEqual([trade["volume"] for trade in session.engine.trades], [100, 100, 50])
        self.assertEqual(session.snapshot()["plans"][0]["status"], "FILLED")
        self.assertEqual(session.engine.depth()["bids"][0]["volume"], 50)

    def test_multiple_icebergs_replenish_in_execution_order(self):
        session = self.session()
        first = session.inject({"side": "S", "price": "10", "volume": 200, "mode": "iceberg", "clip": 100})
        second = session.inject({"side": "S", "price": "10", "volume": 200, "mode": "iceberg", "clip": 100})
        session.inject({"side": "B", "price": "10", "volume": 200})
        displayed = list(session.engine.books["S"][1000].values())
        self.assertEqual([order.parent for order in displayed], [first, second])
        session.inject({"side": "B", "price": "10", "volume": 100})
        seller = session.engine.orders[session.engine.trades[-1]["sell_id"]]
        self.assertEqual(seller.parent, first)

    def test_invalid_input_has_no_order_effect(self):
        session = self.session()
        base = {"side": "B", "price": "10", "volume": 100}
        for update in (
            {"side": "X"}, {"volume": 0}, {"volume": True}, {"volume": 1.5},
            {"price": "NaN"}, {"delay_ms": -1}, {"delay_ms": 20000}, {"mode": "market"},
            {"mode": "split", "clip": 101, "interval_ms": 1},
            {"mode": "split", "clip": 10, "interval_ms": 2000},
        ):
            with self.subTest(update=update), self.assertRaises(ReplayError):
                session.inject({**base, **update})
        self.assertEqual(len(session.queue), 0)
        self.assertEqual(len(session.engine.orders), 0)

    def test_backpressure_never_reorders_or_skips(self):
        session = self.session([event(str(i), "B", 1000, 1, i) for i in range(1500)])
        session.advance_to(2000, budget=100)
        self.assertEqual(session.now, 99)
        self.assertEqual(session.history_processed, 100)
        session.advance_to(2000, budget=2000)
        self.assertEqual(session.history_processed, 1500)
        self.assertEqual(session.engine.depth()["bids"][0]["volume"], 1500)
        self.assertEqual(len(session.snapshot()["buys"]), 200)

    def test_unknown_cancel_warns_and_bad_event_halts(self):
        session = self.session([event("absent", "B", 1000, 10, kind="CANCEL")])
        session.control("step")
        self.assertEqual(session.warning_count, 1)
        self.assertIn("不存在", session.events[-1]["note"])
        broken = self.session([event("x", "B", 1000, 10), event("x", "B", 1000, 10)])
        broken.advance_to(0)
        self.assertIn("重复", broken.error)
        self.assertEqual(len(broken.queue), 1)
        self.assertEqual(broken.history_processed, 1)


class ReplayApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "ord_test.parquet"
        with duckdb.connect() as db:
            db.execute("""
                CREATE TABLE events AS SELECT * FROM (VALUES
                (601688, 93000000, 73, 83, 1002, 0, 1, 0.0, 0),
                (601688, 93000100, 66, 65, 1002, 11, 2, 10.0, 100),
                (601688, 93000200, 83, 65, 1002, 12, 3, 10.0, 40),
                (601688, 93000300, 66, 68, 1002, 11, 4, 10.0, 30),
                (601688, 93000400, 66, 68, 1002, 99, 5, 10.0, 10)
                ) t(Symbol, Time, FunctionCode, OrderKind, Channel, OrderOriNo, BizIndex, Price, Volume)
            """)
            db.execute("COPY events TO ? (FORMAT PARQUET)", [str(self.path)])
        self.app = create_app(self.root)
        self.app.testing = True
        self.client = self.app.test_client()
        self.payload = {"file": self.path.name, "symbol": "601688", "start": "09:30:00", "end": "10:00:00", "mapping_confirmed": True}
        self.headers = {}

    def tearDown(self):
        self.temp.cleanup()

    def create(self, **updates):
        response = self.client.post("/api/replay/session", json={**self.payload, **updates}, headers=self.headers)
        if response.status_code == 201:
            self.headers = {"X-Replay-Session": response.get_json()["session_id"]}
        return response

    def post(self, path, payload=None):
        return self.client.post(f"/api/replay/{path}", json=payload or {}, headers=self.headers)

    def test_real_adapter_and_lifecycle(self):
        self.assertEqual(self.create().status_code, 201)
        for _ in range(5):
            response = self.post("control", {"action": "step"})
            self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertEqual((data["history_processed"], data["trade_count"], data["warnings"]), (5, 1, 1))
        order = next(order for order in data["buys"] if order["id"] == "H:1002:11")
        self.assertEqual((order["filled"], order["canceled"], order["remaining"]), (40, 30, 30))
        self.assertEqual(data["book"]["bids"][0]["volume"], 30)

    def test_sessions_reset_and_isolation(self):
        self.create()
        first = self.headers.copy()
        self.post("orders", {"side": "B", "price": "9", "volume": 100})
        other = self.client.post("/api/replay/session", json=self.payload).get_json()["session_id"]
        response = self.client.get("/api/replay/state", headers={"X-Replay-Session": other})
        self.assertEqual(response.get_json()["order_count"], 0)
        self.create()
        self.assertEqual(self.client.get("/api/replay/state", headers=first).status_code, 404)
        self.assertEqual(self.client.delete("/api/replay/session", headers=self.headers).status_code, 200)
        self.assertEqual(self.client.get("/api/replay/state", headers=self.headers).status_code, 404)

    def test_concurrent_injections_have_unique_ids(self):
        self.create()
        token = self.headers.copy()
        def inject(_):
            with self.app.test_client() as client:
                return client.post("/api/replay/orders", headers=token, json={"side": "B", "price": "9", "volume": 1}).get_json()["submitted_id"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            identifiers = list(pool.map(inject, range(20)))
        self.assertEqual(len(set(identifiers)), 20)
        data = self.client.get("/api/replay/state", headers=token).get_json()
        self.assertEqual(data["book"]["bids"][0]["volume"], 20)

    def test_api_validation_and_static_routes(self):
        self.assertEqual(self.create(mapping_confirmed=False).status_code, 400)
        self.assertEqual(self.create(file="..\\ord_test.parquet").status_code, 400)
        self.assertEqual(self.create(symbol="999999").status_code, 400)
        self.create()
        for path, payload in (("orders", []), ("orders", {"side": "B", "price": "NaN", "volume": 1}), ("control", {"action": "play", "speed": 3}), ("cancel", {"id": "H:1002:11"})):
            with self.subTest(path=path, payload=payload):
                response = self.client.post(f"/api/replay/{path}", json=payload, headers=self.headers)
                self.assertEqual(response.status_code, 400)
                self.assertIn("error", response.get_json())
        for path in ("/replay", "/static/replay.js", "/static/replay.css"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            response.close()

    def test_unsupported_code_is_rejected_not_skipped(self):
        with duckdb.connect() as db:
            db.execute('COPY (SELECT 601688 AS Symbol, 93000000 AS "Time", 99 AS FunctionCode, 65 AS OrderKind, 1 AS Channel, 1 AS OrderOriNo, 1 AS BizIndex, 10 AS Price, 1 AS Volume) TO ? (FORMAT PARQUET)', [str(self.path)])
        with self.assertRaisesRegex(ReplayError, "不支持"):
            load_history(self.path, "601688", 93000000, 100000000, duckdb.connect)


if __name__ == "__main__":
    unittest.main()
