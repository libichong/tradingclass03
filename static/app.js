"use strict";

const $ = (selector) => document.querySelector(selector);
const names = {exe: "逐笔成交", snp: "行情快照", ord: "逐笔委托"};
const english = {exe: "EXECUTIONS", snp: "SNAPSHOTS", ord: "ORDERS"};
const state = {files: [], kind: "exe", data: null, page: 1, busy: false, controller: null};
const number = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 6});
const coreColumns = {
  exe: ["Symbol", "Time", "Price", "Volume", "BSFlag", "AskOrder", "BidOrder", "BizIndex", "Channel"],
  ord: ["Symbol", "Time", "Price", "Volume", "FunctionCode", "OrderKind", "OrderNumber", "Channel"],
  snp: ["Symbol", "Time", "Price", "Volume", "PreClose", "Open", "High", "Low", "AccVolume", "AccTurnover"],
};

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

function format(value, key) {
  if (value === null || value === undefined) return "—";
  if (key === "Symbol") return String(value).padStart(6, "0");
  if (key === "Time") {
    const time = String(value).padStart(9, "0");
    if (/^\d{9}$/.test(time)) return `${time.slice(0, 2)}:${time.slice(2, 4)}:${time.slice(4, 6)}.${time.slice(6)}`;
  }
  return typeof value === "number" ? number.format(value) : String(value);
}

function status(message, error = false) {
  $("#status").textContent = message;
  $("#status").classList.toggle("error", error);
  $("#status").hidden = !message;
}

function setBusy(busy) {
  state.busy = busy;
  $("#table-wrap").setAttribute("aria-busy", String(busy));
  document.querySelectorAll("#filters input, #filters select, #filters button, .tabs button, #refresh, #page-size, #jump-form input, #jump-form button").forEach((element) => {
    element.disabled = busy;
  });
  $("#query").disabled = busy || !$("#file").value;
  $("#prev").disabled = busy || !state.data || state.page <= 1;
  $("#next").disabled = busy || !state.data || state.page >= state.data.pages;
}

async function getJSON(url, signal) {
  const response = await fetch(url, {signal});
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}

function renderSummary() {
  $("#summary").replaceChildren();
  for (const kind of ["exe", "snp", "ord"]) {
    const files = state.files.filter((file) => file.kind === kind);
    const card = node("article", undefined, "stat-card");
    const label = node("div", names[kind], "stat-label");
    label.append(node("span", english[kind]));
    const count = node("div", number.format(files.reduce((sum, file) => sum + file.total, 0)), "stat-number");
    count.append(node("small", "条记录"));
    card.append(label, count, node("div", `${files.length} 个文件 · ${(files.reduce((sum, file) => sum + file.size, 0) / 1024 / 1024).toFixed(1)} MiB`, "stat-meta"));
    $("#summary").append(card);
  }
}

function selectFiles(previous = "") {
  $("#file").replaceChildren();
  state.files.filter((file) => file.kind === state.kind).forEach((file) => {
    $("#file").append(new Option(file.name, file.name));
  });
  if ([...$("#file").options].some((option) => option.value === previous)) $("#file").value = previous;
  if (!$("#file").options.length) $("#file").append(new Option("暂无该类型文件", ""));
  document.querySelectorAll(".tabs button").forEach((button) => {
    button.setAttribute("aria-selected", String(button.dataset.kind === state.kind));
  });
  $("#table-title").textContent = names[state.kind];
}

function clearData() {
  state.data = null;
  state.page = 1;
  $("#data-table thead").replaceChildren();
  $("#data-table tbody").replaceChildren();
  $("#result-count").textContent = "";
  $("#range-info").textContent = "暂无数据";
  $("#page-info").textContent = "— / —";
  $("#empty").hidden = true;
}

async function loadFiles() {
  setBusy(true);
  clearData();
  status("正在读取本地文件目录…");
  try {
    const result = await getJSON("/api/files");
    state.files = result.files;
    renderSummary();
    selectFiles($("#file").value);
  } catch (error) {
    state.files = [];
    renderSummary();
    selectFiles();
    status(error.message, true);
    setBusy(false);
    return;
  }
  await loadData(1);
}

async function loadData(page) {
  if (state.controller) state.controller.abort();
  const controller = new AbortController();
  state.controller = controller;
  clearData();
  if (!$("#file").value) {
    status("Data 目录中没有此类型的 Parquet 文件。添加文件后点击“刷新文件”。");
    setBusy(false);
    return;
  }
  const params = new URLSearchParams({
    file: $("#file").value, symbol: $("#symbol").value.trim(),
    start: $("#start").value, end: $("#end").value,
    page: String(page), page_size: $("#page-size").value,
  });
  for (const key of ["start", "end"]) {
    if (/^\d{2}:\d{2}$/.test(params.get(key))) params.set(key, `${params.get(key)}:00`);
  }
  setBusy(true);
  status("正在查询数据…");
  try {
    state.data = await getJSON(`/api/data?${params}`, controller.signal);
    state.page = state.data.page;
    renderTable();
    status("");
  } catch (error) {
    if (error.name !== "AbortError") status(error.message, true);
  } finally {
    if (state.controller === controller) setBusy(false);
  }
}

