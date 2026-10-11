from __future__ import annotations

import math
from statistics import stdev
from typing import Any

from backtest import (
    FACTORS, MINUTES, MINUTE_INDEX, Bar, BacktestError, Panel, clock_text,
    correlation, factor_value, minute_returns, ranks, ratio,
)


FEATURES = [
    {"id": "price", "name": "最新价", "format": "price", "definition": "信号分钟最后一条有效快照最新价，不使用之后的价格。"},
    {"id": "window_return", "name": "窗口收益", "format": "percent", "definition": "最近 L 个完整分钟收益的复利收益；需要 L+1 个连续收盘价，不跨午休计算。"},
    {"id": "volatility", "name": "分钟波动率", "format": "percent", "definition": "最近 L 个分钟收益的样本标准差，未年化。"},
    {"id": "volume", "name": "成交量", "format": "number", "definition": "最近 L 个交易分钟的逐笔成交 Volume 之和，保留源数据单位。"},
    {"id": "amount", "name": "成交额", "format": "number", "definition": "最近 L 个交易分钟 Σ(Price×Volume)，保留源金额单位。"},
    {"id": "trade_count", "name": "成交笔数", "format": "number", "definition": "最近 L 个交易分钟的逐笔成交记录数，包括方向未知成交。"},
    {"id": "average_trade", "name": "平均单笔金额", "format": "number", "definition": "窗口总成交额 / 总成交笔数，零笔数时无定义。"},
    {"id": "active_buy_share", "name": "主动买额占比", "format": "percent", "definition": "窗口 BSFlag=66 的成交额 / 总成交额；未知方向仍计入分母。"},
    {"id": "flow_imbalance", "name": "主动买卖失衡", "format": "percent", "definition": "(主动买成交额−主动卖成交额) / 总成交额；正值为主动买入占优。"},
    {"id": "large_net_amount", "name": "大单净买额", "format": "number", "definition": "窗口内达到设定单笔金额阈值的主动买额−主动卖额，不代表资金账户实际净流入。"},
    {"id": "large_net_share", "name": "大单净买占比", "format": "percent", "definition": "窗口大单净买额 / 总成交额。"},
    {"id": "order_imbalance", "name": "新增委托失衡", "format": "percent", "definition": "(新增买委托额−新增卖委托额) / (新增买委托额+新增卖委托额)，只取 OrderKind=65，不扣撤单。"},
    {"id": "spread", "name": "买卖价差", "format": "price", "definition": "信号分钟最后一条有效价格快照的卖一价−买一价；缺失、零报价或交叉盘口时无定义，不回填更早报价。"},
    {"id": "spread_bps", "name": "相对价差 bps", "format": "bps", "definition": "(卖一价−买一价) / 买卖中间价 ×10000；不等于实际交易滑点。"},
    {"id": "depth_imbalance", "name": "一档深度失衡", "format": "percent", "definition": "与价差同一快照的 (买一量−卖一量)/(买一量+卖一量)；要求报价有效且数量非负，总量为零时无定义。"},
]


def trading_features(bars: list[Bar], returns: list[float | None], index: int, window: int) -> dict[str, float | int | None]:
    current = bars[index]
    result: dict[str, float | int | None] = {feature["id"]: None for feature in FEATURES}
    result["price"] = current.close
    bid, ask = current.bid_price, current.ask_price
    if bid is not None and ask is not None and math.isfinite(bid) and math.isfinite(ask) and 0 < bid <= ask:
        result["spread"] = ask - bid
        result["spread_bps"] = (ask - bid) / ((ask + bid) / 2) * 10000
        bid_volume, ask_volume = current.bid_volume, current.ask_volume
        if bid_volume is not None and ask_volume is not None and math.isfinite(bid_volume) and math.isfinite(ask_volume) and min(bid_volume, ask_volume) >= 0:
            result["depth_imbalance"] = ratio(bid_volume - ask_volume, bid_volume + ask_volume)
    if index < window - 1:
        return result
    recent = bars[index - window + 1:index + 1]
    amount, volume = sum(bar.amount for bar in recent), sum(bar.volume for bar in recent)
    buy, sell = sum(bar.buy for bar in recent), sum(bar.sell for bar in recent)
    count = sum(bar.trade_count for bar in recent)
    large_net = sum(bar.large_buy - bar.large_sell for bar in recent)
    order_buy, order_sell = sum(bar.order_buy for bar in recent), sum(bar.order_sell for bar in recent)
    result.update(
        amount=amount, volume=volume, trade_count=count, average_trade=ratio(amount, count),
        active_buy_share=ratio(buy, amount), flow_imbalance=ratio(buy - sell, amount),
        large_net_amount=large_net, large_net_share=ratio(large_net, amount),
        order_imbalance=ratio(order_buy - order_sell, order_buy + order_sell),
    )
    recent_returns = returns[index - window + 1:index + 1]
    if all(value is not None and math.isfinite(value) for value in recent_returns):
        values = [value for value in recent_returns if value is not None]
        result["window_return"] = math.prod(1 + value for value in values) - 1
        result["volatility"] = stdev(values)
    return result


