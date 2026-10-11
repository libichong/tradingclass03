"use strict";
const $ = (selector) => document.querySelector(selector);
const palette = ["#7aa6ff", "#b291ef", "#e2be79", "#f397a6", "#51dec2"];
const factorHelp = {
  skew: "观察分钟收益分布是否对称。正值表示右尾更长，负值表示左尾更长；偏度不是平均涨幅，也不表示未来一定上涨或下跌。",
  downside: "衡量总波动中有多少来自负收益，取值在 0 到 1 之间。越高表示下行波动占比越大，不代表下跌幅度一定更大。",
  open_intent_ratio: "衡量开盘后新增委托中买方申报金额的占比。越接近 1，买方新增申报越占优势；申报不等于成交，也未扣除后续撤单。",
  open_intent_strength: "用开盘后的净买方申报金额与实际成交额比较。正值表示新增买委托额更多，绝对值越大表示相对成交规模的申报失衡越强；它不是百分比，可超出 ±1。",
  open_large_ratio: "衡量开盘大单的主动净买入占总成交额的比重。正值偏向大单主动买入，负值偏向主动卖出；大单是单笔成交额达阈值，不是识别出的机构母单。",
  open_large_strength: "只在已知方向的大单成交内部衡量买卖失衡，范围为 −1 到 1。接近 1 表示主动买入占优，接近 −1 表示主动卖出占优；不同于用全部成交额作分母的净买入占比。",
  reversal: "先取窗口累计分钟收益的反方向，再按资金流失衡程度衰减。通常下跌对应正值、上涨对应负值；方向资金流越强，反转信号越弱，不保证随后发生反转。",
  tail_share: "观察截至当前时刻，尾盘成交量占当日已发生成交量的比例。越高表示成交更集中于尾盘，不代表买入或卖出方向；不能用尚未发生的全日成交量作分母。",
  outflow_share: "比较平均每笔主动卖出与主动买入的金额。大于 0.5 表示平均卖出单笔更大，小于 0.5 表示平均买入单笔更大；这不等于总净流出比例。",
  large_push: "观察分钟涨跌与大单净流方向是否同向。上涨伴随大单净买入、或下跌伴随大单净卖出，都会产生正贡献；因此数值为正不等于价格上涨，也不能证明大单导致了涨跌。",
};
let catalog = null;
let report = null;
let busy = false;
function el(tag, text, className = "") {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  item.className = className;
  return item;
}
const factorTooltip = el("aside", undefined, "factor-tooltip");
factorTooltip.id = "factor-tooltip";
factorTooltip.setAttribute("role", "tooltip");
factorTooltip.hidden = true;
document.body.append(factorTooltip);
let tooltipAnchor = null;
let tooltipTimer = null;