function renderTable() {
  if (!state.data) return;
  const {columns, rows, total, page, page_size, pages} = state.data;
  const visible = $("#all-columns").checked ? columns : columns.filter((col) => coreColumns[state.kind].includes(col.name));
  const head = node("tr");
  head.append(node("th", "#"));
  visible.forEach((col) => {
    const th = node("th", col.label);
    th.append(node("small", col.name));
    th.title = `${col.name}: ${col.type}`;
    head.append(th);
  });
  head.append(node("th", "详情"));
  $("#data-table thead").replaceChildren(head);
  const body = document.createDocumentFragment();
  rows.forEach((row, index) => {
    const tr = node("tr");
    tr.append(node("td", number.format((page - 1) * page_size + index + 1), "muted"));
    visible.forEach((col) => {
      const td = node("td", format(row[col.name], col.name), col.name === "Symbol" ? "symbol" : col.name.includes("Price") ? "price" : "");
      td.title = row[col.name] === null ? "null" : String(row[col.name]);
      tr.append(td);
    });
    const action = node("td");
    const button = node("button", "查看 ↗", "detail-button");
    button.type = "button";
    button.setAttribute("aria-label", `查看第 ${(page - 1) * page_size + index + 1} 条记录详情`);
    button.addEventListener("click", () => showDetail(row, columns));
    action.append(button);
    tr.append(action);
    body.append(tr);
  });
  $("#data-table tbody").replaceChildren(body);
  $("#empty").hidden = rows.length !== 0;
  $("#result-count").textContent = `匹配 ${number.format(total)} 条 · ${columns.length} 个字段`;
  $("#range-info").textContent = total ? `显示 ${number.format((page - 1) * page_size + 1)} – ${number.format((page - 1) * page_size + rows.length)}，共 ${number.format(total)} 条` : "共 0 条记录";
  $("#page-info").textContent = `${number.format(page)} / ${number.format(pages)}`;
  $("#jump-page").value = page;
  $("#jump-page").max = pages;
  $("#table-wrap").scrollTop = 0;
}

function showDetail(row, columns) {
  $("#detail-title").textContent = `${format(row.Symbol, "Symbol")} · ${format(row.Time, "Time")}`;
  $("#detail-fields").replaceChildren();
  columns.forEach((col) => {
    const field = node("div", undefined, "field");
    const dl = node("dl");
    dl.append(node("dt", `${col.label} / ${col.name}`), node("dd", row[col.name] === null ? "null" : String(row[col.name])));
    field.append(dl);
    $("#detail-fields").append(field);
  });
  $("#depth").replaceChildren();
  if (state.kind === "snp" && "BidPrice1" in row && "AskPrice1" in row) {
    const table = node("table");
    const thead = node("thead");
    const header = node("tr");
    ["档位", "买价", "买量", "卖价", "卖量"].forEach((label) => header.append(node("th", label)));
    thead.append(header);
    const tbody = node("tbody");
    for (let level = 1; level <= 10; level++) {
      const tr = node("tr");
      tr.append(node("td", String(level)));
      for (const key of ["BidPrice", "BidVolume", "AskPrice", "AskVolume"]) {
        tr.append(node("td", format(row[`${key}${level}`], key), key.startsWith("Bid") ? "bid" : "ask"));
      }
      tbody.append(tr);
    }
    table.append(thead, tbody);
    $("#depth").append(table);
  }
  $("#detail").showModal();
}

$("#filters").addEventListener("submit", (event) => {event.preventDefault(); loadData(1);});
$("#refresh").addEventListener("click", loadFiles);
$("#file").addEventListener("change", () => loadData(1));
$("#reset").addEventListener("click", () => {
  for (const id of ["symbol", "start", "end"]) $(`#${id}`).value = "";
  loadData(1);
});
document.querySelectorAll(".tabs button").forEach((button) => {
  button.addEventListener("click", () => {
    state.kind = button.dataset.kind;
    selectFiles();
    loadData(1);
  });
});
$("#page-size").addEventListener("change", () => loadData(1));
$("#all-columns").addEventListener("change", renderTable);
$("#prev").addEventListener("click", () => loadData(state.page - 1));
$("#next").addEventListener("click", () => loadData(state.page + 1));
$("#jump-form").addEventListener("submit", (event) => {
  event.preventDefault();
  if (!state.busy && state.data) loadData(Number($("#jump-page").value));
});
$("#close-detail").addEventListener("click", () => $("#detail").close());
loadFiles();
