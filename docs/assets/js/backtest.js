/*
 * Backtest page: filter bar, KPIs, strategy comparison, trade list (keyboard navigable) and the
 * audit view of one trade. Talks only to /api/backtest/*; builds DOM with textContent only.
 */

import { ApiError, getJson, isAbort } from "./api.js";
import { TradeCharts } from "./charts.js";
import { byId, el, fill, isTyping, show, toast } from "./dom.js";
import {
  DAY_KEYS,
  DAY_LABELS,
  DEFAULT_FILTERS,
  MISSING,
  clock,
  countActiveFilters,
  describeDays,
  directionLabel,
  exitReasonLabel,
  factor,
  fromQuery,
  modeShort,
  moveSelection,
  normalizeDays,
  ordinal,
  outcomeLabel,
  percent,
  plain,
  prettySymbol,
  rupees,
  shortDate,
  toApiQuery,
  toggleDay,
  toQuery,
  tone,
} from "./format.js";

// Arrow keys (and j/k) move the selection from anywhere on the page. Home/End/PageUp/PageDown
// only do so while the list itself has focus, so the page stays scrollable by keyboard.
const GLOBAL_KEYS = new Set(["ArrowDown", "ArrowUp", "j", "k"]);
const LIST_KEYS = new Set(["Home", "End", "PageDown", "PageUp"]);
const DETAIL_DELAY_MS = 70; // lets a held arrow key fly past rows without a request per row
const OVERLAY_DELAY_MS = 180; // avoids a flash of "loading" when the answer is already cached
const CROSS_CHOICES = 8;

const SKIP_REASONS = {
  no_expiry: "No listed expiry on or after the trade date",
  no_option_data: "No option candle near the fill time",
  bad_price: "Corrupt option price (below the \u20b90.05 tick)",
  no_spot_data: "No NIFTY candle near the fill time",
};

// ------------------------------------------------------------------------------- state

const ui = {
  meta: null,
  filters: { ...DEFAULT_FILTERS },
  data: null, // last /trades answer
  rows: [], // = data.trades
  rowNodes: [],
  selected: -1, // index into rows
  selectedId: null, // trade id, so the selection survives a filter change
  charts: null,
  loadingTrades: null, // AbortController
  loadingDetail: null, // AbortController
  detailTimer: 0,
  overlayTimer: 0,
  detailCache: new Map(),
  hashId: null,
  segments: {},
};

// ----------------------------------------------------------------------------- helpers

function ordered(values) {
  return values.map(String);
}

function showBanner(error, retry) {
  const message = error instanceof ApiError || error instanceof Error ? error.message : String(error);
  byId("banner-text").textContent = message;
  const button = byId("btn-retry");
  button.hidden = !retry;
  button.onclick = retry ?? null;
  show(byId("banner"), true);
}

function hideBanner() {
  show(byId("banner"), false);
}

function setValue(card, text, toneClass = "") {
  const value = card.querySelector("[data-value]");
  value.textContent = text;
  value.className = `kpi__value ${toneClass}`.trim();
}

function setSub(card, text) {
  card.querySelector("[data-sub]").textContent = text;
}

function badge(text, kind, title) {
  return el("span", { class: `badge badge--${kind}`, title }, text);
}

// -------------------------------------------------------------------------- filter controls

/** Radio-style button group with arrow-key support. Returns { set(value) }. */
function segmented(container, options, onPick) {
  const buttons = options.map(({ value, label }) =>
    el("button", { class: "seg__btn", type: "button", role: "radio", "aria-checked": "false", tabindex: "-1", "data-value": value }, label),
  );
  fill(container, buttons);

  function set(value) {
    for (const button of buttons) {
      const on = button.dataset.value === String(value);
      button.setAttribute("aria-checked", on ? "true" : "false");
      button.tabIndex = on ? 0 : -1;
    }
    if (!buttons.some((b) => b.tabIndex === 0)) buttons[0].tabIndex = 0;
  }

  container.addEventListener("click", (event) => {
    const button = event.target.closest(".seg__btn");
    if (button) onPick(button.dataset.value);
  });
  container.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const at = buttons.findIndex((b) => b === document.activeElement);
    if (at < 0) return;
    event.preventDefault();
    const next = buttons[(at + (event.key === "ArrowRight" ? 1 : buttons.length - 1)) % buttons.length];
    next.focus();
    onPick(next.dataset.value);
  });
  return { set };
}

/** Independent on/off buttons, one per weekday. Returns { set(value) }. */
function dayToggles(container, onToggle) {
  const buttons = DAY_KEYS.map((key) =>
    el("button", { class: "seg__btn", type: "button", role: "checkbox", "aria-checked": "true", "data-day": key }, DAY_LABELS[key]),
  );
  fill(container, buttons);
  container.addEventListener("click", (event) => {
    const button = event.target.closest(".seg__btn");
    if (button) onToggle(button.dataset.day);
  });
  function set(value) {
    const on = new Set(normalizeDays(value).split(","));
    for (const button of buttons) button.setAttribute("aria-checked", on.has(button.dataset.day) ? "true" : "false");
  }
  return { set };
}

