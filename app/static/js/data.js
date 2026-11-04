/*
 * Data page: connect a broker, start/stop the market-data download, watch its progress, and see
 * what is stored. Builds DOM with textContent only.
 */

import { getJson, isAbort, postJson } from "./api.js";
import { byId, el, fill, show } from "./dom.js";
import { MISSING, plain, shortDate } from "./format.js";

const TIMEFRAMES = [5, 15, 30];
const POLL_ACTIVE_MS = 1000;

const PHASES = [
  ["expiries", "Expiry calendar"],
  ["spot", "NIFTY candles"],
  ["contracts", "Option contract lists"],
  ["options", "Option candles"],
];

const STATE_TEXT = {
  idle: "Not running",
  running: "Downloading\u2026",
  stopping: "Stopping after the current request\u2026",
  done: "Finished",
  stopped: "Stopped (finished work is kept)",
  failed: "Failed",
};

const FETCH_KINDS = {
  spot: "NIFTY candles",
  option: "Option candles",
  expiries: "Expiry calendar",
  contracts: "Contract lists",
};

const ui = { timer: 0, polling: false, connected: false, job: null, wasActive: false, busy: false };

function showBanner(error, retry) {
  byId("banner-text").textContent = error?.message ?? String(error);
  const button = byId("btn-retry");
  button.hidden = !retry;
  button.onclick = retry ?? null;
  show(byId("banner"), true);
}

function hideBanner() {
  show(byId("banner"), false);
}

// ------------------------------------------------------------------------ connection

function renderConnection(status) {
  ui.connected = Boolean(status?.connected);
  const pill = byId("status-pill");
  pill.className = `pill ${ui.connected ? "pill--ok" : "pill--idle"}`;
  pill.textContent = ui.connected ? "Broker connected" : "Not connected";
  show(byId("btn-connect"), !ui.connected);
  updateButtons();
}

async function refreshConnection() {
  try {
    renderConnection(await getJson("/api/status"));
  } catch (error) {
    showBanner(error, refreshConnection);
  }
}

async function connect() {
  const button = byId("btn-connect");
  button.disabled = true;
  button.textContent = "Connecting\u2026";
  hideBanner();
  try {
    renderConnection(await postJson("/api/connect"));
  } catch (error) {
    showBanner(error);
  } finally {
    button.disabled = false;
    button.textContent = "Connect broker";
  }
}

// ------------------------------------------------------------------------------ job

function selectedTimeframes() {
  return TIMEFRAMES.filter((tf) => byId(`tf-${tf}`).checked);
}

function updateButtons() {
  const active = Boolean(ui.job?.active);
  const start = byId("btn-download");
  start.disabled = active || ui.busy || !ui.connected;
  show(byId("btn-stop"), active);
  byId("btn-stop").disabled = ui.job?.state === "stopping";
  byId("in-months").disabled = active;
  for (const tf of TIMEFRAMES) byId(`tf-${tf}`).disabled = active;
  byId("download-hint").textContent = ui.connected || active ? "" : "Connect a broker first.";
}

function percentOf(progress) {
  if (!progress.total) return null;
  return Math.min(100, Math.round((progress.done / progress.total) * 100));
}

function renderJob(job) {
  ui.job = job;
  const box = byId("job");
  byId("job-state").textContent = STATE_TEXT[job.state] ?? job.state;
  show(box, job.state !== "idle");
  updateButtons();
  if (job.state === "idle") return;

  const { progress } = job;
  const phaseIndex = PHASES.findIndex(([key]) => key === progress.phase);
  const label = phaseIndex >= 0 ? `Step ${phaseIndex + 1} of ${PHASES.length}: ${PHASES[phaseIndex][1]}` : STATE_TEXT[job.state];
  byId("job-title").textContent = job.active ? label : STATE_TEXT[job.state];

  const bar = byId("job-bar");
  const percent = job.active ? percentOf(progress) : job.state === "done" ? 100 : percentOf(progress);
  bar.classList.toggle("progress__bar--busy", job.active && percent === null);
  bar.style.width = percent === null ? (job.active ? "100%" : "0%") : `${percent}%`;
  bar.parentElement.setAttribute("aria-valuenow", String(percent ?? 0));
  byId("job-count").textContent = job.active && progress.total ? `${plain(progress.done)} of ${plain(progress.total)}` : "";
  byId("job-message").textContent = job.active
    ? progress.message
    : job.request
      ? `${shortDate(job.request.from)} to ${shortDate(job.request.to)} \u00b7 timeframes ${job.request.timeframes.join(", ")} min`
      : "";

  const error = byId("job-error");
  error.textContent = job.error ?? "";
  error.hidden = !job.error;
  renderSummary(job.summary);
}

function renderSummary(summary) {
  const node = byId("job-summary");
  if (!summary) {
    fill(node);
    return;
  }
  const rows = PHASES.filter(([key]) => summary.phases[key]).map(([key, label]) => {
    const p = summary.phases[key];
    return el(
      "tr",
      {},
      el("th", { scope: "row" }, label),
      el("td", { class: "num" }, plain(p.fetched)),
      el("td", { class: "num" }, plain(p.empty)),
      el("td", { class: "num" }, plain(p.cached)),
      el("td", { class: `num ${p.errors ? "neg" : ""}`.trim() }, plain(p.errors)),
    );
  });
  fill(
    node,
    el(
      "div",
      { class: "table-wrap" },
      el(
        "table",
        { class: "table table--compare" },
        el("thead", {}, el("tr", {}, [["Step", ""], ["Downloaded", "num"], ["Empty", "num"], ["Already had", "num"], ["Errors", "num"]].map(([h, c]) => el("th", { scope: "col", class: c }, h)))),
        el("tbody", {}, rows),
      ),
    ),
    summary.problems.length
      ? el(
          "details",
          { class: "raw", open: true },
          el("summary", {}, `Data notes (${summary.problems_total})`),
          el("ul", { class: "plainlist" }, summary.problems.map((p) => el("li", {}, p)), summary.problems_total > summary.problems.length ? el("li", {}, `\u2026and ${summary.problems_total - summary.problems.length} more`) : null),
        )
      : null,
  );
}

