from __future__ import annotations

import re
from pathlib import Path
from threading import RLock
from time import monotonic
from typing import Any, Callable
from uuid import uuid4

import duckdb
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import NotFound

from replay import OrderEvent, ReplayError, ReplaySession, price_ticks, raw_time_ms


MAPPING = "FunctionCode: 66=B, 83=S; OrderKind: 65=NEW, 68=CANCEL, 83=PHASE; ID=Channel+OrderOriNo"
SESSION_TTL = 1800


def load_history(
    path: Path, symbol: str, start: int, end: int,
    connect: Callable[[], duckdb.DuckDBPyConnection],
) -> list[OrderEvent]:
    required = {"Symbol", "Time", "FunctionCode", "OrderKind", "Channel", "OrderOriNo", "BizIndex", "Price", "Volume"}
    events = []
    seen = set()
    with connect() as db:
        schema = db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [str(path)]).fetchall()
        missing = required - {row[0] for row in schema}
        if missing:
            raise ReplayError("委托文件缺少字段：" + ", ".join(sorted(missing)))
        result = db.execute(
            """SELECT Time, FunctionCode, OrderKind, Channel, OrderOriNo, BizIndex, Price, Volume
               FROM read_parquet(?, file_row_number=true)
               WHERE Symbol = ? AND Time BETWEEN ? AND ?
               ORDER BY Time, BizIndex, Channel, file_row_number""",
            [str(path), int(symbol), start, end],
        )
        while batch := result.fetchmany(2048):
            for raw_time, function, kind, channel, original, biz, price, volume in batch:
                if any(value is None for value in (raw_time, function, kind, channel, original, biz, price, volume)):
                    raise ReplayError("委托数据含空字段，无法安全适配")
                when = raw_time_ms(int(raw_time))
                if kind == 83:
                    events.append(OrderEvent(when, "PHASE", f"E:{channel}:{biz}", note=f"源阶段码 {function}（仅提示，不执行竞价规则）"))
                    continue
                if function not in (66, 83) or kind not in (65, 68):
                    raise ReplayError(f"不支持的事件编码：FunctionCode={function}, OrderKind={kind}")
                if original <= 0 or volume <= 0 or int(volume) != volume:
                    raise ReplayError(f"委托标识或数量无效：Channel={channel}, OrderOriNo={original}")
                identifier = f"H:{channel}:{original}"
                side = "B" if function == 66 else "S"
                if kind == 65:
                    if identifier in seen:
                        raise ReplayError(f"重复历史新增订单：{identifier}")
                    seen.add(identifier)
                events.append(OrderEvent(
                    when, "NEW" if kind == 65 else "CANCEL", identifier, side,
                    price_ticks(price, historical=True), int(volume),
                ))
    if not events:
        raise ReplayError("所选证券和时间区间没有历史委托，请调整后重试")
    return events


def demo_events(start: int, end: int) -> list[OrderEvent]:
    rows = [
        ("B", 975, 10000), ("B", 976, 20000), ("S", 978, 15000),
        ("S", 977, 8000), ("B", 977, 12000), ("S", 975, 25000),
    ]
    if end - start < len(rows) * 1000:
        raise ReplayError("演示区间至少需要 6 秒")
    return [
        OrderEvent(start + index * 1000, "NEW", f"D:{index}", side, price, volume)
        for index, (side, price, volume) in enumerate(rows, 1)
    ]


def register_replay(
    app: Flask,
    discover: Callable[[Path], dict[str, Path]],
    connect: Callable[[], duckdb.DuckDBPyConnection],
    parse_time: Callable[[str], int],
) -> None:
    sessions: dict[str, ReplaySession] = {}
    registry_lock = RLock()
    app.extensions["replay_sessions"] = sessions

    def body() -> dict[str, Any]:
        data = request.get_json()
        if not isinstance(data, dict):
            raise ReplayError("请求体必须为 JSON 对象")
        return data

    def get_session() -> ReplaySession:
        identifier = request.headers.get("X-Replay-Session", "")
        with registry_lock:
            now = monotonic()
            for key, session in list(sessions.items()):
                if now - session.touched > SESSION_TTL:
                    del sessions[key]
            if identifier not in sessions:
                raise NotFound("回放会话不存在或已过期，请重新加载")
            session = sessions[identifier]
            session.touched = now
            return session

    @app.get("/replay")
    def replay_page():
        return render_template("replay.html")

    @app.post("/api/replay/session")
    def replay_create():
        data = body()
        filename = data.get("file")
        symbol = data.get("symbol")
        if not isinstance(filename, str):
            raise ReplayError("请选择委托文件")
        if not isinstance(symbol, str) or not re.fullmatch(r"[0-9]{6}", symbol):
            raise ReplayError("证券代码必须为 6 位数字")
        start_text, end_text = data.get("start", "09:30:00.000"), data.get("end", "10:00:00.000")
        if not isinstance(start_text, str) or not isinstance(end_text, str):
            raise ReplayError("起止时间必须为字符串")
        start_raw, end_raw = parse_time(start_text), parse_time(end_text)
        start, end = raw_time_ms(start_raw), raw_time_ms(end_raw)
        if start >= end:
            raise ReplayError("结束时间必须晚于开始时间")
        if filename == "__demo__":
            events = demo_events(start, end)
        else:
            if data.get("mapping_confirmed") is not True:
                raise ReplayError("请先确认研究型字段映射和空簿模拟限制")
            paths = discover(app.config["DATA_DIR"])
            if filename not in paths or not filename.lower().startswith("ord"):
                raise ReplayError("请选择 Data 目录内的 ord Parquet 文件")
            events = load_history(paths[filename], symbol, start_raw, end_raw, connect)
        session = ReplaySession(events, start, end, filename, symbol)
        identifier = uuid4().hex
        with registry_lock:
            now = monotonic()
            for key, old in list(sessions.items()):
                if now - old.touched > SESSION_TTL:
                    del sessions[key]
            previous = request.headers.get("X-Replay-Session")
            if previous in sessions:
                del sessions[previous]
            sessions[identifier] = session
        return jsonify(session_id=identifier, mapping=MAPPING, **session.snapshot()), 201

    @app.delete("/api/replay/session")
    def replay_delete():
        session = get_session()
        with session.lock:
            session.running = False
            with registry_lock:
                sessions.pop(request.headers.get("X-Replay-Session"), None)
        return jsonify(closed=True)

    @app.get("/api/replay/state")
    def replay_state():
        session = get_session()
        with session.lock:
            return jsonify(session.snapshot())

    @app.post("/api/replay/tick")
    def replay_tick():
        session = get_session()
        with session.lock:
            session.pump()
            return jsonify(session.snapshot())

    @app.post("/api/replay/control")
    def replay_control():
        data = body()
        session = get_session()
        with session.lock:
            session.control(data.get("action", ""), data.get("speed", 1))
            return jsonify(session.snapshot())

    @app.post("/api/replay/orders")
    def replay_inject():
        data = body()
        session = get_session()
        with session.lock:
            identifier = session.inject(data)
            return jsonify(submitted_id=identifier, **session.snapshot())

    @app.post("/api/replay/cancel")
    def replay_cancel():
        data = body()
        identifier = data.get("id")
        if not isinstance(identifier, str):
            raise ReplayError("请指定模拟委托或计划 ID")
        session = get_session()
        with session.lock:
            session.cancel_simulated(identifier)
            return jsonify(session.snapshot())
