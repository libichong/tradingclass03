from __future__ import annotations

import heapq
import math
from collections import OrderedDict, deque
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import RLock
from time import monotonic
from typing import Any, Callable


DAY_MS = 86_400_000
VIEW_LIMIT = 200


class ReplayError(ValueError):
    pass


def price_ticks(value: Any, *, historical: bool = False) -> int:
    try:
        price = Decimal(str(value))
    except InvalidOperation as error:
        raise ReplayError("价格必须为有效数字") from error
    if not price.is_finite() or not 0 < price <= 10000000:
        raise ReplayError("价格必须大于 0 且不超过 10000000")
    cents = price * 100
    rounded = cents.to_integral_value()
    if historical:
        # The source stores exchange prices as FLOAT32, not exact decimals.
        if abs(cents - rounded) > Decimal("0.02"):
            raise ReplayError("历史价格不符合本模型的 0.01 最小价位")
    elif cents != rounded:
        raise ReplayError("模拟价格必须是 0.01 的整数倍")
    return int(rounded)


def positive_int(value: Any, name: str, maximum: int = 1_000_000_000) -> int:
    if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
        raise ReplayError(f"{name}必须为正整数")
    if len(str(value)) > 12 or not 1 <= int(value) <= maximum:
        raise ReplayError(f"{name}必须在 1 到 {maximum} 之间")
    return int(value)


def raw_time_ms(value: int) -> int:
    hour, rest = divmod(value, 10000000)
    minute, rest = divmod(rest, 100000)
    second, millis = divmod(rest, 1000)
    if not (0 <= hour < 24 and 0 <= minute < 60 and 0 <= second < 60):
        raise ReplayError(f"无效历史时间：{value}")
    return ((hour * 60 + minute) * 60 + second) * 1000 + millis


def time_text(ms: int) -> str:
    seconds, millis = divmod(ms, 1000)
    minutes, second = divmod(seconds, 60)
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}.{millis:03d}"


@dataclass(frozen=True, slots=True)
class OrderEvent:
    time: int
    kind: str
    order_id: str
    side: str = ""
    price: int = 0
    volume: int = 0
    source: str = "history"
    parent: str = ""
    note: str = ""


@dataclass(slots=True)
class Order:
    order_id: str
    time: int
    side: str
    price: int
    volume: int
    remaining: int
    source: str
    parent: str = ""
    filled: int = 0
    canceled: int = 0
    version: int = 0

    def public(self) -> dict[str, Any]:
        status = "OPEN"
        if self.remaining:
            if self.filled or self.canceled:
                status = "PARTIAL"
        else:
            status = "CANCELED" if self.canceled else "FILLED"
        return {
            "id": self.order_id, "time": time_text(self.time), "side": self.side,
            "price": self.price / 100, "volume": self.volume, "remaining": self.remaining,
            "filled": self.filled, "canceled": self.canceled, "status": status,
            "source": self.source, "parent": self.parent, "version": self.version,
        }


