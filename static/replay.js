"use strict";

const $ = (selector) => document.querySelector(selector);
const number = new Intl.NumberFormat("zh-CN", {maximumFractionDigits: 2});
const labels = {NEW: "新增", CANCEL: "撤减", PHASE: "阶段", WARNING: "警告", OPEN: "挂单", PARTIAL: "部分完成", FILLED: "全成", CANCELED: "已撤", ACTIVE: "执行中", STOPPED: "已停止", single: "单笔", split: "定时拆单", iceberg: "冰山"};
let token = "";
let snapshot = null;
let busy = false;
let activeRequest = "";
let pendingCommand = null;
let timer = null;
let polling = true;
let renderedVersion = -1;

function el(tag, text, className = "") {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  item.className = className;
  return item;
}
function fmt(value) { return value === null || value === undefined ? "—" : number.format(value); }
function message(text, error = false) {
  $("#lab-status").textContent = text;
  $("#lab-status").classList.toggle("error", error);
}
function sideCell(side) { return el("td", side === "B" ? "买 B" : side === "S" ? "卖 S" : "—", side === "B" ? "buy-text" : "sell-text"); }
function timeValue(id) {
  const value = $(id).value;
  return value.length === 5 ? `${value}:00.000` : value;
}
function controls() {
  const blocked = Boolean(pendingCommand) || (busy && activeRequest !== "tick");
  document.querySelectorAll("#source-form input, #source-form select, #source-form button, #inject-form input, #inject-form select, #inject-form button, #speed, button.mini").forEach((item) => { item.disabled = blocked; });
  const ready = Boolean(snapshot) && !snapshot.error;
  $("#step").disabled = blocked || !ready || snapshot.running || snapshot.finished;
  $("#play").disabled = blocked || !ready || (snapshot.finished && !snapshot.running);
  $("#inject-submit").disabled = blocked || !ready;
  $("#play").textContent = snapshot?.running ? "Ⅱ 暂停回放" : "▶ 连续回放";
}
async function request(path, method, payload) {
  const response = await fetch(`/api/replay/${path}`, {
    method, headers: {"Content-Type": "application/json", "X-Replay-Session": token},
    ...(payload === undefined ? {} : {body: JSON.stringify(payload)}),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || `请求失败 (${response.status})`);
  return data;
}
function scheduleTick() {
  clearTimeout(timer);
  if (snapshot?.running && polling) timer = setTimeout(() => perform("tick", "POST"), 150);
}
async function perform(path, method = "POST", payload, success) {
  clearTimeout(timer);
  if (busy) {
    if (path !== "tick" && !pendingCommand) {
      pendingCommand = {path, method, payload, success};
      controls();
    }
    return;
  }
  busy = true;
  activeRequest = path;
  controls();
  try {
    const data = await request(path, method, payload);
    if (data.session_id) {
      token = data.session_id;
      renderedVersion = -1;
      $("#inject-price").value = data.source === "__demo__" ? "9.77" : "18.48";
    }
    snapshot = data;
    polling = true;
    render(data);
    if (data.error) {
      message(`回放已停止：${data.error}。请重新加载实验。`, true);
    } else if (success || data.submitted_id) {
      message(success || `已接收 ${data.submitted_id}；当前模拟时钟 ${data.time}。延迟 / 后续子单随回放时钟推进。`);
    } else if (data.finished) {
      message("待处理事件已结束。剩余挂单仍保留；可继续注入模拟委托或重新加载实验。");
    }
  } catch (error) {
    polling = false;
    message(`${error.message}。自动轮询已停止；请求失败不会自动重试。可点击暂停确认状态，或重新加载实验。`, true);
  } finally {
    busy = false;
    activeRequest = "";
    controls();
    if (pendingCommand) {
      const command = pendingCommand;
      pendingCommand = null;
      await perform(command.path, command.method, command.payload, command.success);
    } else {
      scheduleTick();
    }
  }
}
function fillTable(id, rows, width) {
  const body = $(`#${id} tbody`);
  if (!rows.length) {
    const row = el("tr", undefined, "placeholder-row");
    const cell = el("td", "暂无记录");
    cell.colSpan = width;
    row.append(cell);
    body.replaceChildren(row);
  } else body.replaceChildren(...rows);
}
function changedRow(version) { return el("tr", undefined, version > renderedVersion ? "highlight" : ""); }
function cancelButton(identifier) {
  const button = el("button", "撤销", "mini");
  button.type = "button";
  button.setAttribute("aria-label", `撤销 ${identifier}`);
  button.addEventListener("click", () => perform("cancel", "POST", {id: identifier}, `已撤销 ${identifier} 的剩余挂单及待提交子单。`));
  return button;
}
function renderDepth(book) {
  const maximum = Math.max(1, ...book.bids.map((row) => row.volume), ...book.asks.map((row) => row.volume));
  for (const [side, id, data] of [["ask", "ask-depth", book.asks], ["bid", "bid-depth", book.bids]]) {
    const rows = [];
    for (let index = 0; index < 10; index++) {
      const level = data[index];
      const row = el("div", undefined, `depth-row ${side}${level ? "" : " empty-level"}`);
      row.append(el("span", `${side === "ask" ? "卖" : "买"}${index + 1}`), el("span", level ? level.price.toFixed(2) : "—"), el("span", level ? fmt(level.volume) : "—"));
      if (level) {
        const bar = el("i", undefined, "depth-bar");
        bar.style.width = `${level.volume / maximum * 85}%`;
        row.append(bar);
        row.title = `${level.orders} 笔挂单；模拟量 ${fmt(level.sim_volume)}`;
      }
      rows.push(row);
    }
    $(`#${id}`).replaceChildren(...(side === "ask" ? rows.reverse() : rows));
  }
  $("#last-price").textContent = book.last_price === null ? "—" : book.last_price.toFixed(2);
}
function renderOrders(id, orders) {
  fillTable(id, orders.map((order) => {
    const row = changedRow(order.version);
    const identity = el("td", order.time, order.source === "sim" ? "sim" : "");
    identity.append(el("small", order.id + (order.parent ? ` / ${order.parent}` : "")));
    row.append(identity, el("td", fmt(order.price)), el("td", fmt(order.volume)), el("td", fmt(order.filled)), el("td", fmt(order.canceled)), el("td", fmt(order.remaining)), el("td", labels[order.status]));
    const action = el("td");
    if (order.source === "sim" && order.remaining) action.append(cancelButton(order.id));
    row.append(action);
    return row;
  }), 8);
}
function tradeDetail(trade) {
  const container = $("#trade-detail-content");
  container.replaceChildren(el("p", `${trade.time} · ${trade.side === "B" ? "主动买" : "主动卖"} · ${fmt(trade.volume)} @ ${trade.price.toFixed(2)}`, "trade-summary"));
  for (const [side, label] of [["buy", "买方"], ["sell", "卖方"]]) {
    const block = el("section");
    block.append(el("h3", label, side === "buy" ? "buy-text" : "sell-text"));
    for (const [title, value] of [["订单标识", trade[`${side}_id`]], ["申报时间", trade[`${side}_time`]], ["原始总量", fmt(trade[`${side}_total`])], ["本笔成交后余量", fmt(trade[`${side}_left`])]]) {
      const line = el("p");
      line.append(el("span", title), el("strong", value));
      block.append(line);
    }
    container.append(block);
  }
  $("#trade-detail").showModal();
}
function render(data) {
  $("#market-time").textContent = data.time;
  $("#session-badge").textContent = `${data.source === "__demo__" ? "演示数据" : data.symbol} · ${data.error ? "异常停止" : data.running ? "回放中" : "已暂停"}`;
  $("#history-progress").textContent = `历史事件 ${fmt(data.history_processed)} / ${fmt(data.history_total)}`;
  $("#progress").max = Math.max(1, data.history_total);
  $("#progress").value = data.history_processed;
  for (const [id, value] of [["trade-count", data.trade_count], ["trade-volume", data.trade_volume], ["active-count", data.active_orders], ["pending-count", data.pending], ["warning-count", data.warnings]]) $(`#${id}`).textContent = fmt(value);
  if (renderedVersion === data.version && !data.submitted_id) return;
  renderDepth(data.book);
  fillTable("event-table", data.events.map((event) => {
    const row = changedRow(event.version);
    const time = el("td", event.time);
    time.append(el("small", event.source === "sim" ? "模拟注入" : "历史回放"));
    row.append(time, el("td", event.id, event.source === "sim" ? "sim" : ""), el("td", labels[event.kind] || event.kind), sideCell(event.side), el("td", event.price ? fmt(event.price) : "—"), el("td", fmt(event.volume)), el("td", event.note, "note-cell"));
    return row;
  }), 7);
  fillTable("trade-table", data.trades.map((trade) => {
    const row = changedRow(trade.version);
    row.append(el("td", trade.time), sideCell(trade.side), el("td", fmt(trade.price)), el("td", fmt(trade.volume)), el("td", trade.simulated ? "含模拟" : "历史", trade.simulated ? "sim" : ""));
    const cell = el("td");
    const button = el("button", "详情", "mini");
    button.addEventListener("click", () => tradeDetail(trade));
    cell.append(button);
    row.append(cell);
    return row;
  }), 6);
  renderOrders("buy-table", data.buys);
  renderOrders("sell-table", data.sells);
  fillTable("plans-table", data.plans.map((plan) => {
    const row = el("tr");
    row.append(el("td", plan.id, "sim"), el("td", labels[plan.mode]), sideCell(plan.side));
    for (const key of ["total", "filled", "resting", "waiting", "canceled"]) row.append(el("td", fmt(plan[key])));
    row.append(el("td", labels[plan.status]));
    const action = el("td");
    if (plan.status === "ACTIVE" || plan.resting) action.append(cancelButton(plan.id));
    row.append(action);
    return row;
  }), 10);
  $("#scheduled-list").replaceChildren(...data.scheduled.map((order) => {
    const item = el("div", undefined, "scheduled-item");
    item.append(el("span", `${order.time} · ${order.id} · ${fmt(order.volume)}`), cancelButton(order.parent || order.id));
    return item;
  }));
  if (!data.scheduled.length) $("#scheduled-list").append(el("span", "暂无待提交子单", "muted"));
  renderedVersion = data.version;
}
$("#source-form").addEventListener("submit", (event) => {
  event.preventDefault();
  message("正在加载单证券历史切片并校验事件编码…");
  perform("session", "POST", {
    file: $("#source-file").value, symbol: $("#source-symbol").value,
    start: timeValue("#source-start"), end: timeValue("#source-end"),
    mapping_confirmed: $("#mapping-confirmed").checked,
  }, "已从空簿加载历史切片。可单步、连续回放或先注入模拟挂单。");
});
$("#step").addEventListener("click", () => perform("control", "POST", {action: "step"}));
$("#play").addEventListener("click", () => perform("control", "POST", {action: snapshot.running ? "pause" : "play", speed: Number($("#speed").value)}));
$("#speed").addEventListener("change", () => {
  if (snapshot?.running) perform("control", "POST", {action: "play", speed: Number($("#speed").value)});
});
$("#inject-mode").addEventListener("change", () => {
  const mode = $("#inject-mode").value;
  $("#clip-label").hidden = mode === "single";
  $("#interval-label").hidden = mode !== "split";
  $("#inject-clip").required = mode !== "single";
  $("#inject-interval").required = mode === "split";
  $("#strategy-help").textContent = mode === "split" ? "按模拟时间间隔提交固定数量的子单，最后一单取剩余量；各子单独立排队。" : mode === "iceberg" ? "只提交一笔展示量；该子单全部成交后再补下一笔。同价补单重新排队，不保留原优先级。" : "在当前模拟时钟提交限价单。可挂在对手盘侧，或用可成交价格观察扫档。";
});
$("#inject-form").addEventListener("submit", (event) => {
  event.preventDefault();
  perform("orders", "POST", {
    side: $("#inject-side").value, mode: $("#inject-mode").value,
    price: $("#inject-price").value, volume: $("#inject-volume").value,
    delay_ms: Number($("#inject-delay").value), clip: $("#inject-clip").value,
    interval_ms: $("#inject-interval").value,
  });
});
$("#close-trade").addEventListener("click", () => $("#trade-detail").close());

async function initialize() {
  busy = true;
  controls();
  renderDepth({bids: [], asks: [], last_price: null});
  for (const [id, width] of [["event-table", 7], ["trade-table", 6], ["buy-table", 8], ["sell-table", 8], ["plans-table", 10]]) fillTable(id, [], width);
  try {
    const response = await fetch("/api/files");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "目录读取失败");
    const files = data.files.filter((file) => file.kind === "ord");
    for (const file of files) $("#source-file").append(new Option(`${file.name} · ${fmt(file.total)} 条`, file.name));
    if (files.length) $("#source-file").value = files[0].name;
  } catch (error) {
    message(`真实文件目录读取失败：${error.message}。仍可主动选择内置演示。`, true);
  } finally {
    busy = false;
    controls();
  }
}
initialize();