function optionList(select, items, current) {
  fill(
    select,
    items.map(({ value, label }) => el("option", { value }, label)),
  );
  select.value = current;
}

function buildControls() {
  const { meta } = ui;
  const pick = (key) => (value) => update({ [key]: value });

  ui.segments.timeframe = segmented(
    byId("seg-timeframe"),
    meta.timeframes.map((tf) => ({ value: String(tf), label: `${tf} min` })),
    pick("timeframe"),
  );
  ui.segments.mode = segmented(
    byId("seg-mode"),
    [{ value: "all", label: "All" }, ...meta.modes.map((m) => ({ value: m.value, label: modeShort(m.value) }))],
    pick("mode"),
  );
  ui.segments.outcome = segmented(
    byId("seg-outcome"),
    [
      { value: "all", label: "All" },
      { value: "win", label: "Wins" },
      { value: "loss", label: "Losses" },
      { value: "tie", label: "Even" },
    ],
    pick("outcome"),
  );

  ui.segments.days = dayToggles(byId("seg-days"), (key) => {
    const next = toggleDay(ui.filters.days, key);
    if (next === null) {
      toast("Keep at least one weekday selected.");
      return;
    }
    update({ days: next });
  });

  optionList(byId("sel-start"), meta.starts.map((t) => ({ value: t, label: t })), ui.filters.start);
  optionList(byId("sel-cutoff"), meta.cutoffs.map((t) => ({ value: t, label: t })), ui.filters.cutoff);
  const crossItems = [{ value: "all", label: "Any" }];
  for (let n = 1; n <= CROSS_CHOICES; n += 1) crossItems.push({ value: String(n), label: `${ordinal(n)} cross` });
  optionList(byId("sel-cross"), crossItems, ui.filters.cross);

  for (const [id, key] of [
    ["sel-start", "start"],
    ["sel-cutoff", "cutoff"],
    ["sel-cross", "cross"],
    ["in-from", "from"],
    ["in-to", "to"],
  ]) {
    const node = byId(id);
    node.addEventListener("change", () => {
      update({ [key]: node.value });
      node.blur(); // hand the arrow keys back to the trade list
    });
  }
  byId("btn-reset").addEventListener("click", () => update({ ...DEFAULT_FILTERS }));
}

function syncControls() {
  const f = ui.filters;
  ui.segments.timeframe.set(f.timeframe);
  ui.segments.mode.set(f.mode);
  ui.segments.outcome.set(f.outcome);
  ui.segments.days.set(f.days);
  byId("sel-start").value = f.start;
  byId("sel-cutoff").value = f.cutoff;
  byId("sel-cross").value = f.cross;
  byId("in-from").value = f.from;
  byId("in-to").value = f.to;
  const active = countActiveFilters(f);
  const count = byId("filter-count");
  count.textContent = String(active);
  count.hidden = active === 0;
  byId("btn-reset").disabled = active === 0;
}

function writeUrl() {
  const query = toQuery(ui.filters);
  const row = ui.rows[ui.selected];
  const hash = row ? `#t=${row.id}` : "";
  history.replaceState(null, "", `${location.pathname}${query ? `?${query}` : ""}${hash}`);
}

/** Change one or more filters, then reload. Impossible combinations are corrected, not sent. */
function update(changes) {
  const next = { ...ui.filters, ...changes };
  if (next.from && next.to && next.from > next.to) {
    // keep the one the user just touched, and move the other
    if ("from" in changes) next.to = next.from;
    else next.from = next.to;
    toast("The date range was adjusted so it does not end before it starts.");
  }
  ui.filters = next;
  syncControls();
  loadTrades();
}

// ------------------------------------------------------------------------------ loading

function setBusy(busy) {
  byId("kpis").setAttribute("aria-busy", String(busy));
  byId("app").classList.toggle("is-loading", busy);
}

async function loadTrades() {
  ui.loadingTrades?.abort();
  const controller = new AbortController();
  ui.loadingTrades = controller;
  hideBanner();
  setBusy(true);
  try {
    const data = await getJson(`/api/backtest/trades?${toApiQuery(ui.filters)}`, { signal: controller.signal });
    ui.loadingTrades = null;
    ui.data = data;
    renderResults();
  } catch (error) {
    if (isAbort(error)) return;
    ui.loadingTrades = null;
    if (error.code === "no_data") {
      show(byId("app"), false);
      show(byId("no-data"), true);
      return;
    }
    showBanner(error, loadTrades);
  } finally {
    if (ui.loadingTrades === null || ui.loadingTrades === controller) setBusy(false);
  }
}

// ---------------------------------------------------------------------------- rendering

