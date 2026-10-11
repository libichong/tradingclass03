from __future__ import annotations

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

import duckdb
from flask import Flask, jsonify, render_template, request

from backtest import FACTORS, MINUTES, BacktestError, clock_text, load_panel, run_backtest
from factor_analysis import analyze_factors


def register_backtest(
    app: Flask, discover: Callable[[Path], dict[str, Path]],
    connect: Callable[[], duckdb.DuckDBPyConnection],
) -> None:
    def datasets() -> list[dict[str, Any]]:
        dates: dict[str, dict[str, str]] = {}
        for name in discover(app.config["DATA_DIR"]):
            match = re.fullmatch(r"(exe|snp|ord)_(\d{8})\.parquet", name, re.IGNORECASE)
            if match:
                dates.setdefault(match[2], {})[match[1].lower()] = name
        return [
            {"date": date, "files": files, "complete": len(files) == 3, "missing": sorted({"exe", "snp", "ord"} - files.keys())}
            for date, files in sorted(dates.items())
        ]

    @lru_cache(maxsize=4)
    def panel_cache(signatures: tuple[tuple[str, str, int, int], ...], threshold: float):
        return load_panel({kind: Path(path) for kind, path, _, _ in signatures}, threshold, connect)

    @app.get("/backtest")
    def backtest_page():
        return render_template("backtest.html")

    @app.get("/api/backtest/catalog")
    def backtest_catalog():
        return jsonify(datasets=datasets(), factors=FACTORS, analysis_minutes=[clock_text(minute) for minute in MINUTES])

    def prepare():
        data = request.get_json()
        if not isinstance(data, dict):
            raise BacktestError("请求体必须为 JSON 对象")
        selected_date = data.get("date")
        selected = next((item for item in datasets() if item["date"] == selected_date and item["complete"]), None)
        if selected is None:
            raise BacktestError("请选择具有完整 exe / snp / ord 文件的交易日")
        def integer(name: str, default: int, lower: int, upper: int) -> int:
            value = data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise BacktestError(f"{name} 必须为 {lower}–{upper} 之间的整数")
            return value
        def numeric(name: str, default: float, lower: float, upper: float) -> float:
            value = data.get(name, default)
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not lower <= value <= upper:
                raise BacktestError(f"{name} 必须在 {lower}–{upper} 之间")
            return float(value)
        window = integer("window", 20, 5, 60)
        holding = integer("holding", 5, 1, 30)
        groups = integer("groups", 5, 2, 5)
        fee = numeric("fee_bps", 3, 0, 100)
        slippage = numeric("slippage_bps", 2, 0, 100)
        threshold = numeric("large_threshold", 200000, 1, 1e10)
        direction = data.get("direction", "high")
        if direction not in ("high", "low"):
            raise BacktestError("方向必须为 high 或 low")
        signatures = []
        paths = discover(app.config["DATA_DIR"])
        for kind, name in sorted(selected["files"].items()):
            path = paths[name]
            stat = path.stat()
            signatures.append((kind, str(path), stat.st_mtime_ns, stat.st_size))
        panel = panel_cache(tuple(signatures), threshold)
        parameters = {"window": window, "holding": holding, "groups": groups, "fee_bps": fee, "slippage_bps": slippage, "large_threshold": threshold, "direction": direction}
        return selected, panel, parameters, data

    @app.post("/api/backtest/analysis")
    def backtest_analysis():
        selected, panel, parameters, data = prepare()
        requested = data.get("minute")
        minute = None
        if requested is not None:
            if not isinstance(requested, str) or not re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", requested):
                raise BacktestError("分析分钟格式必须为 HH:MM")
            hour, minute_part = (int(part) for part in requested.split(":"))
            minute = hour * 60 + minute_part
        return jsonify(
            date=selected["date"], parameters=parameters,
            analysis=analyze_factors(panel, parameters["window"], minute),
        )

    @app.post("/api/backtest/run")
    def backtest_run():
        selected, panel, parameters, _ = prepare()
        return jsonify(
            date=selected["date"], files=selected["files"], parameters=parameters,
            quality=panel.quality,
            results=run_backtest(
                panel, parameters["window"], parameters["holding"], parameters["groups"],
                (parameters["fee_bps"] + parameters["slippage_bps"]) / 10000, parameters["direction"],
            ),
            analysis=analyze_factors(panel, parameters["window"]),
            warnings=[
                "代理公式，不是图片来源研报的严格复现；不包含深度学习模型。",
                "仅一个交易日、固定文件内股票池；无样本外验证、无幸存者偏差修正，不据此挑选最优因子。",
                "A 股 T+1 与融券限制未实现：日内换仓和多空曲线仅供因子研究，不是可执行交易收益。",
                "快照最新价仅作成交价格代理，无成交量约束、盘口冲击、涨跌停可成交性或借券成本。",
                "各期全额开平仓、两端扣费；最大回撤仅按持有期净值端点计算，未覆盖期间内最差路径。",
                "夏普以零无风险利率计算。年化数值只是 252×237/持有分钟 的机械外推，单日样本不具统计可靠性。",
                "BSFlag 66/83/0 按已确认研究口径解释为主动买/卖/未知；金额为 Price×Volume，单位未经供应商核验。",
                "午休及 14:57 后集合竞价排除；滚动收益因子在午后重新积累完整窗口。开盘/尾盘因子可用期不同，不宜直接比较全日累计收益。",
            ],
        )