class MatchingEngine:
    def __init__(self) -> None:
        self.books: dict[str, dict[int, OrderedDict[str, Order]]] = {"B": {}, "S": {}}
        self.orders: dict[str, Order] = {}
        self.recent: dict[str, OrderedDict[str, Order]] = {"B": OrderedDict(), "S": OrderedDict()}
        self.trades: deque[dict[str, Any]] = deque(maxlen=VIEW_LIMIT)
        self.trade_count = 0
        self.trade_volume = 0
        self.active = 0
        self.last_price: int | None = None

    def touch(self, order: Order, version: int) -> None:
        order.version = version
        recent = self.recent[order.side]
        recent[order.order_id] = order
        recent.move_to_end(order.order_id)
        if len(recent) > VIEW_LIMIT:
            recent.popitem(last=False)

    def remove(self, order: Order) -> None:
        levels = self.books[order.side]
        level = levels[order.price]
        del level[order.order_id]
        if not level:
            del levels[order.price]
        self.active -= 1

    def submit(self, event: OrderEvent, version: int) -> list[str]:
        if event.order_id in self.orders:
            raise ReplayError(f"重复新增订单：{event.order_id}")
        if event.side not in ("B", "S") or event.price <= 0 or event.volume <= 0:
            raise ReplayError("新增订单的方向、价格或数量无效")
        order = Order(
            event.order_id, event.time, event.side, event.price, event.volume,
            event.volume, event.source, event.parent,
        )
        self.orders[order.order_id] = order
        changed = [order.order_id]
        opposite = self.books["S" if event.side == "B" else "B"]
        while order.remaining and opposite:
            best = min(opposite) if order.side == "B" else max(opposite)
            if (order.side == "B" and order.price < best) or (order.side == "S" and order.price > best):
                break
            passive = next(iter(opposite[best].values()))
            volume = min(order.remaining, passive.remaining)
            for participant in (order, passive):
                participant.remaining -= volume
                participant.filled += volume
                self.touch(participant, version)
            buy, sell = (order, passive) if order.side == "B" else (passive, order)
            self.trade_count += 1
            self.trade_volume += volume
            self.last_price = best
            self.trades.append({
                "id": self.trade_count, "version": version, "time": time_text(event.time),
                "price": best / 100, "volume": volume, "side": order.side,
                "buy_id": buy.order_id, "sell_id": sell.order_id,
                "buy_time": time_text(buy.time), "sell_time": time_text(sell.time),
                "buy_total": buy.volume, "sell_total": sell.volume,
                "buy_left": buy.remaining, "sell_left": sell.remaining,
                "simulated": buy.source == "sim" or sell.source == "sim",
            })
            changed.append(passive.order_id)
            if passive.remaining == 0:
                self.remove(passive)
        if order.remaining:
            self.books[order.side].setdefault(order.price, OrderedDict())[order.order_id] = order
            self.active += 1
        self.touch(order, version)
        return changed

    def cancel(self, order_id: str, volume: int | None, version: int) -> str:
        order = self.orders.get(order_id)
        if order is None or order.remaining == 0:
            return "撤减目标不存在或已结束：可能来自切片前订单，或已被模拟撮合消耗"
        amount = order.remaining if volume is None else min(volume, order.remaining)
        note = "撤减量超出剩余量，仅撤减当前剩余量" if volume is not None and volume > order.remaining else ""
        order.remaining -= amount
        order.canceled += amount
        self.touch(order, version)
        if order.remaining == 0:
            self.remove(order)
        return note

    def depth(self) -> dict[str, Any]:
        result: dict[str, Any] = {"last_price": None if self.last_price is None else self.last_price / 100}
        for side, name in (("B", "bids"), ("S", "asks")):
            levels = self.books[side]
            result[name] = [
                {
                    "price": price / 100,
                    "volume": sum(order.remaining for order in levels[price].values()),
                    "orders": len(levels[price]),
                    "sim_volume": sum(order.remaining for order in levels[price].values() if order.source == "sim"),
                }
                for price in sorted(levels, reverse=side == "B")[:10]
            ]
        return result


@dataclass(slots=True)
class Strategy:
    id: str
    mode: str
    side: str
    price: int
    total: int
    clip: int
    interval: int
    undispatched: int
    children: list[str]
    stopped: bool = False


