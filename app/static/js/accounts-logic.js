/* Pure helpers for the Accounts page (no DOM), so they can be tested with `node --test`. */

/** The letter shown inside an account's circle. */
export function initialOf(label) {
  const first = Array.from(String(label ?? "").trim())[0];
  return first ? first.toUpperCase() : "?";
}

const STATUS = {
  connected: { text: "Connected", tone: "ok" },
  not_connected: { text: "Not connected", tone: "idle" },
  saved: { text: "Saved", tone: "idle" },
};

export function statusInfo(status) {
  return STATUS[status] ?? { text: String(status ?? "Unknown"), tone: "idle" };
}

/** Why the form cannot be sent yet ("" when it can). The server checks again; this is for speed. */
export function formProblem(broker, label, values) {
  if (!broker) return "Pick a broker.";
  if (!String(label ?? "").trim()) return "Give the account a name.";
  for (const field of broker.fields) {
    if (field.required && !String(values[field.name] ?? "").trim()) {
      return `${field.label} is required for ${broker.name}.`;
    }
  }
  return "";
}

/** The request body: trimmed values, and optional fields left out when empty. */
export function buildPayload(broker, label, values) {
  const credentials = {};
  for (const field of broker.fields) {
    const text = String(values[field.name] ?? "").trim();
    if (text) credentials[field.name] = text;
  }
  return { broker: broker.key, label: String(label).trim(), credentials };
}

/** "3 accounts", "1 account", "No accounts yet". */
export function countText(count) {
  if (count === 0) return "No accounts yet";
  return `${count} account${count === 1 ? "" : "s"}`;
}