function renderResults() {
  const { data } = ui;
  ui.rows = data.trades;
  const dateFrom = byId("in-from");
  const dateTo = byId("in-to");
  for (const input of [dateFrom, dateTo]) {
    input.min = data.range.first ?? "";
    input.max = data.range.last ?? "";
  }
  renderSummary();
  renderKpis();
  renderCompare();
  renderEquity();
  renderList();

  const keep = ui.rows.findIndex((row) => row.id === (ui.selectedId ?? ui.hashId));
  ui.hashId = null;
  selectIndex(keep >= 0 ? keep : ui.rows.length ? 0 : -1, { immediate: true, scroll: true });
  if (byId("skips").open) loadSkips();
}

function renderSummary() {
  const { data, filters } = ui;
  const n = data.kpis.trades;
  const span = data.range.first ? `${shortDate(data.range.first)} \u2013 ${shortDate(data.range.last)}` : "";
  const origin = data.computed.from_cache ? "saved results" : "freshly computed";
  const days = describeDays(filters.days);
  const parts =
    n === 0
      ? ["No trades match these filters", `NIFTY ${filters.timeframe}-minute`, span, days]
      : [`${plain(n)} trade${n === 1 ? "" : "s"}`, `NIFTY ${filters.timeframe}-minute signals`, span, days, origin];
  byId("summary").textContent = parts.filter(Boolean).join(" \u00b7 ");
}

function renderKpis() {
  const k = ui.data.kpis;
  const none = k.trades === 0;

  setValue(byId("kpi-trades"), plain(k.trades));
  setSub(byId("kpi-trades"), none ? "No trades" : `${k.wins} won \u00b7 ${k.losses} lost${k.ties ? ` \u00b7 ${k.ties} even` : ""}`);

  setValue(byId("kpi-winrate"), none ? MISSING : percent(k.win_rate));
  setSub(byId("kpi-winrate"), none ? "\u00a0" : `Avg win ${rupees(k.avg_win)} \u00b7 avg loss ${rupees(k.avg_loss)}`);

  setValue(byId("kpi-net"), none ? MISSING : rupees(k.net_pnl, { sign: true }), tone(k.net_pnl));
  setSub(byId("kpi-net"), none ? "\u00a0" : `${rupees(k.expectancy, { sign: true })} per trade (expectancy)`);

  setValue(byId("kpi-gross"), none ? MISSING : rupees(k.gross_pnl, { sign: true }), tone(k.gross_pnl));
  setSub(byId("kpi-gross"), none ? "\u00a0" : `Slippage ${rupees(k.slippage)} \u00b7 charges ${rupees(k.charges)}`);

  setValue(byId("kpi-pf"), none ? MISSING : factor(k.profit_factor, k.profit_factor_infinite));
  setSub(byId("kpi-pf"), none ? "\u00a0" : `Payoff ratio ${factor(k.payoff_ratio)}`);

  const stats = [
    ["Max drawdown", none ? MISSING : `${rupees(k.max_drawdown)}${k.max_drawdown_pct == null ? "" : ` (${k.max_drawdown_pct.toFixed(1)}%)`}`],
    ["Best trade", none ? MISSING : rupees(k.best_trade, { sign: true })],
    ["Worst trade", none ? MISSING : rupees(k.worst_trade, { sign: true })],
    ["Longest losing streak", none ? MISSING : String(k.max_consecutive_losses)],
  ];
  fill(
    byId("more-stats"),
    stats.map(([label, value]) => el("div", { class: "stats__item" }, el("dt", {}, label), el("dd", {}, value))),
  );
}

function renderSelectedKpi() {
  const card = byId("kpi-selected");
  const row = ui.rows[ui.selected];
  if (!row) {
    setValue(card, MISSING);
    setSub(card, ui.rows.length ? "Select a trade" : "No trade selected");
    return;
  }
  setValue(card, rupees(row.net, { sign: true, decimals: 2 }), tone(row.net));
  setSub(card, `${shortDate(row.date, false)} \u00b7 ${row.entry} \u2192 ${row.exit} \u00b7 ${modeShort(row.mode)}`);
}

const COMPARE_ROWS = [
  ["Trades", (m) => plain(m.trades), null],
  ["Win rate", (m) => (m.trades ? percent(m.win_rate) : MISSING), null],
  ["Net P&L", (m) => (m.trades ? rupees(m.net_pnl, { sign: true }) : MISSING), (m) => tone(m.net_pnl)],
  ["Gross P&L", (m) => (m.trades ? rupees(m.gross_pnl, { sign: true }) : MISSING), (m) => tone(m.gross_pnl)],
  ["Profit factor", (m) => (m.trades ? factor(m.profit_factor, m.profit_factor_infinite) : MISSING), null],
  ["Payoff ratio", (m) => (m.trades ? factor(m.payoff_ratio) : MISSING), null],
  ["Expectancy", (m) => (m.trades ? rupees(m.expectancy, { sign: true }) : MISSING), (m) => tone(m.expectancy)],
  ["Max drawdown", (m) => (m.trades ? rupees(m.max_drawdown) : MISSING), null],
];

