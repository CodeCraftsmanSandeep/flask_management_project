import assert from "node:assert/strict";
import test from "node:test";

import { buildPayload, countText, formProblem, initialOf, statusInfo } from "../../app/static/js/accounts-logic.js";

const zerodha = {
  key: "zerodha",
  name: "Zerodha",
  fields: [
    { name: "api_key", label: "API key", required: true },
    { name: "api_secret", label: "API secret", required: true },
  ],
};
const other = {
  key: "other",
  name: "Another broker",
  fields: [
    { name: "broker_name", label: "Broker name", required: true },
    { name: "api_key", label: "API key", required: false },
  ],
};
const groww = { key: "groww", name: "Groww", fields: [] };

test("the circle shows the first letter of the name, upper-cased", () => {
  assert.equal(initialOf("my zerodha"), "M");
  assert.equal(initialOf("  Groww"), "G");
  assert.equal(initialOf(""), "?");
  assert.equal(initialOf(null), "?");
  assert.equal(initialOf("\u{1F4C8} trades"), "\u{1F4C8}");
});

test("statuses have a readable text and a tone", () => {
  assert.deepEqual(statusInfo("connected"), { text: "Connected", tone: "ok" });
  assert.equal(statusInfo("saved").text, "Saved");
  assert.equal(statusInfo("weird").text, "weird");
  assert.equal(statusInfo(undefined).tone, "idle");
});

test("the form says what is missing, in the order a person fills it in", () => {
  assert.equal(formProblem(null, "A", {}), "Pick a broker.");
  assert.equal(formProblem(zerodha, "  ", {}), "Give the account a name.");
  assert.equal(formProblem(zerodha, "Main", { api_key: "k" }), "API secret is required for Zerodha.");
  assert.equal(formProblem(zerodha, "Main", { api_key: "k", api_secret: "s" }), "");
  assert.equal(formProblem(other, "X", { broker_name: " " }), "Broker name is required for Another broker.");
  assert.equal(formProblem(other, "X", { broker_name: "Upstox" }), "");
  assert.equal(formProblem(groww, "Groww", {}), "");
});

test("the payload is trimmed and leaves optional fields out when empty", () => {
  assert.deepEqual(buildPayload(zerodha, " Main ", { api_key: " k ", api_secret: "s" }), {
    broker: "zerodha",
    label: "Main",
    credentials: { api_key: "k", api_secret: "s" },
  });
  assert.deepEqual(buildPayload(other, "X", { broker_name: "Upstox", api_key: "  " }).credentials, {
    broker_name: "Upstox",
  });
  assert.deepEqual(buildPayload(groww, "G", {}), { broker: "groww", label: "G", credentials: {} });
});

test("the account count reads naturally", () => {
  assert.equal(countText(0), "No accounts yet");
  assert.equal(countText(1), "1 account");
  assert.equal(countText(3), "3 accounts");
});