function hideFactorHelp() {
  clearTimeout(tooltipTimer);
  if (tooltipAnchor) tooltipAnchor.removeAttribute("aria-describedby");
  tooltipAnchor = null;
  factorTooltip.hidden = true;
}
function showFactorHelp(anchor, factor) {
  if (!factor) return;
  hideFactorHelp();
  tooltipAnchor = anchor;
  anchor.setAttribute("aria-describedby", factorTooltip.id);
  factorTooltip.replaceChildren(
    el("strong", factor.name, "tooltip-heading"),
    el("p", factorHelp[factor.id]),
    el("span", "本项目计算口径", "tooltip-label"),
    el("p", factor.formula),
    el("span", "生效条件 / 注意事项", "tooltip-label"),
    el("p", factor.availability),
    el("small", "研究代理公式，非原研报复现；因子高低不等于未来收益好坏。Esc 关闭。"),
  );
  factorTooltip.hidden = false;
  const anchorRect = anchor.getBoundingClientRect();
  const tipRect = factorTooltip.getBoundingClientRect();
  const left = Math.max(12, Math.min(anchorRect.left, window.innerWidth - tipRect.width - 12));
  const below = anchorRect.bottom + 8;
  const top = below + tipRect.height <= window.innerHeight - 12 ? below : Math.max(12, anchorRect.top - tipRect.height - 8);
  factorTooltip.style.left = `${left}px`;
  factorTooltip.style.top = `${top}px`;
}
function deferHideFactorHelp() {
  clearTimeout(tooltipTimer);
  tooltipTimer = setTimeout(() => {
    if (!factorTooltip.matches(":hover") && !tooltipAnchor?.matches(":hover") && document.activeElement !== tooltipAnchor) hideFactorHelp();
  }, 150);
}
function bindFactorHelp(anchor, resolveFactor) {
  anchor.classList.add("has-factor-help");
  anchor.addEventListener("mouseenter", () => showFactorHelp(anchor, resolveFactor()));
  anchor.addEventListener("mouseleave", deferHideFactorHelp);
  anchor.addEventListener("focus", () => showFactorHelp(anchor, resolveFactor()));
  anchor.addEventListener("blur", deferHideFactorHelp);
}
factorTooltip.addEventListener("mouseenter", () => clearTimeout(tooltipTimer));
factorTooltip.addEventListener("mouseleave", deferHideFactorHelp);
document.addEventListener("keydown", (event) => { if (event.key === "Escape") hideFactorHelp(); });
document.addEventListener("scroll", (event) => {
  if (!factorTooltip.contains(event.target)) hideFactorHelp();
}, true);
window.addEventListener("resize", hideFactorHelp);
bindFactorHelp($("#bt-factor"), () => catalog?.factors.find((factor) => factor.id === $("#bt-factor").value));
function percent(value) { return value === null || value === undefined ? "—" : `${(value * 100).toFixed(2)}%`; }
function decimal(value, digits = 3) { return value === null || value === undefined ? "—" : Number(value).toFixed(digits); }
function color(value) { return value > 0 ? "positive" : value < 0 ? "negative" : ""; }
function status(text, error = false) {
  $("#bt-status").textContent = text;
  $("#bt-status").classList.toggle("error", error);
}
function table(id, rows, width) {
  if (!rows.length) {
    const row = el("tr", undefined, "no-data");
    const cell = el("td", "没有可用结果；请查看公式生效时点、样本量或错误提示。");
    cell.colSpan = width;
    row.append(cell);
    rows.push(row);
  }
  $(`#${id} tbody`).replaceChildren(...rows);
}
function svgNode(tag, attributes, text) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attributes)) node.setAttribute(key, String(value));
  if (text !== undefined) node.textContent = text;
  return node;
}
function chart(id, points, series, percentage = false) {
  const svg = $(`#${id}`);
  svg.replaceChildren();
  if (!points.length) {
    svg.append(svgNode("text", {x: 400, y: 145, fill: "#8899ad", "text-anchor": "middle", "font-size": 14}, "暂无有效回测区间"));
    return;
  }
  const values = points.flatMap((point) => series.map((line) => line.value(point)));
  let low = Math.min(...values), high = Math.max(...values);
  const padding = Math.max((high - low) * 0.15, percentage ? 0.001 : 0.0005);
  low -= padding; high += padding;
  const x = (index) => 74 + index / Math.max(1, points.length - 1) * 704;
  const y = (value) => 254 - (value - low) / (high - low) * 222;
  for (let index = 0; index <= 4; index++) {
    const value = low + (high - low) * index / 4;
    svg.append(svgNode("line", {x1: 74, x2: 778, y1: y(value), y2: y(value), stroke: "#243446", "stroke-dasharray": "3 5"}));
    svg.append(svgNode("text", {x: 62, y: y(value) + 4, fill: "#7f94ad", "font-size": 11, "text-anchor": "end"}, percentage ? percent(value) : value.toFixed(4)));
  }
  const positions = [...new Set([0, Math.floor((points.length - 1) / 2), points.length - 1])];
  positions.forEach((index) => svg.append(svgNode("text", {x: x(index), y: 280, fill: "#7f94ad", "font-size": 11, "text-anchor": "middle"}, points[index].time)));
  series.forEach((line) => {
    svg.append(svgNode("polyline", {points: points.map((point, index) => `${x(index)},${y(line.value(point))}`).join(" "), fill: "none", stroke: line.color, "stroke-width": 2}));
    points.forEach((point, index) => {
      const dot = svgNode("circle", {cx: x(index), cy: y(line.value(point)), r: 3, fill: line.color, opacity: 0.5});
      dot.append(svgNode("title", {}, `${point.time} ${line.label}: ${percentage ? percent(line.value(point)) : decimal(line.value(point), 6)}`));
      svg.append(dot);
    });
  });
}
function legend(id, series) {
  $(`#${id}`).replaceChildren(...series.map((line) => {
    const item = el("span");
    const icon = el("i");
    icon.style.backgroundColor = line.color;
    item.append(icon, document.createTextNode(line.label));
    return item;
  }));
}
function definitions() {
  $("#formula-list").replaceChildren(...catalog.factors.map((factor, index) => {
    const row = el("div", undefined, "definition-row");
    const content = el("span", factor.formula);
    content.append(el("small", factor.availability));
    const name = el("button", `${index + 1}. ${factor.name}`, "factor-link definition-name");
    name.type = "button";
    bindFactorHelp(name, () => factor);
    row.append(name, content);
    return row;
  }));
}
function render() {
  const explainSelection = tooltipAnchor === $("#bt-factor");
  hideFactorHelp();
  const id = $("#bt-factor").value;
  const formula = catalog.factors.find((factor) => factor.id === id);
  $("#active-formula").replaceChildren(document.createTextNode(formula.formula), el("small", formula.availability));
  if (explainSelection) showFactorHelp($("#bt-factor"), formula);
  const result = report?.results.find((item) => item.id === id);
  const stats = result?.metrics;
  const cards = [
    ["策略扣费收益率", percent(stats?.total_return), "每期全额开平仓，收益复利", stats?.total_return],
    ["累计相对收益", percent(result?.relative_return), "策略净值 / 同期等权基准 − 1", result?.relative_return],
    ["最大回撤", percent(stats?.max_drawdown), "仅持有期端点采样", null],
    ["期频夏普", decimal(stats?.sharpe_period), stats?.sharpe_reason || "均值 / 样本标准差，RF=0", null],
    ["年化夏普 · 参考", decimal(stats?.sharpe_annual_reference, 2), "单日机械外推，不具可靠性", null],
    ["有效持有期", stats ? String(stats.periods) : "—", `平均 Rank IC：${decimal(result?.rank_ic)}`, null],
  ];
  $("#bt-metrics").replaceChildren(...cards.map(([title, value, note, change]) => {
    const card = el("article", undefined, "metric");
    card.append(el("span", title), el("strong", value, color(change)), el("small", note));
    return card;
  }));
  const lines = [
    {label: "策略组（扣费）", color: "#f397a6", value: (point) => point.equity},
    {label: "同期等权基准（扣费）", color: "#8398b3", value: (point) => point.benchmark},
    {label: "50/50 多空（研究）", color: "#51dec2", value: (point) => point.spread},
  ];
  legend("nav-legend", lines);
  chart("nav-chart", result?.curve || [], lines);
  chart("dd-chart", result?.curve || [], [{label: "回撤", color: "#f397a6", value: (point) => point.drawdown}], true);
  const groupCount = report?.parameters.groups || Number($("#bt-groups").value);
  const groupLines = Array.from({length: groupCount}, (_, index) => ({label: `G${index + 1}`, color: palette[index], value: (point) => point.groups[index]}));
  $("#group-number").textContent = groupCount;
  legend("group-legend", groupLines);
  chart("group-chart", result?.curve || [], groupLines);
  table("factor-results", (report?.results || []).map((factor) => {
    const row = el("tr", undefined, factor.id === id ? "selected" : "");
    const cell = el("td");
    const button = el("button", factor.name, "factor-link");
    button.type = "button";
    bindFactorHelp(button, () => factor);
    button.addEventListener("click", () => { $("#bt-factor").value = factor.id; render(); });
    cell.append(button); row.append(cell);
    if (factor.metrics) {
      row.append(el("td", percent(factor.metrics.total_return), color(factor.metrics.total_return)), el("td", percent(factor.metrics.max_drawdown)), el("td", decimal(factor.metrics.sharpe_period)), el("td", decimal(factor.rank_ic)), el("td", String(factor.metrics.periods)), el("td", `${factor.periods[0].entry}–${factor.periods.at(-1).exit}`));
    } else {
      const missing = el("td", factor.reason);
      missing.colSpan = 6; row.append(missing);
    }
    return row;
  }), 7);
  table("group-results", (result?.group_metrics || []).map((group) => {
    const row = el("tr");
    row.append(el("td", `G${group.group}`), el("td", percent(group.total_return), color(group.total_return)), el("td", percent(group.max_drawdown)), el("td", decimal(group.sharpe_period)), el("td", percent(group.win_rate)));
    return row;
  }), 5);
  $("#factor-diagnostics").textContent = result ? (result.reason || `跳过 ${result.skipped_periods} 期（因子未生效 / 样本或不同取值不足）；${result.tied_periods} 期存在同值，按代码稳定排序。基准收益 ${percent(result.benchmark_metrics.total_return)}，多空收益 ${percent(result.spread_metrics.total_return)}。`) : "等待计算";
  table("period-results", (result?.periods || []).map((period) => {
    const row = el("tr");
    row.append(el("td", period.signal_through), el("td", `${period.entry} → ${period.exit}`), el("td", String(period.universe)), el("td", percent(period.gross_return), color(period.gross_return)), el("td", percent(period.net_return), color(period.net_return)), el("td", percent(period.benchmark_return)), el("td", decimal(period.rank_ic)), el("td", period.members.join(" · "), "members"));
    return row;
  }), 8);
  table("value-results", (result?.latest_values || []).map((value) => {
    const row = el("tr");
    row.append(el("td", value.symbol), el("td", decimal(value.value, 6)), el("td", `G${value.group}`), el("td", percent(value.forward_return), color(value.forward_return)));
    return row;
  }), 4);
  $("#last-signal").textContent = result?.periods.length ? `信号截止 ${result.periods.at(-1).signal_through}` : "暂无有效截面";
  if (report) {
    $("#bt-coverage").textContent = `${report.date} · ${report.quality.symbols} 只证券 · ${report.parameters.window} 分钟窗口 / ${report.parameters.holding} 分钟持有 · ${report.parameters.direction === "high" ? "做多高组" : "做多低组"}`;
    $("#bt-warnings").replaceChildren(...report.warnings.map((warning) => el("li", warning)));
    const q = report.quality;
    $("#quality-info").textContent = `连续交易分钟：${q.minute_slots}\n股票池：${q.symbols}（固定当日文件样本，不代表全市场）\n成交记录：${q.execution_rows.toLocaleString()}\n未知成交方向：${q.unknown_direction_rows}\n无效快照价格行：${q.excluded_snapshot_rows}\n缺少有效价格的分钟格：${q.missing_price_bars}\n无快照覆盖的成交证券：${q.execution_symbols_without_snapshots.join(", ") || "无"}\n数据文件：${Object.values(report.files).join(" / ")}\n参数：${JSON.stringify(report.parameters)}`;
  } else {
    $("#bt-coverage").textContent = "等待成功计算";
    $("#bt-warnings").replaceChildren();
    $("#quality-info").textContent = "暂无有效回测结果";
  }
}
async function run() {
  if (busy || !$("#bt-date").value) return;
  busy = true;
  document.querySelectorAll("#backtest-form input, #backtest-form select, #backtest-form button").forEach((item) => { item.disabled = true; });
  report = null;
  resetAnalysis();
  render();
  status("正在聚合真实逐笔数据，计算十个因子并验证滞后成交时序…");
  try {
    const response = await fetch("/api/backtest/run", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({
      date: $("#bt-date").value, window: Number($("#bt-window").value), holding: Number($("#bt-holding").value),
      groups: Number($("#bt-groups").value), direction: $("#bt-direction").value,
      large_threshold: Number($("#bt-threshold").value), fee_bps: Number($("#bt-fee").value), slippage_bps: Number($("#bt-slippage").value),
    })});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `回测失败 (${response.status})`);
    report = data;
    render();
    acceptAnalysis(data.analysis);
    const count = data.results.filter((factor) => factor.status === "ok").length;
    status(`计算完成：${count} / 10 个因子有可用分组结果。曲线与指标来自实际文件；请同时查看有效期数和研究限制。`);
  } catch (error) {
    status(error.message, true);
  } finally {
    busy = false;
    document.querySelectorAll("#backtest-form input, #backtest-form select, #backtest-form button").forEach((item) => { item.disabled = false; });
  }
}
$("#backtest-form").addEventListener("submit", (event) => { event.preventDefault(); run(); });
$("#bt-factor").addEventListener("change", render);
async function initialize() {
  $("#bt-run").disabled = true;
  try {
    const response = await fetch("/api/backtest/catalog");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "数据目录加载失败");
    catalog = data;
    for (const item of data.datasets) {
      const option = new Option(`${item.date}${item.complete ? "" : `（缺 ${item.missing.join("/")}）`}`, item.date);
      option.disabled = !item.complete;
      $("#bt-date").append(option);
    }
    for (const factor of data.factors) $("#bt-factor").append(new Option(factor.name, factor.id));
    for (const minute of data.analysis_minutes) $("#analysis-minute").append(new Option(`${minute}:59.999`, minute));
    definitions();
    render();
    const available = data.datasets.filter((item) => item.complete);
    if (!available.length) throw new Error("没有同日完整的 exe_日期 / snp_日期 / ord_日期 Parquet 文件");
    $("#bt-date").value = available.at(-1).date;
    $("#bt-run").disabled = false;
    await run();
  } catch (error) {
    status(error.message, true);
  }
}
