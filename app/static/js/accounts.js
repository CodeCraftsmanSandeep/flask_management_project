/*
 * Accounts page: one circle per broker account, a "+" circle to add one, and a detail panel for the
 * selected account. Talks only to /api/accounts; builds DOM with textContent only, so nothing the
 * server returns can inject markup. The server never sends a full secret, only a masked tail.
 */

import { ApiError, deleteJson, getJson, postJson } from "./api.js";
import { buildPayload, countText, formProblem, initialOf, statusInfo } from "./accounts-logic.js";
import { byId, el, fill, show, toast } from "./dom.js";

const REMOVE_CONFIRM_MS = 4000;

const ui = {
  brokers: [],
  accounts: [],
  selected: null, // account id
  pickedBroker: null, // broker key inside the dialog
  removeTimer: 0,
};

function showBanner(error) {
  const banner = byId("accounts-banner");
  banner.textContent = error instanceof ApiError ? error.message : "Something went wrong. Check the server log.";
  show(banner, true);
}

// ------------------------------------------------------------------------------ circles

function circle(account) {
  const status = statusInfo(account.status);
  const button = el(
    "button",
    {
      class: "acct",
      type: "button",
      "aria-pressed": String(ui.selected === account.id),
      "data-id": account.id,
      title: `${account.label} \u00b7 ${account.broker_name} \u00b7 ${status.text}`,
    },
    el("span", { class: `acct__disc acct__disc--${account.broker}` }, initialOf(account.label), el("span", { class: `acct__dot acct__dot--${status.tone}`, "aria-hidden": "true" })),
    el("span", { class: "acct__name" }, account.label),
    el("span", { class: "acct__broker" }, account.broker_name),
  );
  button.addEventListener("click", () => select(ui.selected === account.id ? null : account.id));
  return button;
}

function addCircle() {
  const button = el(
    "button",
    { class: "acct acct--add", type: "button" },
    el("span", { class: "acct__disc acct__disc--add" }, "+"),
    el("span", { class: "acct__name" }, "Add account"),
    el("span", { class: "acct__broker" }, "Any broker"),
  );
  button.addEventListener("click", openDialog);
  return button;
}

function renderCircles() {
  fill(byId("account-circles"), ui.accounts.map(circle), addCircle());
  byId("accounts-meta").textContent = countText(ui.accounts.length);
}

// ------------------------------------------------------------------------------- detail

function select(id) {
  ui.selected = id;
  window.clearTimeout(ui.removeTimer);
  renderCircles();
  renderDetail();
}

function renderDetail() {
  const box = byId("account-detail");
  const account = ui.accounts.find((a) => a.id === ui.selected);
  const groww = Boolean(account && account.broker === "groww");
  show(byId("groww-overview"), groww);
  if (!account) {
    show(box, false);
    return;
  }
  const broker = ui.brokers.find((b) => b.key === account.broker);
  const status = statusInfo(account.status);
  const rows = Object.entries(account.credentials).map(([name, value]) => {
    const field = broker?.fields.find((f) => f.name === name);
    return el("div", { class: "kv__row" }, el("dt", {}, field?.label ?? name), el("dd", { class: "mono" }, value));
  });
  const remove = el("button", { class: "btn btn--ghost", type: "button" }, "Remove account");
  remove.addEventListener("click", () => removeAccount(account, remove));
  fill(
    box,
    el("h3", { class: "detail__title" }, account.label, el("span", { class: `pill pill--${status.tone === "ok" ? "ok" : "idle"}` }, status.text)),
    el("p", { class: "detail__sub" }, `${account.broker_name} \u00b7 added ${account.created_at.slice(0, 10)}`),
    rows.length ? el("dl", { class: "kv" }, rows) : null,
    broker?.note ? el("p", { class: "note" }, broker.note) : null,
    el("p", { class: "actions" }, remove),
  );
  show(box, true);
}