function renderCompare() {
  const { compare } = ui.data;
  const modes = ui.meta.modes;
  const best = modes.reduce(
    (top, m) => (compare[m.value].trades && (top === null || compare[m.value].net_pnl > compare[top].net_pnl) ? m.value : top),
    null,
  );
  const activeMode = ui.filters.mode;
  const columnClass = (value) => `num${activeMode === value ? " is-active" : ""}`;

  const head = el(
    "tr",
    {},
    el("th", { scope: "col" }, "Metric"),
    modes.map((m) =>
      el(
        "th",
        { scope: "col", class: columnClass(m.value) },
        el(
          "button",
          {
            class: "linkish",
            type: "button",
            "data-mode": m.value,
            "aria-pressed": String(activeMode === m.value),
            title: `Show only ${m.label}`,
          },
          m.label,
          best === m.value ? el("span", { class: "best", title: "Highest net P&L" }, "\u2605") : null,
        ),
      ),
    ),
  );
  const body = COMPARE_ROWS.map(([label, format, toneOf]) =>
    el(
      "tr",
      {},
      el("th", { scope: "row" }, label),
      modes.map((m) => el("td", { class: `${columnClass(m.value)} ${toneOf ? toneOf(compare[m.value]) : ""}`.trim() }, format(compare[m.value]))),
    ),
  );
  fill(byId("compare"), el("thead", {}, head), el("tbody", {}, body));
}

function renderEquity() {
  const points = ui.charts.setEquity(ui.data.equity);
  const note = byId("equity-note");
  const last = ui.data.equity.at(-1);
  note.textContent = points ? `ends ${rupees(last[1], { sign: true })} after ${plain(ui.data.kpis.trades)} trades` : "no trades";
}

// -------------------------------------------------------------------------- trade list

function skeletonRows() {
  return Array.from({ length: 8 }, () => el("div", { class: "trade-row trade-row--skeleton", "aria-hidden": "true" }));
}

function tradeRow(row, index) {
  const bullish = row.direction === "bullish";
  return el(
    "div",
    {
      class: `trade-row is-${row.outcome}`,
      role: "option",
      id: `trade-${row.id}`,
      "data-index": index,
      "aria-selected": "false",
    },
    el("span", { class: `trade-row__dir ${bullish ? "pos" : "neg"}`, title: directionLabel(row.direction) }, bullish ? "\u25b2" : "\u25bc"),
    el(
      "span",
      { class: "trade-row__main" },
      el("span", { class: "trade-row__title" }, shortDate(row.date), el("span", { class: "trade-row__no", title: "Crossover of the day, within the entry window" }, `#${row.entry_no}`)),
      el(
        "span",
        { class: "trade-row__sub" },
        `${row.entry} \u2192 ${row.exit} \u00b7 ${row.option} \u00b7 ${modeShort(row.mode)}`,
        row.whipsaw ? el("span", { class: "trade-row__flag", title: "Whipsaw: exited within 2 signal bars" }, "whipsaw") : null,
      ),
    ),
    el("span", { class: `trade-row__pnl ${tone(row.net)}` }, rupees(row.net, { sign: true })),
  );
}

function renderList() {
  const list = byId("trade-list");
  byId("list-count").textContent = `${plain(ui.rows.length)} trade${ui.rows.length === 1 ? "" : "s"}`;
  ui.selected = -1;
  if (!ui.rows.length) {
    ui.rowNodes = [];
    fill(
      list,
      el(
        "div",
        { class: "empty-state" },
        el("p", { class: "empty-state__title" }, "No trades match these filters"),
        el("p", { class: "empty-state__text" }, "Try a wider entry window, another timeframe or a longer date range."),
        el("button", { class: "btn", type: "button", "data-action": "reset" }, "Reset filters"),
      ),
    );
    return;
  }
  const fragment = document.createDocumentFragment();
  ui.rowNodes = ui.rows.map((row, index) => {
    const node = tradeRow(row, index);
    fragment.append(node);
    return node;
  });
  list.replaceChildren(fragment);
  list.scrollTop = 0;
}

/**
 * Scroll only the list (never the page) just far enough to show a row. Element.scrollIntoView
 * would also scroll the whole page, which jumps the layout when the list is partly off-screen.
 */
function keepVisible(list, node) {
  const box = list.getBoundingClientRect();
  const row = node.getBoundingClientRect();
  const margin = 6;
  if (row.top < box.top) list.scrollTop -= box.top - row.top + margin;
  else if (row.bottom > box.bottom) list.scrollTop += row.bottom - box.bottom + margin;
}

function selectIndex(index, { immediate = false, scroll = false } = {}) {
  const previous = ui.rowNodes[ui.selected];
  previous?.setAttribute("aria-selected", "false");
  previous?.classList.remove("is-selected");
  ui.selected = index;
  ui.selectedId = ui.rows[index]?.id ?? null;
  const node = ui.rowNodes[index];
  if (node) {
    node.setAttribute("aria-selected", "true");
    node.classList.add("is-selected");
    byId("trade-list").setAttribute("aria-activedescendant", node.id);
    if (scroll) keepVisible(byId("trade-list"), node);
  } else {
    byId("trade-list").removeAttribute("aria-activedescendant");
  }
  renderSelectedKpi();
  writeUrl();
  scheduleDetail(immediate);
}

