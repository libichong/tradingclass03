from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import duckdb
from flask import Flask, jsonify, render_template, request
from werkzeug.exceptions import HTTPException

from replay import ReplayError
from replay_api import register_replay
from backtest import BacktestError
from backtest_api import register_backtest

BASE_DIR = Path(__file__).resolve().parent
KINDS = {"exe": "逐笔成交", "snp": "行情快照", "ord": "逐笔委托"}
LABELS = {
    "Symbol": "证券代码", "Time": "时间", "Price": "价格",
    "Volume": "数量", "Turnover": "成交额", "AccTurnover": "累计成交额",
    "AccVolume": "累计成交量", "PreClose": "昨收", "Open": "开盘",
    "High": "最高", "Low": "最低", "BSFlag": "买卖标记",
    "AskOrder": "卖方委托号", "BidOrder": "买方委托号",
    "BizIndex": "业务序号", "Channel": "通道", "Index": "序号",
    "FunctionCode": "功能代码", "OrderKind": "委托类型",
    "OrderNumber": "委托编号", "OrderOriNo": "原始委托号",
    "MatchItem": "成交笔数", "AskAvgPrice": "卖方均价",
    "BidAvgPrice": "买方均价", "TotalAskVolume": "卖方总量",
    "TotalBidVolume": "买方总量", "AfterPrice": "盘后价格",
    "AfterVolume": "盘后数量", "AfterTurnover": "盘后成交额",
    "AfterMatchItem": "盘后成交笔数",
}
for side, label in (("Ask", "卖"), ("Bid", "买")):
    for level in range(1, 11):
        LABELS[f"{side}Price{level}"] = f"{label}{level}价"
        LABELS[f"{side}Volume{level}"] = f"{label}{level}量"


class QueryError(ValueError):
    pass


def connect() -> duckdb.DuckDBPyConnection:
    return duckdb.connect(config={"threads": 4, "memory_limit": "512MB"})


def discover(data_dir: Path) -> dict[str, Path]:
    if not data_dir.is_dir():
        raise FileNotFoundError("Data directory does not exist")
    return {
        path.name: path
        for path in sorted(data_dir.iterdir())
        if path.is_file()
        and not path.is_symlink()
        and path.suffix.lower() == ".parquet"
        and path.name[:3].lower() in KINDS
    }


@lru_cache(maxsize=128)
def metadata(path: str, modified_ns: int, size: int) -> dict[str, Any]:
    # File identity includes size and mtime so a replaced file invalidates the cache.
    with connect() as db:
        schema = db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()
        count = db.execute("SELECT count(*) FROM read_parquet(?)", [path]).fetchone()
    if count is None:
        raise RuntimeError("Parquet count returned no result")
    preferred = ["Symbol", "Time", "Price", "Volume"]
    columns = [
        {"name": row[0], "type": row[1], "label": LABELS.get(row[0], row[0])}
        for row in schema
    ]
    columns.sort(key=lambda col: preferred.index(col["name"]) if col["name"] in preferred else 4)
    name = Path(path).name
    match = re.search(r"(\d{8})", name)
    return {
        "name": name, "kind": name[:3].lower(), "size": size,
        "date": match.group(1) if match else None,
        "total": count[0], "columns": columns,
    }