async function removeAccount(account, button) {
  // Two clicks: the first arms the button, the second (within a few seconds) removes the account.
  if (button.dataset.armed !== "yes") {
    button.dataset.armed = "yes";
    button.textContent = "Click again to remove";
    button.classList.add("btn--danger");
    ui.removeTimer = window.setTimeout(() => {
      button.dataset.armed = "";
      button.textContent = "Remove account";
      button.classList.remove("btn--danger");
    }, REMOVE_CONFIRM_MS);
    return;
  }
  window.clearTimeout(ui.removeTimer);
  button.disabled = true;
  try {
    await deleteJson(`/api/accounts/${encodeURIComponent(account.id)}`);
    ui.selected = null;
    await load();
    toast(`Removed ${account.label}.`);
  } catch (error) {
    button.disabled = false;
    showBanner(error);
  }
}

// ------------------------------------------------------------------------------- dialog

function brokerOf(key) {
  return ui.brokers.find((b) => b.key === key) ?? null;
}

function pickBroker(key) {
  ui.pickedBroker = key;
  const broker = brokerOf(key);
  for (const button of byId("add-broker").children) {
    const on = button.dataset.value === key;
    button.setAttribute("aria-checked", String(on));
    button.tabIndex = on ? 0 : -1;
  }
  byId("add-note").textContent = broker?.note ?? "";
  fill(
    byId("add-fields"),
    (broker?.fields ?? []).map((field) =>
      el(
        "div",
        { class: "field" },
        el("label", { class: "field__label", for: `f-${field.name}` }, field.required ? field.label : `${field.label} (optional)`),
        el("input", {
          id: `f-${field.name}`,
          class: "input",
          type: field.secret ? "password" : "text",
          autocomplete: "off",
          autocapitalize: "off",
          spellcheck: "false",
          maxlength: "200",
          "data-field": field.name,
        }),
        field.help ? el("span", { class: "field__help" }, field.help) : null,
      ),
    ),
  );
  show(byId("add-error"), false);
}

function openDialog() {
  const dialog = byId("add-dialog");
  byId("add-label").value = "";
  fill(
    byId("add-broker"),
    ui.brokers.map((broker) => {
      const button = el("button", { class: "seg__btn", type: "button", role: "radio", "aria-checked": "false", tabindex: "-1", "data-value": broker.key }, broker.name);
      button.addEventListener("click", () => pickBroker(broker.key));
      return button;
    }),
  );
  pickBroker(ui.brokers[0]?.key ?? null);
  byId("add-save").disabled = false;
  dialog.showModal();
  byId("add-label").focus();
}

function formValues() {
  const values = {};
  for (const input of byId("add-fields").querySelectorAll("input[data-field]")) values[input.dataset.field] = input.value;
  return values;
}

async function submitAccount(event) {
  event.preventDefault();
  const broker = brokerOf(ui.pickedBroker);
  const label = byId("add-label").value;
  const values = formValues();
  const problem = formProblem(broker, label, values);
  const error = byId("add-error");
  if (problem) {
    error.textContent = problem;
    show(error, true);
    return;
  }
  show(error, false);
  const save = byId("add-save");
  save.disabled = true;
  try {
    const created = await postJson("/api/accounts", buildPayload(broker, label, values));
    byId("add-dialog").close();
    // the values never stay in the page once they are sent
    for (const input of byId("add-fields").querySelectorAll("input")) input.value = "";
    ui.selected = created.id;
    await load();
    toast(`Added ${created.label}.`);
  } catch (caught) {
    error.textContent = caught instanceof ApiError ? caught.message : "Could not add the account.";
    show(error, true);
  } finally {
    save.disabled = false;
  }
}

// --------------------------------------------------------------------------------- load

async function load() {
  try {
    const data = await getJson("/api/accounts");
    ui.brokers = data.brokers;
    ui.accounts = data.accounts;
    if (ui.selected && !ui.accounts.some((a) => a.id === ui.selected)) ui.selected = null;
    show(byId("accounts-banner"), false);
  } catch (error) {
    showBanner(error);
    return;
  }
  renderCircles();
  renderDetail();
}

function init() {
  byId("add-form").addEventListener("submit", submitAccount);
  byId("add-cancel").addEventListener("click", () => byId("add-dialog").close());
  byId("add-dialog").addEventListener("close", () => {
    for (const input of byId("add-fields").querySelectorAll("input")) input.value = "";
  });
  load();
}

init();