// --------------------------------------------------------------------------- trade audit

function detailUrl(row) {
  const query = new URLSearchParams(toApiQuery(ui.filters));
  query.delete("timeframe");
  query.set("run", ui.data.run);
  return `/api/backtest/trade/${ui.filters.timeframe}/${row.id}?${query}`;
}

function scheduleDetail(immediate) {
  window.clearTimeout(ui.detailTimer);
  ui.loadingDetail?.abort();
  const row = ui.rows[ui.selected];
  if (!row) {
    clearViewer();
    return;
  }
  const cached = ui.detailCache.get(detailUrl(row));
  if (cached) {
    renderDetail(cached);
    prefetchNeighbour();
    return;
  }
  ui.detailTimer = window.setTimeout(() => loadDetail(row), immediate ? 0 : DETAIL_DELAY_MS);
}

function setOverlay(kind, message, retry) {
  const overlay = byId("viewer-overlay");
  window.clearTimeout(ui.overlayTimer);
  if (!kind) {
    overlay.hidden = true;
    byId("viewer").setAttribute("aria-busy", "false");
    return;
  }
  overlay.className = `overlay overlay--${kind}`;
  fill(
    overlay,
    el("p", { class: "overlay__text" }, message),
    retry ? el("button", { class: "btn", type: "button", "data-action": "retry-detail" }, "Try again") : null,
  );
  overlay.hidden = false;
  byId("viewer").setAttribute("aria-busy", String(kind === "loading"));
}

async function fetchDetail(row, signal) {
  const url = detailUrl(row);
  const detail = await getJson(url, { signal });
  ui.detailCache.set(url, detail);
  if (ui.detailCache.size > 200) ui.detailCache.delete(ui.detailCache.keys().next().value);
  return detail;
}

async function loadDetail(row) {
  const controller = new AbortController();
  ui.loadingDetail = controller;
  ui.overlayTimer = window.setTimeout(() => setOverlay("loading", "Loading trade\u2026"), OVERLAY_DELAY_MS);
  try {
    const detail = await fetchDetail(row, controller.signal);
    if (ui.rows[ui.selected]?.id !== row.id) return;
    renderDetail(detail);
    prefetchNeighbour();
  } catch (error) {
    if (isAbort(error)) return;
    window.clearTimeout(ui.overlayTimer);
    if (error.code === "results_changed") {
      toast("The results were refreshed. Reloading the list.");
      ui.detailCache.clear();
      loadTrades();
      return;
    }
    setOverlay("error", error.message, true);
  }
}

/** Quietly fetch the trades either side so stepping through the list feels instant. */
function prefetchNeighbour() {
  for (const neighbour of [ui.rows[ui.selected + 1], ui.rows[ui.selected - 1]]) {
    if (!neighbour || ui.detailCache.has(detailUrl(neighbour))) continue;
    fetchDetail(neighbour).catch(() => {
      // a failed prefetch is not worth telling anyone about; the real request will report it
    });
  }
}

function clearViewer() {
  ui.charts.clear();
  fill(byId("meta-bar"), el("p", { class: "metabar__empty" }, ui.rows.length ? "Select a trade to inspect it." : "Nothing to show for these filters."));
  fill(byId("cost-card"), el("p", { class: "empty" }, MISSING));
  fill(byId("facts"), el("p", { class: "empty" }, MISSING));
  byId("costs-note").textContent = "";
  setOverlay("empty", ui.rows.length ? "Select a trade from the list." : "No trade to show.");
}

function renderDetail(detail) {
  setOverlay(null);
  const { trade } = detail;
  renderMetaBar(trade);
  byId("spot-title").textContent = `NIFTY 50 \u00b7 ${trade.timeframe}-minute signal bars \u00b7 ${shortDate(trade.date)}`;
  byId("option-title").textContent = `${prettySymbol(trade.symbol)} \u00b7 1-minute`;
  ui.charts.showTrade(detail);
  renderCosts(trade);
  renderFacts(trade);
}

function metaItem(label, ...value) {
  return el("div", { class: "metabar__item" }, el("span", { class: "metabar__label" }, label), el("span", { class: "metabar__value" }, ...value));
}

