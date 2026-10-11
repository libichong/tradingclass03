"use strict";

let factorAnalysis = null;
let analysisController = null;
let analysisBusy = false;
const featureNumber = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});

function analysisStatus(text, error = false) {
  $("#analysis-status").textContent = text;
  $("#analysis-status").classList.toggle("error", error);
}
function resetAnalysis() {
  if (analysisController) analysisController.abort();
  analysisController = null;
  analysisBusy = false;
  factorAnalysis = null;
  $("#analysis-minute").value = "";
  $("#analysis-minute").disabled = true;
  analysisStatus("等待本次回测参数对应的截面数据…");
  renderAnalysis();
}
function acceptAnalysis(data) {
  factorAnalysis = data;
  $("#analysis-minute").disabled = false;
  analysisStatus(data.common_count >= 3
    ? `信号截止 ${data.signal_through}：${data.common_count} 只股票为所有格子的共同样本；排除 ${data.excluded_count} 只。`
    : `信号截止 ${data.signal_through}：共同样本仅 ${data.common_count} 只，不足 3 只，相关矩阵不生成系数。交易特征仍可查看。`);
  renderAnalysis();
}
function renderAnalysis() {
  hideFactorHelp();
  const data = factorAnalysis;
  const matrix = $("#correlation-matrix");
  matrix.querySelector("thead").replaceChildren();
  matrix.querySelector("tbody").replaceChildren();
  $("#correlation-pairs").replaceChildren();
  $("#factor-availability").replaceChildren();
  $("#analysis-warnings").replaceChildren();
  if (!data) {
    $("#analysis-summary").textContent = "暂无本次参数的截面结果";
    renderTradingFeatures();
    return;
  }
  const method = $("#correlation-method").value;
  const coefficients = data.matrices[method];
  $("#analysis-summary").textContent = `${report.date} · ${data.signal_through} · 回看 ${data.window} 分钟 · N=${data.common_count}/${data.universe}`;
  const header = el("tr");
  header.append(el("th", `${method === "pearson" ? "Pearson" : "Spearman"} / N=${data.common_count}`));
  data.factors.forEach((factor, index) => {
    const th = el("th", `F${index + 1}`);
    th.scope = "col";
    th.title = factor.name;
    th.setAttribute("aria-label", factor.name);
    header.append(th);
  });
  matrix.querySelector("thead").append(header);
  const rows = data.factors.map((factor, index) => {
    const row = el("tr");
    const th = el("th");
    th.scope = "row";
    const name = el("button", `F${index + 1} ${factor.name}`, "factor-link");
    name.type = "button";
    bindFactorHelp(name, () => factor);
    th.append(name); row.append(th);
    data.factors.forEach((other, column) => {
      const value = coefficients.values[index][column];
      const cell = el("td", decimal(value, 2), value === null ? "correlation-missing" : "");
      if (value !== null) {
        const rgb = value >= 0 ? "207,91,112" : "65,119,196";
        cell.style.backgroundColor = `rgba(${rgb},${0.08 + Math.abs(value) * 0.68})`;
      }
      cell.title = `${factor.name} × ${other.name}\n${method}: ${decimal(value, 6)}；共同样本 N=${data.common_count}${coefficients.reasons[index][column] ? `\n${coefficients.reasons[index][column]}` : ""}`;
      cell.setAttribute("aria-label", cell.title);
      cell.tabIndex = 0;
      row.append(cell);
    });
    return row;
  });
  matrix.querySelector("tbody").append(...rows);
  const names = Object.fromEntries(data.factors.map((factor) => [factor.id, factor.name]));
  coefficients.top_pairs.forEach((pair) => {
    const item = el("div", undefined, "correlation-pair");
    item.append(el("span", `${names[pair.left]} × ${names[pair.right]}`), el("strong", decimal(pair.value)));
    $("#correlation-pairs").append(item);
  });
  if (!coefficients.top_pairs.length) $("#correlation-pairs").append(el("p", "共同样本不足或因子无方差，没有可排序的相关系数。"));
  data.factors.forEach((factor, index) => {
    const item = el("div", undefined, "availability-row");
    item.title = factor.name;
    item.append(el("span", `F${index + 1} ${factor.name}`), el("strong", String(data.available_counts[factor.id])));
    $("#factor-availability").append(item);
  });
  $("#analysis-warnings").append(...data.warnings.map((warning) => el("li", warning)));
  renderTradingFeatures();
}
function renderTradingFeatures() {
  const data = factorAnalysis;
  $("#trading-features thead").replaceChildren();
  $("#feature-definitions").replaceChildren();
  if (!data) {
    table("trading-features", [], 2);
    $("#feature-summary").textContent = "暂无本次参数的交易特征";
    return;
  }
  const header = el("tr");
  header.append(el("th", "证券代码"), el("th", "相关样本"));
  data.feature_definitions.forEach((feature) => {
    const th = el("th", feature.name);
    th.title = feature.definition;
    th.tabIndex = 0;
    header.append(th);
    const definition = el("div", undefined, "definition-row");
    definition.append(el("strong", feature.name), el("span", feature.definition));
    $("#feature-definitions").append(definition);
  });
  $("#trading-features thead").append(header);
  const code = $("#feature-symbol").value.trim();
  const commonOnly = $("#features-common").checked;
  const shown = data.features.filter((stock) => stock.symbol.includes(code) && (!commonOnly || stock.in_common_sample));
  const names = Object.fromEntries(data.factors.map((factor) => [factor.id, factor.name]));
  table("trading-features", shown.map((stock) => {
    const row = el("tr");
    const membership = el("td", stock.in_common_sample ? "纳入" : "排除", stock.in_common_sample ? "common-stock" : "excluded-stock");
    membership.title = stock.in_common_sample ? "十个因子均有有效值" : `缺失 / 无定义：${stock.missing_factors.map((key) => names[key]).join("、")}`;
    row.append(el("td", stock.symbol), membership);
    data.feature_definitions.forEach((feature) => {
      const value = stock.values[feature.id];
      const text = value === null ? "—" : feature.format === "percent" ? percent(value) : feature.format === "price" ? decimal(value, 4) : feature.format === "bps" ? decimal(value, 2) : featureNumber.format(value);
      const cell = el("td", text);
      cell.title = value === null ? `无定义 / 缺失。${feature.definition}` : `${value}\n${feature.definition}`;
      row.append(cell);
    });
    return row;
  }), data.feature_definitions.length + 2);
  $("#feature-summary").textContent = `${data.signal_through} · 显示 ${shown.length}/${data.universe} 只 · ${data.feature_definitions.length} 项特征 · 窗口 ${data.window} 分钟`;
}
async function requestAnalysis() {
  if (!report || busy) return;
  if (analysisController) analysisController.abort();
  const controller = new AbortController();
  analysisController = controller;
  analysisBusy = true;
  const sourceReport = report;
  factorAnalysis = null;
  renderAnalysis();
  analysisStatus("正在按所选时点重新计算共同样本和交易特征…");
  try {
    const response = await fetch("/api/backtest/analysis", {
      method: "POST", headers: {"Content-Type": "application/json"}, signal: controller.signal,
      body: JSON.stringify({date: sourceReport.date, ...sourceReport.parameters, minute: $("#analysis-minute").value || null}),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `截面分析失败 (${response.status})`);
    if (report === sourceReport && analysisController === controller) acceptAnalysis(data.analysis);
  } catch (error) {
    if (error.name !== "AbortError" && analysisController === controller) analysisStatus(error.message, true);
  } finally {
    if (analysisController === controller) analysisBusy = false;
  }
}
$("#analysis-minute").addEventListener("change", requestAnalysis);
$("#correlation-method").addEventListener("change", renderAnalysis);
$("#feature-symbol").addEventListener("input", renderTradingFeatures);
$("#features-common").addEventListener("change", renderTradingFeatures);
initialize();
