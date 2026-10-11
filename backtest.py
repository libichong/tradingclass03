from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from statistics import mean, stdev
from typing import Any, Callable

import duckdb


SESSIONS = ((570, 690), (780, 897))
MINUTES = tuple(minute for start, end in SESSIONS for minute in range(start, end))
MINUTE_INDEX = {minute: index for index, minute in enumerate(MINUTES)}
FACTORS = [
    {"id": "skew", "name": "高频偏度因子", "formula": "窗口内 1 分钟收益率的校正样本偏度：n/((n−1)(n−2)) × Σ((r−均值)/样本标准差)³", "availability": "需 L 个完整分钟收益率；零方差时无定义"},
    {"id": "downside", "name": "下行波动占比因子", "formula": "Σ min(r,0)² / Σ r²，使用最近 L 个已完成交易分钟", "availability": "分母为零时无定义"},
    {"id": "open_intent_ratio", "name": "开盘后买入意愿占比因子", "formula": "开盘 30 分钟新增买委托额 / (新增买委托额 + 新增卖委托额)", "availability": "09:30–10:00 完成后固定；不含撤单"},
    {"id": "open_intent_strength", "name": "开盘后买入意愿强度因子", "formula": "开盘 30 分钟 (新增买委托额 − 新增卖委托额) / 同期总成交额", "availability": "10:00 起可用；委托相对实际成交的需求代理"},
    {"id": "open_large_ratio", "name": "开盘后大单净买入占比因子", "formula": "开盘 30 分钟 (大单主动买成交额 − 大单主动卖成交额) / 同期总成交额", "availability": "10:00 起可用；大单由单笔成交额阈值划分"},
    {"id": "open_large_strength", "name": "开盘后大单净买入强度因子", "formula": "开盘 30 分钟 (大单主动买成交额 − 大单主动卖成交额) / (大单主动买成交额 + 大单主动卖成交额)", "availability": "10:00 起可用；无已知方向大单时无定义"},
    {"id": "reversal", "name": "改进反转因子", "formula": "−Σr × (1 − |主动买成交额 − 主动卖成交额| / 总成交额)，均取最近 L 分钟", "availability": "弱方向资金流下的收益反转代理，不是原研报公式"},
    {"id": "tail_share", "name": "尾盘成交占比因子", "formula": "14:30 至信号时刻累计成交量 / 09:30 至信号时刻累计成交量", "availability": "14:31 起可用；分母绝不使用全日未来成交量"},
    {"id": "outflow_share", "name": "平均单笔流出金额占比因子", "formula": "平均主动卖成交额 / (平均主动买成交额 + 平均主动卖成交额)，取最近 L 分钟", "availability": "买卖两侧均需有成交；按成交笔数而非委托笔数"},
    {"id": "large_push", "name": "大单推动涨幅因子", "formula": "Σ [rₘ × (大单主动买额ₘ − 大单主动卖额ₘ) / 总成交额ₘ]，取最近 L 分钟", "availability": "该分钟没有成交时贡献为 0；这是涨幅与大单流共振代理"},
]


class BacktestError(ValueError):
    pass


@dataclass
class Bar:
    open: float | None = None
    close: float | None = None
    amount: float = 0
    volume: float = 0
    buy: float = 0
    sell: float = 0
    buy_count: int = 0
    sell_count: int = 0
    large_buy: float = 0
    large_sell: float = 0
    order_buy: float = 0
    order_sell: float = 0
    trade_count: int = 0
    bid_price: float | None = None
    ask_price: float | None = None
    bid_volume: float | None = None
    ask_volume: float | None = None


@dataclass
class Panel:
    stocks: dict[str, list[Bar]]
    quality: dict[str, Any]