class ReplaySession:
    def __init__(
        self, events: list[OrderEvent], start: int, end: int,
        source: str, symbol: str, clock: Callable[[], float] = monotonic,
    ) -> None:
        self.lock = RLock()
        self.clock = clock
        self.touched = monotonic()
        self.source = source
        self.symbol = symbol
        self.start = start
        self.end = end
        self.now = start
        self.engine = MatchingEngine()
        self.queue: list[tuple[int, int, OrderEvent]] = []
        self.serial = 0
        self.version = 0
        self.sim_counter = 0
        self.events: deque[dict[str, Any]] = deque(maxlen=VIEW_LIMIT)
        self.strategies: dict[str, Strategy] = {}
        self.history_total = len(events)
        self.history_processed = 0
        self.warning_count = 0
        self.running = False
        self.speed = 1.0
        self.anchor_real = clock()
        self.anchor_market = start
        self.error = ""
        for event in events:
            self.enqueue(event)

    def enqueue(self, event: OrderEvent) -> None:
        if not self.now <= event.time <= self.end:
            raise ReplayError("事件时间超出当前回放时钟或结束时间")
        self.serial += 1
        heapq.heappush(self.queue, (event.time, self.serial, event))

    def new_id(self, prefix: str = "S") -> str:
        self.sim_counter += 1
        return f"{prefix}:{self.sim_counter}"

    def schedule_child(self, strategy: Strategy, when: int) -> None:
        if strategy.stopped or strategy.undispatched == 0:
            return
        if when > self.end:
            strategy.stopped = True
            self.warning_count += 1
            self.events.append({
                "version": self.version, "time": time_text(self.now), "kind": "WARNING",
                "id": strategy.id, "side": strategy.side, "price": strategy.price / 100,
                "volume": strategy.undispatched, "source": "sim",
                "note": "已到区间末尾，剩余计划量停止提交",
            })
            return
        volume = min(strategy.clip, strategy.undispatched)
        child = self.new_id()
        self.enqueue(OrderEvent(when, "NEW", child, strategy.side, strategy.price, volume, "sim", strategy.id))
        strategy.children.append(child)
        strategy.undispatched -= volume

    def process_one(self) -> None:
        if not self.queue or self.error:
            return
        when, _, event = self.queue[0]
        self.now = when
        version = self.version + 1
        changed: list[str] = []
        note = event.note
        try:
            if event.kind == "NEW":
                changed = self.engine.submit(event, version)
            elif event.kind == "CANCEL":
                note = self.engine.cancel(event.order_id, event.volume, version)
            elif event.kind != "PHASE":
                raise ReplayError(f"不支持的事件类型：{event.kind}")
        except ReplayError as error:
            self.error = str(error)
            self.running = False
            return
        heapq.heappop(self.queue)
        self.version = version
        if event.source == "history":
            self.history_processed += 1
        if note and event.kind == "CANCEL":
            self.warning_count += 1
        self.events.append({
            "version": version, "time": time_text(event.time), "kind": event.kind,
            "id": event.order_id, "side": event.side, "price": event.price / 100,
            "volume": event.volume, "source": event.source, "note": note,
        })
        if event.parent:
            strategy = self.strategies[event.parent]
            if strategy.mode == "split":
                self.schedule_child(strategy, event.time + strategy.interval)
        for order_id in dict.fromkeys(changed):
            order = self.engine.orders[order_id]
            if order.parent:
                strategy = self.strategies[order.parent]
                if strategy.mode == "iceberg" and not order.remaining and strategy.children[-1] == order_id:
                    self.schedule_child(strategy, event.time)

    def advance_to(self, target: int, budget: int = 1000) -> None:
        processed = 0
        while self.queue and self.queue[0][0] <= target and processed < budget and not self.error:
            self.process_one()
            processed += 1
        if not self.queue or self.queue[0][0] > target:
            self.now = max(self.now, min(target, self.end))
        if not self.queue or self.error:
            self.running = False

    def pump(self) -> None:
        if self.running:
            target = self.anchor_market + int((self.clock() - self.anchor_real) * 1000 * self.speed)
            self.advance_to(min(target, self.end))

    def control(self, action: str, speed: Any = 1) -> None:
        if self.error:
            raise ReplayError(f"回放已停止：{self.error}。请重新加载")
        if action not in ("play", "pause", "step"):
            raise ReplayError("不支持的回放操作")
        if action == "play":
            try:
                value = float(speed)
            except (ValueError, TypeError) as error:
                raise ReplayError("回放倍速无效") from error
            if not math.isfinite(value) or value not in (0.5, 1, 5, 10, 50, 100):
                raise ReplayError("倍速仅支持 0.5 / 1 / 5 / 10 / 50 / 100")
            if not self.queue:
                raise ReplayError("当前没有待回放事件")
            self.pump()
            self.speed = value
            self.anchor_market, self.anchor_real = self.now, self.clock()
            self.running = bool(self.queue)
        elif action == "pause":
            self.pump()
            self.running = False
        else:
            if self.running:
                raise ReplayError("请暂停后再单步")
            self.process_one()

    def inject(self, payload: dict[str, Any]) -> str:
        if self.error:
            raise ReplayError("回放存在错误，请重新加载")
        side = payload.get("side")
        if side not in ("B", "S"):
            raise ReplayError("方向必须为 B 或 S")
        price = price_ticks(payload.get("price"))
        volume = positive_int(payload.get("volume"), "委托量")
        mode = payload.get("mode", "single")
        if mode not in ("single", "split", "iceberg"):
            raise ReplayError("不支持的模拟委托模式")
        delay = payload.get("delay_ms", 0)
        if isinstance(delay, bool) or not isinstance(delay, int) or not 0 <= delay < DAY_MS:
            raise ReplayError("延迟必须为非负整数毫秒，且小于一天")
        clip = positive_int(payload.get("clip"), "子单量") if mode != "single" else volume
        if clip > volume:
            raise ReplayError("子单量不能超过总委托量")
        interval = positive_int(payload.get("interval_ms"), "拆单间隔", DAY_MS) if mode == "split" else 0
        self.pump()
        when = self.now + delay
        if when > self.end:
            raise ReplayError("注入时间超过所选区间")
        if mode == "split" and when + ((volume - 1) // clip) * interval > self.end:
            raise ReplayError("拆单计划超出区间结束时间，请缩短间隔或增加子单量")
        if mode == "single":
            identifier = self.new_id()
            self.enqueue(OrderEvent(when, "NEW", identifier, side, price, volume, "sim"))
        else:
            identifier = self.new_id("P")
            strategy = Strategy(identifier, mode, side, price, volume, clip, interval, volume, [])
            self.strategies[identifier] = strategy
            self.schedule_child(strategy, when)
        if not delay:
            self.advance_to(self.now)
        return identifier

    def cancel_simulated(self, identifier: str) -> None:
        self.pump()
        order = self.engine.orders.get(identifier)
        strategy = self.strategies.get(identifier)
        if order and order.parent:
            strategy = self.strategies[order.parent]
        if strategy:
            identifiers = set(strategy.children)
        elif (order and order.source == "sim") or any(event.order_id == identifier and event.source == "sim" for _, _, event in self.queue):
            identifiers = {identifier}
        else:
            raise ReplayError("找不到该模拟订单或计划")
        live = [self.engine.orders[key] for key in identifiers if key in self.engine.orders and self.engine.orders[key].remaining]
        pending = [item for item in self.queue if item[2].order_id in identifiers]
        if not live and not pending and not (strategy and strategy.undispatched and not strategy.stopped):
            raise ReplayError("该模拟委托已结束")
        self.version += 1
        if strategy:
            strategy.stopped = True
        self.queue = [item for item in self.queue if item[2].order_id not in identifiers]
        heapq.heapify(self.queue)
        for active in live:
            self.engine.cancel(active.order_id, None, self.version)
        self.events.append({
            "version": self.version, "time": time_text(self.now), "kind": "CANCEL",
            "id": strategy.id if strategy else identifier, "side": "", "price": 0,
            "volume": 0, "source": "sim", "note": "已停止后续子单，并撤销所有剩余挂单",
        })
        if not self.queue:
            self.running = False

    def snapshot(self) -> dict[str, Any]:
        plans = []
        for strategy in self.strategies.values():
            orders = [self.engine.orders[key] for key in strategy.children if key in self.engine.orders]
            filled = sum(order.filled for order in orders)
            resting = sum(order.remaining for order in orders)
            waiting = strategy.total - filled - resting - sum(order.canceled for order in orders)
            plans.append({
                "id": strategy.id, "mode": strategy.mode, "side": strategy.side,
                "total": strategy.total, "filled": filled, "resting": resting,
                "waiting": 0 if strategy.stopped else waiting,
                "canceled": strategy.total - filled - resting if strategy.stopped else sum(order.canceled for order in orders),
                "status": "STOPPED" if strategy.stopped else "FILLED" if filled == strategy.total else "ACTIVE",
            })
        return {
            "source": self.source, "symbol": self.symbol, "time": time_text(self.now),
            "running": self.running, "speed": self.speed, "version": self.version,
            "finished": not self.queue, "error": self.error,
            "history_total": self.history_total, "history_processed": self.history_processed,
            "pending": len(self.queue), "warnings": self.warning_count,
            "order_count": len(self.engine.orders), "active_orders": self.engine.active,
            "trade_count": self.engine.trade_count, "trade_volume": self.engine.trade_volume,
            "book": self.engine.depth(), "events": list(reversed(self.events)),
            "trades": list(reversed(self.engine.trades)),
            "buys": [order.public() for order in reversed(self.engine.recent["B"].values())],
            "sells": [order.public() for order in reversed(self.engine.recent["S"].values())],
            "plans": plans,
            "scheduled": [
                {"id": event.order_id, "time": time_text(when), "side": event.side,
                 "price": event.price / 100, "volume": event.volume, "parent": event.parent}
                for when, _, event in sorted(item for item in self.queue if item[2].source == "sim")[:VIEW_LIMIT]
            ],
        }