def describe(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return metadata(str(path), stat.st_mtime_ns, stat.st_size)


def integer_arg(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = request.args.get(name, str(default))
    if not re.fullmatch(r"[0-9]+", raw) or len(raw) > 10:
        raise QueryError(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    value = int(raw)
    if not minimum <= value <= maximum:
        raise QueryError(f"{name} 必须是 {minimum} 到 {maximum} 之间的整数")
    return value


def parse_time(value: str) -> int:
    match = re.fullmatch(r"([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,3}))?", value)
    if not match:
        raise QueryError("时间格式应为 HH:MM:SS 或 HH:MM:SS.sss")
    hour, minute, second = (int(part) for part in match.groups()[:3])
    if hour > 23 or minute > 59 or second > 59:
        raise QueryError("时间超出有效范围")
    millis = int((match.group(4) or "0").ljust(3, "0"))
    return hour * 10000000 + minute * 100000 + second * 1000 + millis


def json_value(value: Any) -> Any:
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def create_app(data_dir: Path | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DATA_DIR"] = data_dir if data_dir is not None else BASE_DIR / "Data"
    app.json.ensure_ascii = False
    register_replay(app, discover, connect, parse_time)
    register_backtest(app, discover, connect)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/files")
    def files():
        return jsonify(files=[describe(path) for path in discover(app.config["DATA_DIR"]).values()])

    @app.get("/api/data")
    def data():
        name = request.args.get("file", "")
        paths = discover(app.config["DATA_DIR"])
        if name not in paths:
            raise QueryError("请选择 Data 目录中存在的 exe / snp / ord Parquet 文件")
        path = paths[name]
        info = describe(path)
        columns = [column["name"] for column in info["columns"]]
        page = integer_arg("page", 1, 1, 1000000000)
        page_size = integer_arg("page_size", 100, 1, 500)
        clauses: list[str] = []
        params: list[Any] = [str(path)]
        symbol = request.args.get("symbol", "").strip()
        if symbol:
            if not re.fullmatch(r"[0-9]{6}", symbol):
                raise QueryError("证券代码必须为 6 位数字")
            if "Symbol" not in columns:
                raise QueryError("当前文件不包含 Symbol 字段，无法按代码筛选")
            clauses.append('"Symbol" = ?')
            params.append(int(symbol))
        start = request.args.get("start", "").strip()
        end = request.args.get("end", "").strip()
        start_value = parse_time(start) if start else None
        end_value = parse_time(end) if end else None
        if start_value is not None and end_value is not None and start_value > end_value:
            raise QueryError("开始时间不能晚于结束时间")
        for value, operator in ((start_value, ">="), (end_value, "<=")):
            if value is not None:
                if "Time" not in columns:
                    raise QueryError("当前文件不包含 Time 字段，无法按时间筛选")
                clauses.append(f'"Time" {operator} ?')
                params.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        projection = ", ".join('"' + col.replace('"', '""') + '"' for col in columns)
        with connect() as db:
            if clauses:
                result = db.execute(
                    "SELECT count(*) FROM read_parquet(?)" + where, params
                ).fetchone()
                if result is None:
                    raise RuntimeError("Filtered count returned no result")
                total = result[0]
            else:
                total = info["total"]
            pages = max(1, math.ceil(total / page_size))
            page = min(page, pages)
            # Explicit physical row ordering keeps pagination stable without inventing a market sequence.
            rows = db.execute(
                f"SELECT {projection} FROM read_parquet(?, file_row_number=true)"
                + where + " ORDER BY file_row_number LIMIT ? OFFSET ?",
                params + [page_size, (page - 1) * page_size],
            ).fetchall()
        return jsonify(
            file=name, columns=info["columns"],
            rows=[{key: json_value(value) for key, value in zip(columns, row)} for row in rows],
            total=total, page=page, page_size=page_size, pages=pages,
        )

    @app.errorhandler(QueryError)
    @app.errorhandler(ReplayError)
    @app.errorhandler(BacktestError)
    def invalid_query(error: QueryError):
        return jsonify(error=str(error)), 400

    @app.errorhandler(duckdb.Error)
    @app.errorhandler(OSError)
    def storage_error(error: Exception):
        app.logger.exception("Unable to read market data")
        return jsonify(error="数据文件读取失败，请检查 Data 目录、文件权限和 Parquet 格式。详情见服务端日志。"), 503

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException):
        return jsonify(error=error.description), error.code

    @app.errorhandler(500)
    def server_error(error: HTTPException):
        return jsonify(error="服务器内部错误，请查看服务端日志。"), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)