function renderMetaBar(trade) {
  const bullish = trade.direction === "bullish";
  const verb = trade.side === "BUY" ? "Buy" : "Sell";
  const spotMove = trade.spot_exit - trade.spot_entry;
  const flags = [];
  if (trade.whipsaw) flags.push(badge("Whipsaw", "warn", "Closed by the opposite crossover within 2 signal bars of entry"));
  if (trade.stale_exit) flags.push(badge("Stale exit price", "warn", "No option candle at the exit time: the last earlier candle's close was used"));
  if (!trade.cost_verified) flags.push(badge("Unverified cost rates", "warn", "The charge rates for this date are best-effort estimates"));
  if (trade.outcome === "tie") flags.push(badge("Break-even", "muted", "Net P&L is exactly zero"));

  fill(
    byId("meta-bar"),
    el(
      "div",
      { class: "metabar__row" },
      el("div", { class: "metabar__item metabar__item--wide" }, el("span", { class: "metabar__label" }, trade.mode_label), el("span", { class: "metabar__symbol", title: trade.symbol }, trade.symbol)),
      metaItem("Direction", el("span", { class: bullish ? "pos" : "neg" }, `${bullish ? "\u25b2" : "\u25bc"} ${directionLabel(trade.direction)}`), ` \u2192 ${verb} ${trade.option_type}`),
      metaItem("NIFTY", `${plain(trade.spot_entry, 2)} \u2192 ${plain(trade.spot_exit, 2)} `, el("span", { class: tone(spotMove) }, `(${spotMove >= 0 ? "+" : "\u2212"}${plain(Math.abs(spotMove), 2)})`)),
      metaItem("Option fill", `${plain(trade.option_entry_fill, 2)} \u2192 ${plain(trade.option_exit_fill, 2)}`),
      metaItem("Expiry", `${shortDate(trade.expiry)} \u00b7 `, trade.dte === 0 ? "expiry day" : `${trade.dte} DTE`),
      metaItem("Exit", exitReasonLabel(trade.exit_reason)),
      el("div", { class: "metabar__item metabar__item--pnl" }, el("span", { class: "metabar__label" }, "Net P&L"), el("span", { class: `metabar__pnl ${tone(trade.costs.net)}` }, rupees(trade.costs.net, { sign: true, decimals: 2 }))),
    ),
    flags.length ? el("div", { class: "metabar__flags" }, flags) : null,
  );
}

function pct(value) {
  return `${Number(value.toPrecision(4))}%`;
}

function renderCosts(trade) {
  const { costs, rates } = trade;
  // Charges are stored as positive amounts and shown as deductions.
  const line = (label, amount, rate) =>
    el(
      "tr",
      {},
      el("th", { scope: "row" }, label, el("span", { class: "sub" }, rate)),
      el("td", { class: "num" }, `${amount ? "\u2212" : ""}${rupees(Math.abs(amount), { decimals: 2 })}`),
    );
  const grossLine = el(
    "tr",
    { class: "cost--strong" },
    el("th", { scope: "row" }, "Gross P&L", el("span", { class: "sub" }, "from the raw candle prices")),
    el("td", { class: `num ${tone(costs.gross)}` }, rupees(costs.gross, { sign: true, decimals: 2 })),
  );
  const table = el(
    "table",
    { class: "table table--costs" },
    el(
      "tbody",
      {},
      grossLine,
      line("Slippage", costs.slippage, "0.5% of premium + 0.5 points per leg"),
      line("Brokerage", costs.brokerage, `\u20b9${rates.brokerage_per_order} per order`),
      line("STT", costs.stt, `${pct(rates.stt_sell_pct)} of premium, sell side`),
      line("Exchange charges", costs.exchange, `${pct(rates.exchange_pct)} of premium`),
      line("SEBI fee", costs.sebi, `\u20b9${rates.sebi_per_crore} per crore`),
      line("Stamp duty", costs.stamp, `${pct(rates.stamp_buy_pct)} of premium, buy side`),
      line("GST", costs.gst, `${pct(rates.gst_pct)} on brokerage, exchange, SEBI`),
      el(
        "tr",
        { class: "cost--total" },
        el("th", { scope: "row" }, "Net P&L"),
        el("td", { class: `num ${tone(costs.net)}` }, rupees(costs.net, { sign: true, decimals: 2 })),
      ),
    ),
  );
  fill(byId("cost-card"), table, el("p", { class: "cost__note" }, `Charges total ${rupees(costs.charges_total, { decimals: 2 })}; slippage ${rupees(costs.slippage, { decimals: 2 })}.`));
  byId("costs-note").textContent = `rates from ${shortDate(rates.effective_from)} \u00b7 ${rates.verified ? "verified" : "UNVERIFIED"}`;
  byId("costs-note").className = `card__meta ${rates.verified ? "" : "warn"}`.trim();
}