def matrices(rows: list[list[float]]) -> dict[str, Any]:
    size = len(FACTORS)
    columns = [[row[index] for row in rows] for index in range(size)]
    result: dict[str, Any] = {}
    for method in ("pearson", "spearman"):
        data = [ranks(column) for column in columns] if method == "spearman" else columns
        grid: list[list[float | None]] = [[None] * size for _ in range(size)]
        reasons: list[list[str | None]] = [[None] * size for _ in range(size)]
        for left in range(size):
            for right in range(left, size):
                value = correlation(data[left], data[right]) if len(rows) >= 3 else None
                reason = None
                if len(rows) < 3:
                    reason = "完整共同样本不足 3 只"
                elif value is None:
                    reason = "至少一个因子在共同样本内为常数或数值方差过小"
                else:
                    value = max(-1.0, min(1.0, value))
                grid[left][right] = grid[right][left] = value
                reasons[left][right] = reasons[right][left] = reason
        pairs = [
            {"left": FACTORS[left]["id"], "right": FACTORS[right]["id"], "value": grid[left][right]}
            for left in range(size) for right in range(left + 1, size)
            if grid[left][right] is not None
        ]
        pairs.sort(key=lambda pair: abs(pair["value"]), reverse=True)
        result[method] = {"values": grid, "reasons": reasons, "top_pairs": pairs[:5]}
    return result


def analyze_factors(panel: Panel, window: int, minute: int | None = None) -> dict[str, Any]:
    if minute is not None and minute not in MINUTE_INDEX:
        raise BacktestError("分析时点必须是 09:30–11:29 或 13:00–14:56 的完整分钟")
    returns = {symbol: minute_returns(bars) for symbol, bars in panel.stocks.items()}

    def snapshot(at: int) -> dict[str, dict[str, float | None]]:
        index = MINUTE_INDEX[at]
        return {
            symbol: {factor["id"]: factor_value(factor["id"], bars, returns[symbol], index, window) for factor in FACTORS}
            for symbol, bars in sorted(panel.stocks.items())
        }

    def complete(values: dict[str, float | None]) -> bool:
        return all(value is not None and math.isfinite(value) for value in values.values())

    automatic = minute is None
    selected_minute = MINUTES[-1] if minute is None else minute
    values = snapshot(selected_minute)
    if automatic:
        # All ten factors include tail_share, which cannot exist before 14:30.
        for candidate in reversed([item for item in MINUTES if item >= 870]):
            candidate_values = values if candidate == selected_minute else snapshot(candidate)
            if sum(complete(row) for row in candidate_values.values()) >= 3:
                selected_minute, values = candidate, candidate_values
                break
    common = [symbol for symbol, row in values.items() if complete(row)]
    common_set = set(common)
    index = MINUTE_INDEX[selected_minute]
    features = []
    for symbol, bars in sorted(panel.stocks.items()):
        feature_values = trading_features(bars, returns[symbol], index, window)
        features.append({
            "symbol": symbol, "in_common_sample": symbol in common_set,
            "missing_factors": [factor["id"] for factor in FACTORS if values[symbol][factor["id"]] is None or not math.isfinite(values[symbol][factor["id"]])],
            "missing_features": [feature["id"] for feature in FEATURES if feature_values[feature["id"]] is None],
            "values": feature_values, "factors": values[symbol],
        })
    rows = [[float(values[symbol][factor["id"]]) for factor in FACTORS] for symbol in common]
    return {
        "minute": clock_text(selected_minute), "signal_through": clock_text(selected_minute) + ":59.999",
        "automatic": automatic, "window": window, "universe": len(panel.stocks),
        "common_count": len(common), "common_symbols": common,
        "excluded_count": len(panel.stocks) - len(common),
        "factors": FACTORS, "feature_definitions": FEATURES,
        "available_counts": {factor["id"]: sum(row[factor["id"]] is not None and math.isfinite(row[factor["id"]]) for row in values.values()) for factor in FACTORS},
        "matrices": matrices(rows), "features": features,
        "warnings": [
            "所有矩阵格子都使用十因子同时有值的同一批股票，不逐对更换样本，也不混合多个时点。",
            "Pearson 衡量线性关系；Spearman 衡量秩关系（同值取平均秩）。常数列相关性无定义，包括其对角线。",
            "单日单时点相关性只是描述；高相关不代表因果或可获利，也不自动构成删除因子的依据。",
            "相关性分析不需要后续收益，故可选择 14:56 等没有后续完整持有期的时点；没有引入未来数据。",
            "交易量、金额特征使用最近 L 个交易分钟；窗口收益和波动率需要连续分钟价格，不跨午休。盘口特征取信号分钟末快照，不是窗口均值。",
            "特征缺失显示为 —；无盘口字段、无效或交叉报价不会用 0 或更早报价回填。",
        ],
    }