def clock_text(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


def load_panel(
    paths: dict[str, Path], threshold: float,
    connect: Callable[[], duckdb.DuckDBPyConnection],
) -> Panel:
    stocks: dict[str, list[Bar]] = {}
    quality: dict[str, Any] = {}
    minute_sql = '("Time" // 10000000) * 60 + (("Time" // 100000) % 100)'
    trading = '(("Time" >= 93000000 AND "Time" < 113000000) OR ("Time" >= 130000000 AND "Time" < 145700000))'
    required = {
        "snp": {"Symbol", "Time", "Price"},
        "exe": {"Symbol", "Time", "Price", "Volume", "BSFlag"},
        "ord": {"Symbol", "Time", "Price", "Volume", "FunctionCode", "OrderKind"},
    }
    available_fields: dict[str, set[str]] = {}
    quote_fields = ("BidPrice1", "AskPrice1", "BidVolume1", "AskVolume1")
    with connect() as db:
        for kind, fields in required.items():
            path = str(paths[kind])
            schema = db.execute("DESCRIBE SELECT * FROM read_parquet(?)", [path]).fetchall()
            available_fields[kind] = {row[0] for row in schema}
            missing = fields - available_fields[kind]
            if missing:
                raise BacktestError(f"{kind} 文件缺少字段：{', '.join(sorted(missing))}")
            db.execute(f"CREATE TEMP VIEW {kind}_source AS SELECT *, {minute_sql} AS minute FROM read_parquet('{path.replace(chr(39), chr(39) * 2)}') WHERE {trading}")
        quote_projection = ", ".join(
            f'last(CAST("{field}" AS DOUBLE) ORDER BY "Time", file_row_number)'
            if field in available_fields["snp"] else "NULL::DOUBLE"
            for field in quote_fields
        )
        snapshots = db.execute(f"""
            SELECT Symbol, minute,
                first(CAST(Price AS DOUBLE) ORDER BY "Time", file_row_number),
                last(CAST(Price AS DOUBLE) ORDER BY "Time", file_row_number),
                {quote_projection}
            FROM (SELECT *, (("Time" // 10000000)*60 + (("Time" // 100000)%100)) AS minute
                  FROM read_parquet(?, file_row_number=true))
            WHERE minute IN (SELECT unnest(?)) AND Price>0 AND isfinite(Price)
            GROUP BY Symbol, minute ORDER BY Symbol, minute
        """, [str(paths["snp"]), list(MINUTES)]).fetchall()
        for symbol, minute, first, last, bid, ask, bid_volume, ask_volume in snapshots:
            if symbol is None:
                raise BacktestError("快照含空证券代码")
            key = str(symbol).zfill(6)
            if key not in stocks:
                stocks[key] = [Bar() for _ in MINUTES]
            stocks[key][MINUTE_INDEX[minute]] = Bar(
                open=first, close=last, bid_price=bid, ask_price=ask,
                bid_volume=bid_volume, ask_volume=ask_volume,
            )
        if not stocks:
            raise BacktestError("连续竞价时段没有有效价格快照")
        invalid = db.execute("""
            SELECT count(*) FROM exe_source
            WHERE Price IS NULL OR Volume IS NULL OR Symbol IS NULL
               OR NOT isfinite(Price) OR Price<=0 OR Volume<=0
               OR BSFlag IS NULL OR BSFlag NOT IN (0,66,83)
        """).fetchone()
        if invalid is None or invalid[0]:
            raise BacktestError("连续竞价成交记录含无效价格、数量、代码或未确认方向编码，不能安全回测")
        bad_orders = db.execute("""
            SELECT count(*) FROM ord_source
            WHERE OrderKind IS NULL OR OrderKind NOT IN (65,68,83)
               OR (OrderKind=65 AND (FunctionCode IS NULL OR FunctionCode NOT IN (66,83)
                   OR Price IS NULL OR NOT isfinite(Price) OR Price<=0
                   OR Volume IS NULL OR Volume<=0 OR Symbol IS NULL))
        """).fetchone()
        if bad_orders is None or bad_orders[0]:
            raise BacktestError("委托数据包含未确认编码或无效新增委托")
        flows = db.execute("""
            WITH trades AS (SELECT *, CAST(Price AS DOUBLE)*Volume AS amount FROM exe_source)
            SELECT Symbol, minute, sum(amount), sum(Volume),
                sum(CASE WHEN BSFlag=66 THEN amount ELSE 0 END),
                sum(CASE WHEN BSFlag=83 THEN amount ELSE 0 END),
                count(*) FILTER (WHERE BSFlag=66), count(*) FILTER (WHERE BSFlag=83),
                sum(CASE WHEN BSFlag=66 AND amount>=? THEN amount ELSE 0 END),
                sum(CASE WHEN BSFlag=83 AND amount>=? THEN amount ELSE 0 END),
                count(*)
            FROM trades GROUP BY Symbol, minute
        """, [threshold, threshold]).fetchall()
        excluded_symbols: set[str] = set()
        for symbol, minute, amount, volume, buy, sell, buy_n, sell_n, large_b, large_s, count in flows:
            key = str(symbol).zfill(6)
            if key not in stocks:
                excluded_symbols.add(key)
                continue
            bar = stocks[key][MINUTE_INDEX[minute]]
            bar.amount, bar.volume, bar.buy, bar.sell = amount, volume, buy, sell
            bar.buy_count, bar.sell_count = buy_n, sell_n
            bar.large_buy, bar.large_sell = large_b, large_s
            bar.trade_count = count
        orders = db.execute("""
            SELECT Symbol, minute,
                sum(CASE WHEN FunctionCode=66 THEN CAST(Price AS DOUBLE)*Volume ELSE 0 END),
                sum(CASE WHEN FunctionCode=83 THEN CAST(Price AS DOUBLE)*Volume ELSE 0 END)
            FROM ord_source WHERE OrderKind=65 GROUP BY Symbol, minute
        """).fetchall()
        for symbol, minute, buy, sell in orders:
            key = str(symbol).zfill(6)
            if key in stocks:
                bar = stocks[key][MINUTE_INDEX[minute]]
                bar.order_buy, bar.order_sell = buy, sell
        counts = db.execute("SELECT count(*), count(*) FILTER (WHERE BSFlag=0) FROM exe_source").fetchone()
        excluded_prices = db.execute("SELECT count(*) FROM snp_source WHERE Price IS NULL OR NOT isfinite(Price) OR Price<=0").fetchone()
        quality = {
            "symbols": len(stocks), "minute_slots": len(MINUTES),
            "missing_price_bars": sum(bar.open is None for bars in stocks.values() for bar in bars),
            "excluded_snapshot_rows": excluded_prices[0] if excluded_prices else 0,
            "execution_rows": counts[0] if counts else 0,
            "unknown_direction_rows": counts[1] if counts else 0,
            "execution_symbols_without_snapshots": sorted(excluded_symbols),
            "missing_quote_fields": sorted(set(quote_fields) - available_fields["snp"]),
        }
    return Panel(stocks, quality)


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator > 0 else None


def minute_returns(bars: list[Bar]) -> list[float | None]:
    values: list[float | None] = [None]
    for index in range(1, len(bars)):
        previous, current = bars[index - 1].close, bars[index].close
        # Do not treat the lunch gap as a one-minute return.
        values.append(current / previous - 1 if previous and current and MINUTES[index] == MINUTES[index - 1] + 1 else None)
    return values


def factor_value(key: str, bars: list[Bar], returns: list[float | None], index: int, window: int) -> float | None:
    minute = MINUTES[index]
    if key.startswith("open_"):
        if minute < 599:
            return None
        opening = bars[:30]
        if any(bar.close is None for bar in opening):
            return None
        buy, sell = sum(bar.order_buy for bar in opening), sum(bar.order_sell for bar in opening)
        amount = sum(bar.amount for bar in opening)
        large_b, large_s = sum(bar.large_buy for bar in opening), sum(bar.large_sell for bar in opening)
        return {
            "open_intent_ratio": lambda: ratio(buy, buy + sell),
            "open_intent_strength": lambda: ratio(buy - sell, amount),
            "open_large_ratio": lambda: ratio(large_b - large_s, amount),
            "open_large_strength": lambda: ratio(large_b - large_s, large_b + large_s),
        }[key]()
    if key == "tail_share":
        if minute < 870:
            return None
        return ratio(sum(bar.volume for bar in bars[MINUTE_INDEX[870]:index + 1]), sum(bar.volume for bar in bars[:index + 1]))
    if index < window - 1:
        return None
    recent = bars[index - window + 1:index + 1]
    if key == "outflow_share":
        mean_buy = ratio(sum(bar.buy for bar in recent), sum(bar.buy_count for bar in recent))
        mean_sell = ratio(sum(bar.sell for bar in recent), sum(bar.sell_count for bar in recent))
        return None if mean_buy is None or mean_sell is None else ratio(mean_sell, mean_buy + mean_sell)
    raw_returns = returns[index - window + 1:index + 1]
    if any(value is None for value in raw_returns):
        return None
    r = [value for value in raw_returns if value is not None]
    amount = sum(bar.amount for bar in recent)
    buy, sell = sum(bar.buy for bar in recent), sum(bar.sell for bar in recent)
    if key == "skew":
        sigma = stdev(r)
        if sigma <= 1e-15:
            return None
        center = mean(r)
        return len(r) / ((len(r) - 1) * (len(r) - 2)) * sum(((value - center) / sigma) ** 3 for value in r)
    if key == "downside":
        return ratio(sum(min(value, 0) ** 2 for value in r), sum(value ** 2 for value in r))
    if key == "reversal":
        imbalance = ratio(abs(buy - sell), amount)
        return None if imbalance is None else -sum(r) * (1 - imbalance)
    if key == "large_push":
        return sum(value * (bar.large_buy - bar.large_sell) / bar.amount if bar.amount else 0 for value, bar in zip(r, recent))
    raise BacktestError(f"未知因子：{key}")


def ranks(values: list[float]) -> list[float]:
    result = [0.0] * len(values)
    ordered = sorted(range(len(values)), key=lambda index: values[index])
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and values[ordered[end]] == values[ordered[index]]:
            end += 1
        for position in ordered[index:end]:
            result[position] = (index + end - 1) / 2
        index = end
    return result


def correlation(left: list[float], right: list[float]) -> float | None:
    if len(left) < 2:
        return None
    a, b = mean(left), mean(right)
    covariance = sum((x - a) * (y - b) for x, y in zip(left, right))
    denominator = math.sqrt(sum((x - a) ** 2 for x in left) * sum((y - b) ** 2 for y in right))
    return covariance / denominator if denominator > 1e-20 else None


def metrics(returns: list[float], holding: int) -> dict[str, Any]:
    equity = peak = 1.0
    maximum_drawdown = 0.0
    for value in returns:
        equity *= 1 + value
        peak = max(peak, equity)
        maximum_drawdown = max(maximum_drawdown, 1 - equity / peak)
    sigma = stdev(returns) if len(returns) >= 2 else 0
    sharpe = mean(returns) / sigma if len(returns) >= 2 and sigma > 1e-15 else None
    return {
        "total_return": equity - 1, "max_drawdown": maximum_drawdown,
        "sharpe_period": sharpe,
        "sharpe_annual_reference": None if sharpe is None else sharpe * math.sqrt(252 * len(MINUTES) / holding),
        "sharpe_reason": None if sharpe is not None else "不足两期或收益零方差，夏普无定义",
        "win_rate": sum(value > 0 for value in returns) / len(returns) if returns else None,
        "periods": len(returns),
    }


def net_long_return(gross: float, cost: float) -> float:
    return (1 + gross) * (1 - cost) / (1 + cost) - 1


def run_backtest(panel: Panel, window: int, holding: int, groups: int, cost: float, direction: str) -> list[dict[str, Any]]:
    returns = {symbol: minute_returns(bars) for symbol, bars in panel.stocks.items()}
    intervals = [
        (entry - 1, entry, entry + holding)
        for start, end in SESSIONS
        for entry in range(start + holding, end - holding, holding)
    ]
    results = []
    for factor in FACTORS:
        key = factor["id"]
        period_returns: list[float] = []
        benchmark_returns: list[float] = []
        spread_returns: list[float] = []
        group_returns: list[list[float]] = [[] for _ in range(groups)]
        periods: list[dict[str, Any]] = []
        curve: list[dict[str, Any]] = []
        latest: list[dict[str, Any]] = []
        rank_ics: list[float] = []
        equity = benchmark = spread = peak = 1.0
        group_equities = [1.0] * groups
        skipped = 0
        tied_periods = 0
        for signal, entry, exit_minute in intervals:
            signal_index = MINUTE_INDEX[signal]
            values = []
            for symbol, bars in panel.stocks.items():
                value = factor_value(key, bars, returns[symbol], signal_index, window)
                if value is not None and math.isfinite(value) and bars[signal_index].close:
                    values.append((symbol, value))
            if len(values) < groups or len({value for _, value in values}) < groups:
                skipped += 1
                continue
            values.sort(key=lambda item: (item[1], item[0]))
            tied_periods += len({value for _, value in values}) < len(values)
            allocations: list[list[tuple[str, float, float]]] = [[] for _ in range(groups)]
            forward_returns = []
            # Membership is fixed before looking at any execution/forward prices.
            for rank, (symbol, value) in enumerate(values):
                bars = panel.stocks[symbol]
                entry_price = bars[MINUTE_INDEX[entry]].open
                exit_price = bars[MINUTE_INDEX[exit_minute]].open
                if entry_price is None or exit_price is None:
                    raise BacktestError(f"{symbol} 在 {clock_text(entry)} / {clock_text(exit_minute)} 缺少成交价格代理；拒绝用未来可用性筛选证券或静默填价")
                forward = exit_price / entry_price - 1
                allocations[rank * groups // len(values)].append((symbol, value, forward))
                forward_returns.append(forward)
            gross_groups = [mean(row[2] for row in group) for group in allocations]
            net_groups = [net_long_return(value, cost) for value in gross_groups]
            selected_index = groups - 1 if direction == "high" else 0
            selected_return = net_groups[selected_index]
            benchmark_return = net_long_return(mean(forward_returns), cost)
            long_gross, short_gross = (gross_groups[-1], gross_groups[0]) if direction == "high" else (gross_groups[0], gross_groups[-1])
            spread_return = 0.5 * (long_gross - short_gross) - cost * (2 + 0.5 * (long_gross + short_gross))
            if any(value <= -1 for value in [*net_groups, benchmark_return, spread_return]):
                raise BacktestError("组合净值耗尽，当前简单回测模型不支持继续交易")
            rank_ic = correlation(ranks([value for _, value in values]), ranks(forward_returns))
            if rank_ic is not None:
                rank_ics.append(rank_ic)
            if not curve or curve[-1]["time"] != clock_text(entry):
                curve.append({
                    "time": clock_text(entry), "equity": equity, "benchmark": benchmark,
                    "spread": spread, "drawdown": equity / peak - 1, "groups": group_equities.copy(),
                })
            equity *= 1 + selected_return
            benchmark *= 1 + benchmark_return
            spread *= 1 + spread_return
            peak = max(peak, equity)
            for group_index, value in enumerate(net_groups):
                group_returns[group_index].append(value)
                group_equities[group_index] *= 1 + value
            period_returns.append(selected_return)
            benchmark_returns.append(benchmark_return)
            spread_returns.append(spread_return)
            curve.append({
                "time": clock_text(exit_minute), "equity": equity,
                "benchmark": benchmark, "spread": spread, "drawdown": equity / peak - 1,
                "groups": group_equities.copy(),
            })
            periods.append({
                "signal_through": clock_text(signal) + ":59.999",
                "entry": clock_text(entry), "exit": clock_text(exit_minute),
                "universe": len(values), "gross_return": gross_groups[selected_index],
                "net_return": selected_return, "benchmark_return": benchmark_return,
                "rank_ic": rank_ic, "members": [row[0] for row in allocations[selected_index]],
            })
            latest = [
                {"symbol": symbol, "value": value, "group": group_index + 1, "forward_return": forward}
                for group_index, allocation in enumerate(allocations)
                for symbol, value, forward in allocation
            ]
        results.append({
            **factor, "status": "ok" if periods else "unavailable",
            "reason": None if periods else "可用证券 / 不同因子值不足以分组，或因子尚未生效；不生成虚假零收益",
            "metrics": metrics(period_returns, holding) if periods else None,
            "benchmark_metrics": metrics(benchmark_returns, holding) if periods else None,
            "spread_metrics": metrics(spread_returns, holding) if periods else None,
            "relative_return": equity / benchmark - 1 if periods else None,
            "rank_ic": mean(rank_ics) if rank_ics else None, "rank_ic_periods": len(rank_ics),
            "skipped_periods": skipped, "tied_periods": tied_periods,
            "group_metrics": [{"group": index + 1, **metrics(series, holding)} for index, series in enumerate(group_returns)] if periods else [],
            "curve": curve, "periods": periods, "latest_values": latest,
        })
    return results