function renderFacts(trade) {
  const fact = (label, value) => el("div", { class: "kv__row" }, el("dt", {}, label), el("dd", {}, value));
  fill(
    byId("facts"),
    el(
      "dl",
      { class: "kv" },
      fact("Signal", `${ordinal(trade.signal_no)} crossover of the day`),
      fact("Entry number in window", `#${trade.entry_no}`),
      fact("Entry decided", `${shortDate(trade.entry_decision, false)} ${clock(trade.entry_decision)} (bar close)`),
      fact("Entry filled", `${clock(trade.entry_fill)} at ${plain(trade.option_entry_raw, 2)} \u2192 ${plain(trade.option_entry_fill, 2)} after slippage`),
      fact("Exit decided", `${clock(trade.exit_decision)} \u00b7 ${exitReasonLabel(trade.exit_reason)}`),
      fact("Exit filled", `${clock(trade.exit_fill)} at ${plain(trade.option_exit_raw, 2)} \u2192 ${plain(trade.option_exit_fill, 2)} after slippage`),
      fact("Quantity", `${plain(trade.quantity)} (${trade.side.toLowerCase()})`),
      fact("Outcome", outcomeLabel(trade.outcome)),
    ),
  );
}

// ---------------------------------------------------------------------- skipped signals

async function loadSkips() {
  const body = byId("skips-body");
  fill(body, el("p", { class: "empty" }, "Loading\u2026"));
  const query = new URLSearchParams({ timeframe: ui.filters.timeframe });
  if (ui.filters.from) query.set("from", ui.filters.from);
  if (ui.filters.to) query.set("to", ui.filters.to);
  try {
    const data = await getJson(`/api/backtest/skips?${query}`);
    renderSkips(data);
  } catch (error) {
    if (!isAbort(error)) fill(body, el("p", { class: "error" }, error.message));
  }
}

function renderSkips(data) {
  const body = byId("skips-body");
  const total = data.skipped_trades.length;
  const incomplete = data.incomplete_days.length;
  byId("skips-count").textContent = `${total} signal${total === 1 ? "" : "s"} \u00b7 ${incomplete} day${incomplete === 1 ? "" : "s"}`;
  const parts = [];

  const reasons = Object.entries(data.skip_reasons);
  parts.push(
    el("h3", { class: "subhead" }, "Signals that could not become trades"),
    reasons.length
      ? el("ul", { class: "plainlist" }, reasons.map(([reason, n]) => el("li", {}, el("strong", {}, String(n)), ` \u00d7 ${SKIP_REASONS[reason] ?? reason}`)))
      : el("p", { class: "empty" }, "None: every signal in this range had usable option prices."),
  );
  if (total) {
    const shown = data.skipped_trades.slice(0, 300);
    parts.push(
      el(
        "div",
        { class: "table-wrap skips__table" },
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {}, ["Date", "Time", "Mode", "Reason", "Detail"].map((h) => el("th", { scope: "col" }, h)))),
          el(
            "tbody",
            {},
            shown.map((s) =>
              el("tr", {}, el("td", {}, shortDate(s.date)), el("td", {}, s.decision), el("td", {}, modeShort(s.mode)), el("td", {}, SKIP_REASONS[s.reason] ?? s.reason), el("td", {}, s.detail ?? "")),
            ),
          ),
        ),
      ),
      total > shown.length ? el("p", { class: "empty" }, `Showing the first ${shown.length} of ${total}.`) : null,
    );
  }

  const days = Object.entries(data.day_counts);
  parts.push(
    el("h3", { class: "subhead" }, "Days"),
    days.length
      ? el("ul", { class: "plainlist" }, days.map(([kind, n]) => el("li", {}, el("strong", {}, String(n)), ` \u00d7 ${kind.replaceAll("_", " ")}`)))
      : el("p", { class: "empty" }, "No day notes."),
  );
  if (incomplete) {
    parts.push(
      el("p", { class: "empty" }, "Days with an incomplete NIFTY session are excluded, never patched:"),
      el("ul", { class: "plainlist" }, data.incomplete_days.map((d) => el("li", {}, `${shortDate(d.date)} \u2014 ${d.detail}`))),
    );
  }
  fill(body, parts);
}

// ---------------------------------------------------------------------------- rules dialog

function renderRules() {
  const { meta } = ui;
  const s = meta.strategy;
  const settings = [
    ["EMAs", `${s.fast} / ${s.slow}`],
    ["Lot size", `${s.lot_size} \u00d7 ${s.lots} lot`],
    ["Square-off", s.square_off],
    ["Slippage", `${s.slippage_pct}% of premium + ${s.slippage_points} points per leg`],
    ["Engine version", meta.engine_version],
    ["Data", meta.data_range ? `NIFTY ${shortDate(meta.data_range[0])} \u2013 ${shortDate(meta.data_range[1])}` : MISSING],
  ];
  const schedule = el(
    "table",
    { class: "table" },
    el("thead", {}, el("tr", {}, ["Effective from", "STT (sell)", "Exchange", "Stamp (buy)", "Status"].map((h) => el("th", { scope: "col" }, h)))),
    el(
      "tbody",
      {},
      meta.cost_schedule.map((r) =>
        el(
          "tr",
          { title: r.source },
          el("td", {}, shortDate(r.effective_from)),
          el("td", {}, pct(r.stt_sell_pct)),
          el("td", {}, pct(r.exchange_pct)),
          el("td", {}, pct(r.stamp_buy_pct)),
          el("td", {}, r.verified ? badge("verified", "ok") : badge("unverified", "warn", "Best-effort estimate; check against a contract note")),
        ),
      ),
    ),
  );
  fill(
    byId("rules-body"),
    el("dl", { class: "kv" }, settings.map(([label, value]) => el("div", { class: "kv__row" }, el("dt", {}, label), el("dd", {}, value)))),
    meta.rules.map((rule) => el("section", { class: "rule" }, el("h3", { class: "rule__title" }, rule.title), el("p", { class: "rule__text" }, rule.text))),
    el("h3", { class: "rule__title" }, "Charge rates by date"),
    el("div", { class: "table-wrap" }, schedule),
  );
}