async function startDownload() {
  const months = Number(byId("in-months").value);
  const timeframes = selectedTimeframes();
  if (!Number.isInteger(months) || months < 1 || months > 24) {
    showBanner(new Error("Months of history must be a whole number from 1 to 24."));
    return;
  }
  if (!timeframes.length) {
    showBanner(new Error("Pick at least one signal timeframe."));
    return;
  }
  hideBanner();
  ui.busy = true;
  updateButtons();
  try {
    renderJob(await postJson("/api/data/download", { months, timeframes }));
    schedulePoll();
  } catch (error) {
    showBanner(error);
  } finally {
    ui.busy = false;
    updateButtons();
  }
}

async function stopDownload() {
  try {
    renderJob(await postJson("/api/data/stop"));
  } catch (error) {
    showBanner(error);
  }
}

// ----------------------------------------------------------------------------- stored

function sourceLabel(data) {
  if (!data.has_data) return "Nothing yet";
  if (data.synthetic) return "Simulated data";
  return data.data_source?.title ?? "Imported or downloaded candles";
}

function renderStored(data) {
  const meta = byId("stored-meta");
  meta.textContent = data.has_data ? `${shortDate(data.spot_range[0])} \u2013 ${shortDate(data.spot_range[1])}` : "nothing yet";
  const convention =
    data.timestamp_convention === "start"
      ? "Opening minute (as expected)"
      : data.timestamp_convention === "close"
        ? "Closing minute (shifted back one minute)"
        : (data.timestamp_convention ?? "Not detected yet");
  const facts = [
    ["Data source", sourceLabel(data)],
    ["NIFTY 1-minute rows", plain(data.spot_rows)],
    ["Option 1-minute rows", plain(data.option_rows)],
    ["Expiry dates known", plain(data.expiries)],
    ["A candle\u2019s timestamp marks its", convention],
  ];
  const summary = data.fetch_summary.length
    ? el(
        "div",
        { class: "table-wrap" },
        el(
          "table",
          { class: "table" },
          el("thead", {}, el("tr", {}, [["Kind", ""], ["Result", ""], ["Days", "num"], ["Rows", "num"]].map(([h, c]) => el("th", { scope: "col", class: c }, h)))),
          el(
            "tbody",
            {},
            data.fetch_summary.map((r) =>
              el("tr", {}, el("td", {}, FETCH_KINDS[r.kind] ?? r.kind), el("td", { class: r.status === "error" ? "neg" : "" }, r.status), el("td", { class: "num" }, plain(r.days)), el("td", { class: "num" }, plain(r.rows))),
            ),
          ),
        ),
      )
    : el("p", { class: "empty" }, "No downloads recorded yet.");
  fill(
    byId("stored"),
    data.has_data ? null : el("p", { class: "empty" }, "No NIFTY data yet. Connect a broker and start a download, or import CSV files with make import-csv (see the readme)."),
    el("dl", { class: "kv" }, facts.map(([label, value]) => el("div", { class: "kv__row" }, el("dt", {}, label), el("dd", {}, value)))),
    el("h3", { class: "subhead" }, "Download log"),
    summary,
    data.failures.length
      ? [
          el("h3", { class: "subhead" }, "Recent failures (retried on the next download)"),
          el("ul", { class: "plainlist" }, data.failures.map((f) => el("li", {}, `${FETCH_KINDS[f.kind] ?? f.kind} ${f.key} on ${shortDate(f.day)}: ${f.detail || MISSING}`))),
        ]
      : null,
    data.has_data ? el("p", { class: "note" }, el("a", { href: "/backtest" }, "Open the backtest \u2192")) : null,
  );
}

// ------------------------------------------------------------------------- refresh loop

async function refresh() {
  try {
    const data = await getJson("/api/data/status");
    hideBanner();
    show(byId("synthetic-banner"), data.synthetic);
    renderStored(data);
    const wasActive = ui.wasActive;
    renderJob(data.job);
    ui.wasActive = Boolean(data.job.active);
    if (data.job.active) schedulePoll();
    else if (wasActive) await refreshConnection(); // a long download may have re-logged in
  } catch (error) {
    if (!isAbort(error)) showBanner(error, refresh);
  }
}

function schedulePoll() {
  window.clearTimeout(ui.timer);
  ui.timer = window.setTimeout(refresh, POLL_ACTIVE_MS);
}

function init() {
  fill(
    byId("tf-checks"),
    TIMEFRAMES.map((tf) =>
      el("label", { class: "check" }, el("input", { id: `tf-${tf}`, type: "checkbox", checked: true }), `${tf} min`),
    ),
  );
  byId("btn-connect").addEventListener("click", connect);
  byId("btn-download").addEventListener("click", startDownload);
  byId("btn-stop").addEventListener("click", stopDownload);
  refreshConnection();
  refresh();
}

init();
