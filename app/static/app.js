"use strict";

/*
 * Strategy Desk: the Groww account overview.
 *
 * Talks only to this app's own /api/* endpoints. Everything that comes back is rendered with
 * textContent / createElement (never innerHTML), so no API value can ever inject markup.
 */
(() => {
  const $ = (selector, root = document) => root.querySelector(selector);

  const MISSING = "\u2014";
  const money = new Intl.NumberFormat("en-IN", { style: "currency", currency: "INR" });
  const quantity = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 4 });
  const dateTime = new Intl.DateTimeFormat("en-IN", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "Asia/Kolkata",
  });
  const timeOnly = new Intl.DateTimeFormat("en-IN", { timeStyle: "medium", timeZone: "Asia/Kolkata" });

  // ------------------------------------------------------------------ helpers

  /** Tiny DOM builder: el("td", { class: "num" }, "text", childNode, ...). */
  function el(tag, attrs = {}, ...children) {
    const node = document.createElement(tag);
    for (const [name, value] of Object.entries(attrs)) {
      if (value === false || value == null) continue;
      if (name === "class") node.className = value;
      else node.setAttribute(name, value === true ? "" : value);
    }
    node.append(...children.flat().filter((child) => child != null && child !== false));
    return node;
  }

  /** Numbers, or numeric strings, become numbers; anything else becomes null. */
  function toNumber(value) {
    if (typeof value === "string" && value.trim() !== "") value = Number(value);
    return typeof value === "number" && Number.isFinite(value) ? value : null;
  }

  const fmtMoney = (value) => (toNumber(value) === null ? MISSING : money.format(toNumber(value)));
  const fmtQty = (value) => (toNumber(value) === null ? MISSING : quantity.format(toNumber(value)));
  const tone = (value) => {
    const n = toNumber(value);
    return n ? (n > 0 ? "pos" : "neg") : "";
  };
  const sum = (rows, pick) => rows.reduce((total, row) => total + (toNumber(pick(row)) ?? 0), 0);

  class ApiError extends Error {
    constructor(message, code, status) {
      super(message);
      this.code = code;
      this.status = status;
    }
  }

  async function api(path, method = "GET") {
    let response;
    try {
      response = await fetch(path, {
        method,
        cache: "no-store",
        // The server rejects POSTs without this header, which blocks cross-site requests.
        headers: { "X-Requested-With": "fetch", Accept: "application/json" },
      });
    } catch {
      throw new ApiError("Cannot reach the local server. Is it still running?", "network", 0);
    }
    let body = null;
    try {
      body = await response.json();
    } catch {
      /* not JSON, e.g. a proxy error page */
    }
    if (!response.ok) {
      const error = body && body.error;
      throw new ApiError(
        (error && error.message) || `Request failed (HTTP ${response.status}).`,
        (error && error.code) || "http_error",
        response.status,
      );
    }
    return body;
  }

  // ------------------------------------------------------------------- cards

  const ui = {
    pill: $("#status-pill"),
    connect: $("#btn-connect"),
    refresh: $("#btn-refresh"),
    disconnect: $("#btn-disconnect"),
    banner: $("#banner"),
    meta: $("#meta"),
  };

  const cards = {
    profile: $("#card-profile"),
    funds: $("#card-funds"),
    holdings: $("#card-holdings"),
    positions: $("#card-positions"),
  };

  const PLACEHOLDER = "Connect to Groww to load this section.";

  const showBody = (card, ...nodes) => $("[data-body]", card).replaceChildren(...nodes);
  const setMeta = (card, text) => ($("[data-meta]", card).textContent = text ?? "");
  const setRaw = (card, data) => {
    $("[data-raw]", card).textContent = data == null ? "" : JSON.stringify(data, null, 2);
  };

  function showMessage(card, text, kind = "empty") {
    showBody(card, el("p", { class: kind }, text));
    setMeta(card, "");
    setRaw(card, null);
  }

  function resetCards() {
    for (const card of Object.values(cards)) showMessage(card, PLACEHOLDER);
  }

  function kvList(rows) {
    return el(
      "dl",
      { class: "kv" },
      rows.map((row) =>
        el("div", { class: "kv__row" }, el("dt", {}, row.label), el("dd", { class: row.tone ?? "" }, row.value)),
      ),
    );
  }

  const flag = (value) =>
    typeof value === "boolean"
      ? el("span", { class: `flag ${value ? "flag--on" : "flag--off"}` }, value ? "Enabled" : "Disabled")
      : MISSING;

  function table(columns, rows, emptyText) {
    if (!rows.length) return el("p", { class: "empty" }, emptyText);
    const head = el(
      "tr",
      {},
      columns.map((c) => el("th", { scope: "col", class: c.numeric ? "num" : "" }, c.label)),
    );
    const body = rows.map((row) => el("tr", {}, columns.map((c) => c.cell(row))));
    return el("div", { class: "table-wrap" }, el("table", { class: "table" }, el("thead", {}, head), el("tbody", {}, body)));
  }

  const numCell = (text, cls = "") => el("td", { class: `num ${cls}`.trim() }, text);
  const symbolCell = (symbol, sub) =>
    el("td", {}, el("span", { class: "symbol" }, symbol ?? MISSING), sub ? el("span", { class: "sub" }, sub) : null);

  // ---------------------------------------------------------------- renderers

  function renderProfile(profile) {
    const segments = Array.isArray(profile.active_segments) ? profile.active_segments : [];
    showBody(
      cards.profile,
      kvList([
        { label: "Client code (UCC)", value: el("span", { class: "mono" }, profile.ucc ?? MISSING) },
        { label: "User ID", value: el("span", { class: "mono" }, profile.vendor_user_id ?? MISSING) },
        { label: "NSE trading", value: flag(profile.nse_enabled) },
        { label: "BSE trading", value: flag(profile.bse_enabled) },
        { label: "DDPI", value: flag(profile.ddpi_enabled) },
        {
          label: "Active segments",
          value: segments.length
            ? el("span", { class: "chips" }, segments.map((segment) => el("span", { class: "chip" }, segment)))
            : MISSING,
        },
      ]),
    );
  }

  function renderFunds(funds) {
    const equity = funds.equity_margin_details ?? {};
    const fno = funds.fno_margin_details ?? {};
    const commodity = funds.commodity_margin_details; // not in every response / older docs
    const row = (label, value, colored = false) => ({
      label,
      value: fmtMoney(value),
      tone: colored ? tone(value) : "",
    });

    showBody(
      cards.funds,
      el(
        "div",
        { class: "hero" },
        el("span", { class: "hero__label" }, "Available cash"),
        el("span", { class: "hero__value" }, fmtMoney(funds.clear_cash)),
      ),
      kvList([
        row("Net margin used", funds.net_margin_used),
        row("Collateral available", funds.collateral_available),
        row("Collateral used", funds.collateral_used),
        row("Brokerage & charges", funds.brokerage_and_charges),
        row("Ad-hoc margin", funds.adhoc_margin),
      ]),
      el("h3", { class: "subhead" }, "Equity"),
      kvList([
        row("CNC balance available", equity.cnc_balance_available),
        row("MIS balance available", equity.mis_balance_available),
        row("CNC margin used", equity.cnc_margin_used),
        row("MIS margin used", equity.mis_margin_used),
        row("Net equity margin used", equity.net_equity_margin_used),
      ]),
      el("h3", { class: "subhead" }, "Futures & options"),
      kvList([
        row("Futures balance available", fno.future_balance_available),
        row("Option buy balance available", fno.option_buy_balance_available),
        row("Option sell balance available", fno.option_sell_balance_available),
        row("SPAN margin used", fno.span_margin_used),
        row("Exposure margin used", fno.exposure_margin_used),
        row("Net F&O margin used", fno.net_fno_margin_used),
      ]),
      commodity ? el("h3", { class: "subhead" }, "Commodity") : null,
      commodity
        ? kvList([
            row("SPAN margin", commodity.commodity_span_margin),
            row("Exposure margin", commodity.commodity_exposure_margin),
            row("Tender margin", commodity.commodity_tender_margin),
            row("Special margin", commodity.commodity_special_margin),
            row("Additional margin", commodity.commodity_additional_margin),
            row("Unrealised M2M", commodity.commodity_unrealised_m2m, true),
            row("Realised M2M", commodity.commodity_realised_m2m, true),
          ])
        : null,
    );
  }

  const invested = (row) => {
    const qty = toNumber(row.quantity);
    const price = toNumber(row.average_price);
    return qty !== null && price !== null ? qty * price : null;
  };

  function renderHoldings(rows) {
    const columns = [
      { label: "Symbol", cell: (r) => symbolCell(r.trading_symbol, r.isin) },
      { label: "Quantity", numeric: true, cell: (r) => numCell(fmtQty(r.quantity)) },
      { label: "Avg. price", numeric: true, cell: (r) => numCell(fmtMoney(r.average_price)) },
      { label: "Invested", numeric: true, cell: (r) => numCell(fmtMoney(invested(r))) },
      { label: "Free", numeric: true, cell: (r) => numCell(fmtQty(r.demat_free_quantity)) },
      { label: "Pledged", numeric: true, cell: (r) => numCell(fmtQty(r.pledge_quantity)) },
      { label: "T+1", numeric: true, cell: (r) => numCell(fmtQty(r.t1_quantity)) },
    ];
    showBody(cards.holdings, table(columns, rows, "No holdings in this account."));
    setMeta(
      cards.holdings,
      rows.length ? `${rows.length} holding${rows.length === 1 ? "" : "s"} \u00b7 invested ${fmtMoney(sum(rows, invested))}` : "",
    );
  }

  const qtyAtPrice = (qty, price) => (toNumber(qty) ? `${fmtQty(qty)} @ ${fmtMoney(price)}` : MISSING);

  function renderPositions(rows) {
    const columns = [
      {
        label: "Symbol",
        cell: (r) => symbolCell(r.trading_symbol, [r.exchange, r.segment, r.product].filter(Boolean).join(" \u00b7 ")),
      },
      { label: "Net qty", numeric: true, cell: (r) => numCell(fmtQty(r.quantity)) },
      { label: "Net price", numeric: true, cell: (r) => numCell(fmtMoney(r.net_price)) },
      { label: "Credit", numeric: true, cell: (r) => numCell(qtyAtPrice(r.credit_quantity, r.credit_price)) },
      { label: "Debit", numeric: true, cell: (r) => numCell(qtyAtPrice(r.debit_quantity, r.debit_price)) },
      { label: "Carry-fwd qty", numeric: true, cell: (r) => numCell(fmtQty(r.net_carry_forward_quantity)) },
      { label: "Realised P&L", numeric: true, cell: (r) => numCell(fmtMoney(r.realised_pnl), tone(r.realised_pnl)) },
    ];
    showBody(cards.positions, table(columns, rows, "No positions right now."));
    setMeta(
      cards.positions,
      rows.length
        ? `${rows.length} position${rows.length === 1 ? "" : "s"} \u00b7 realised ${fmtMoney(sum(rows, (r) => r.realised_pnl))}`
        : "",
    );
  }

  const SECTIONS = [
    { key: "profile", path: "/api/profile", render: renderProfile },
    { key: "funds", path: "/api/funds", render: renderFunds },
    { key: "holdings", path: "/api/holdings", render: renderHoldings },
    { key: "positions", path: "/api/positions", render: renderPositions },
  ];

  // ------------------------------------------------------------ connection UI

  function setPill(kind, text) {
    ui.pill.className = `pill pill--${kind}`;
    ui.pill.textContent = text;
  }

  function showBanner(message) {
    ui.banner.textContent = message;
    ui.banner.hidden = false;
  }

  const hideBanner = () => {
    ui.banner.hidden = true;
  };

  function describeSession(status) {
    const parts = [];
    if (status.connected_at) parts.push(`Logged in ${dateTime.format(new Date(status.connected_at))} IST`);
    if (status.expires_at) {
      const expires = new Date(status.expires_at);
      parts.push(
        expires.getTime() > Date.now()
          ? `token valid until ${dateTime.format(expires)} IST`
          : "token expired, it renews automatically on the next refresh",
      );
    }
    if (status.login_count) parts.push(`${status.login_count} login${status.login_count === 1 ? "" : "s"} since the server started`);
    return parts.join(" \u00b7 ");
  }

  function applyStatus(status) {
    const connected = Boolean(status.connected);
    ui.connect.hidden = connected;
    ui.refresh.hidden = !connected;
    ui.disconnect.hidden = !connected;
    setPill(connected ? "ok" : "idle", connected ? "Connected" : "Not connected");
    ui.meta.textContent = connected ? describeSession(status) : "";
    if (!connected) resetCards();
  }

  async function refreshStatus() {
    try {
      const status = await api("/api/status");
      applyStatus(status);
      return status;
    } catch (error) {
      setPill("error", "Server unreachable");
      ui.connect.hidden = true;
      ui.refresh.hidden = true;
      ui.disconnect.hidden = true;
      showBanner(error.message);
      return null;
    }
  }

  // ------------------------------------------------------------------ actions

  async function loadSection(section) {
    const card = cards[section.key];
    card.setAttribute("aria-busy", "true");
    try {
      const body = await api(section.path);
      setRaw(card, body.data);
      section.render(body.data);
    } catch (error) {
      if (error.status === 401) throw error; // connection lost: handled once, for the whole page
      showMessage(card, error.message, "error");
    } finally {
      card.removeAttribute("aria-busy");
    }
  }

  async function loadAll() {
    const results = await Promise.allSettled(SECTIONS.map(loadSection));
    if (results.some((result) => result.status === "rejected")) {
      const rejected = results.find((result) => result.status === "rejected");
      showBanner(rejected.reason.message);
      await refreshStatus(); // the server knows whether we are still connected
      return;
    }
    const status = await refreshStatus();
    if (status) ui.meta.textContent = `Updated ${timeOnly.format(new Date())} IST \u00b7 ${describeSession(status)}`;
  }

  async function connect() {
    hideBanner();
    setPill("busy", "Connecting\u2026");
    ui.connect.textContent = "Connecting\u2026";
    for (const section of SECTIONS) showMessage(cards[section.key], "Loading\u2026");
    try {
      applyStatus(await api("/api/connect", "POST"));
    } catch (error) {
      showBanner(error.message);
      setPill("error", "Connection failed");
      resetCards();
      return;
    } finally {
      ui.connect.textContent = "Connect to Groww";
    }
    await loadAll();
  }

  async function disconnect() {
    hideBanner();
    try {
      applyStatus(await api("/api/disconnect", "POST"));
    } catch (error) {
      showBanner(error.message);
    }
  }

  let busy = false;
  const buttons = [ui.connect, ui.refresh, ui.disconnect];

  async function exclusive(task) {
    if (busy) return;
    busy = true;
    for (const button of buttons) button.disabled = true;
    try {
      hideBanner();
      await task();
    } finally {
      busy = false;
      for (const button of buttons) button.disabled = false;
    }
  }

  ui.connect.addEventListener("click", () => exclusive(connect));
  ui.refresh.addEventListener("click", () => exclusive(loadAll));
  ui.disconnect.addEventListener("click", () => exclusive(disconnect));

  // Opening (or reloading) the page never logs in: it only asks the server whether a session
  // already exists, and loads the data if so.
  exclusive(async () => {
    const status = await refreshStatus();
    if (status && status.connected) await loadAll();
  });
})();