// ------------------------------------------------------------------------------- wiring

function wire() {
  const list = byId("trade-list");
  list.addEventListener("click", (event) => {
    const reset = event.target.closest("[data-action=reset]");
    if (reset) return update({ ...DEFAULT_FILTERS });
    const node = event.target.closest(".trade-row");
    if (node && !node.classList.contains("trade-row--skeleton")) {
      selectIndex(Number(node.dataset.index), { immediate: true });
      list.focus({ preventScroll: true });
      // Stacked layout (tablet/phone): the chart is below the list, so bring it into view.
      if (window.matchMedia("(max-width: 1050px)").matches) {
        const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        byId("viewer").scrollIntoView({ block: "start", behavior: reduced ? "auto" : "smooth" });
      }
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.defaultPrevented || event.metaKey || event.ctrlKey || event.altKey) return;
    if (isTyping(event.target) || byId("rules-dialog").open) return;
    const inList = event.target === byId("trade-list");
    if (!GLOBAL_KEYS.has(event.key) && !(inList && LIST_KEYS.has(event.key))) return;
    event.preventDefault(); // the page itself must not scroll while the list moves
    const next = moveSelection(ui.selected, ui.rows.length, event.key);
    if (next >= 0 && next !== ui.selected) selectIndex(next, { scroll: true });
  });

  byId("compare").addEventListener("click", (event) => {
    const button = event.target.closest("[data-mode]");
    if (button) update({ mode: ui.filters.mode === button.dataset.mode ? "all" : button.dataset.mode });
  });

  byId("viewer-overlay").addEventListener("click", (event) => {
    if (event.target.closest("[data-action=retry-detail]")) {
      const row = ui.rows[ui.selected];
      if (row) loadDetail(row);
    }
  });

  byId("skips").addEventListener("toggle", () => {
    if (byId("skips").open) loadSkips();
  });

  const dialog = byId("rules-dialog");
  byId("btn-rules").addEventListener("click", () => {
    if (ui.meta) dialog.showModal();
  });
  byId("btn-rules-close").addEventListener("click", () => dialog.close());
  dialog.addEventListener("click", (event) => {
    if (event.target === dialog) dialog.close(); // click on the backdrop
  });

  window.addEventListener("themechange", () => ui.charts?.applyTheme());
}

/** "Backtest data: <source> \u00b7 <caveat>": says where the candles came from. */
function renderSourceNote(source) {
  const note = byId("source-note");
  if (!source) {
    show(note, false);
    return;
  }
  const name = source.url
    ? el("a", { href: source.url, target: "_blank", rel: "noopener noreferrer" }, source.title)
    : source.title;
  fill(note, el("strong", {}, "Backtest data: "), name, source.note ? ` \u00b7 ${source.note}` : null);
  show(note, true);
}

async function init() {
  wire();
  try {
    ui.meta = await getJson("/api/backtest/meta");
  } catch (error) {
    showBanner(error, init);
    return;
  }
  show(byId("synthetic-banner"), ui.meta.synthetic);
  renderSourceNote(ui.meta.data_source);
  if (!ui.meta.has_data) {
    show(byId("no-data"), true);
    return;
  }
  hideBanner();

  const allowed = {
    timeframe: ordered(ui.meta.timeframes),
    mode: ["all", ...ui.meta.modes.map((m) => m.value)],
    outcome: ["all", "win", "loss", "tie"],
    start: ui.meta.starts,
    cutoff: ui.meta.cutoffs,
    cross: ["all", ...Array.from({ length: CROSS_CHOICES }, (_, i) => String(i + 1))],
  };
  ui.filters = fromQuery(location.search, allowed);
  const hash = /^#t=(\d+)$/.exec(location.hash);
  ui.hashId = hash ? Number(hash[1]) : null;

  show(byId("app"), true);
  try {
    ui.charts = new TradeCharts({
      spot: byId("chart-spot"),
      option: byId("chart-option"),
      equity: byId("chart-equity"),
      spotLegend: byId("spot-legend"),
      optionLegend: byId("option-legend"),
    });
  } catch (error) {
    showBanner(error);
    return;
  }
  buildControls();
  syncControls();
  renderRules();
  fill(byId("trade-list"), skeletonRows());
  clearViewer();
  setOverlay("loading", "Loading results\u2026");
  await loadTrades();
}

init();
