/*
 * Pure helpers (no DOM, no network) so they can be unit-tested with `node --test`.
 * Times from the API are naive Indian wall-clock ISO strings ("2026-05-12T09:45:00").
 */

export const MISSING = "\u2014";
const MINUS = "\u2212";

const whole = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
const cents = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

/** ₹1,23,456 — with `decimals: 2` for per-trade figures and `sign: true` for P&L. */
export function rupees(value, { decimals = 0, sign = false } = {}) {
  if (!isNumber(value)) return MISSING;
  const formatter = decimals === 2 ? cents : whole;
  const text = formatter.format(Math.abs(value));
  // A value that rounds to zero shows no sign: "-₹0" would be misleading.
  const isZero = Number(text.replace(/,/g, "")) === 0;
  if (isZero) return `\u20b9${text}`;
  if (value < 0) return `${MINUS}\u20b9${text}`;
  return `${sign ? "+" : ""}\u20b9${text}`;
}

/** 123456.5 -> "1,23,457" (no currency symbol), for index levels and counts. */
export function plain(value, decimals = 0) {
  if (!isNumber(value)) return MISSING;
  return (decimals === 2 ? cents : whole).format(value);
}

/** Win rate etc. arrive as ratios (0.5556) and are shown as percentages. */
export function percent(ratio, digits = 1) {
  if (!isNumber(ratio)) return MISSING;
  return `${(ratio * 100).toFixed(digits)}%`;
}

export function factor(value, infinite = false) {
  if (infinite) return "\u221e";
  if (!isNumber(value)) return MISSING;
  return value.toFixed(2);
}

export function tone(value) {
  if (!isNumber(value) || value === 0) return "";
  return value > 0 ? "pos" : "neg";
}

/** 1 -> "1st", 2 -> "2nd", 11 -> "11th", 22 -> "22nd". */
export function ordinal(n) {
  const v = n % 100;
  if (v >= 11 && v <= 13) return `${n}th`;
  return `${n}${["th", "st", "nd", "rd"][n % 10] ?? "th"}`;
}

/** "2026-05-12" -> "12 May 2026"; "2026-05-12T09:45:00" -> same. Never uses the local time zone. */
export function shortDate(iso, withYear = true) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso ?? "");
  if (!match) return MISSING;
  const month = MONTHS[Number(match[2]) - 1];
  if (!month) return MISSING;
  const day = Number(match[3]);
  return withYear ? `${day} ${month} ${match[1]}` : `${day} ${month}`;
}

/** "2026-05-12T09:45:00" -> "09:45". */
export function clock(iso) {
  const match = /T(\d{2}):(\d{2})/.exec(iso ?? "");
  return match ? `${match[1]}:${match[2]}` : MISSING;
}

/**
 * Seconds for the chart library. The wall-clock time is read as if it were UTC, so the chart's
 * axis shows exactly the exchange time whatever the browser's own time zone is.
 */
export function wallSeconds(iso) {
  const match = /^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2})(?::(\d{2}))?/.exec(iso ?? "");
  if (!match) return null;
  const [, y, mo, d, h, mi, s] = match;
  return Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s ?? 0)) / 1000;
}

/**
 * The signal bar a decision time belongs to: the bar with start < t <= start + timeframe.
 * A decision made at a bar's close belongs to that closing bar; a 15:00 square-off that falls
 * inside a longer bar belongs to the bar it falls in. Returns that bar's chart time or null.
 */
export function barForTime(bars, iso, timeframeMinutes) {
  const at = wallSeconds(iso);
  if (at === null) return null;
  const span = timeframeMinutes * 60;
  for (let index = bars.length - 1; index >= 0; index -= 1) {
    const start = wallSeconds(bars[index][0]);
    if (start !== null && start < at && at <= start + span) return start;
  }
  return null;
}

/**
 * The chart time of the candle a fill belongs to: the latest candle that starts at or before
 * `at`, or the first candle if the fill predates all of them. `times` must be ascending.
 */
export function snapTime(times, at) {
  if (!times.length || !isNumber(at)) return null;
  let found = times[0];
  for (const time of times) {
    if (time <= at) found = time;
    else break;
  }
  return found;
}

/**
 * Keyboard navigation for a list. Returns the new selected index (or -1 for an empty list).
 * Unknown keys return the current index, so callers can tell nothing moved.
 */
export function moveSelection(current, count, key) {
  if (count <= 0) return -1;
  const clamp = (n) => Math.min(count - 1, Math.max(0, n));
  const from = current < 0 ? -1 : current;
  switch (key) {
    case "ArrowDown":
    case "j":
      return clamp(from + 1);
    case "ArrowUp":
    case "k":
      return clamp(from < 0 ? 0 : from - 1);
    case "PageDown":
      return clamp(from + 10);
    case "PageUp":
      return clamp(from < 0 ? 0 : from - 10);
    case "Home":
      return 0;
    case "End":
      return count - 1;
    default:
      return current;
  }
}

/** Chart series need strictly increasing, unique times: sort, and keep the last of any repeats. */
export function uniqueAscending(points) {
  const sorted = points
    .filter((point) => point && isNumber(point.time) && isNumber(point.value))
    .sort((a, b) => a.time - b.time);
  const result = [];
  for (const point of sorted) {
    if (result.length && result[result.length - 1].time === point.time) result[result.length - 1] = point;
    else result.push(point);
  }
  return result;
}

// ------------------------------------------------------------------- filter state in the URL

export const DEFAULT_FILTERS = Object.freeze({
  timeframe: "15",
  mode: "all",
  outcome: "all",
  start: "09:30",
  cutoff: "14:30",
  cross: "all",
  days: "mon,tue,wed,thu,fri,sat,sun",
  from: "",
  to: "",
});

export const DAY_KEYS = Object.freeze(["mon", "tue", "wed", "thu", "fri", "sat", "sun"]);
export const DAY_LABELS = Object.freeze({ mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" });

/** "thu, MON" -> "mon,thu" (week order). Anything unusable gives the default: every day. */
export function normalizeDays(value) {
  const asked = new Set(
    String(value ?? "")
      .toLowerCase()
      .split(",")
      .map((part) => part.trim()),
  );
  const kept = DAY_KEYS.filter((key) => asked.has(key));
  return kept.length ? kept.join(",") : DEFAULT_FILTERS.days;
}

/** Switch one weekday on or off. Returns null when that would leave no day at all. */
export function toggleDay(current, key) {
  const chosen = new Set(normalizeDays(current).split(","));
  if (chosen.has(key)) chosen.delete(key);
  else chosen.add(key);
  const next = DAY_KEYS.filter((day) => chosen.has(day));
  return next.length ? next.join(",") : null;
}

/** A short phrase for the summary line: "" when every day is on. */
export function describeDays(value) {
  const days = normalizeDays(value).split(",");
  if (days.length === DAY_KEYS.length) return "";
  if (days.join(",") === "mon,tue,wed,thu,fri") return "Mon\u2013Fri only";
  return `${days.map((day) => DAY_LABELS[day]).join(", ")} only`;
}

/** Only values that differ from the defaults go into the URL, so a bare /backtest stays clean. */
export function toQuery(filters) {
  const params = new URLSearchParams();
  for (const key of Object.keys(DEFAULT_FILTERS)) {
    const value = String(filters[key] ?? "");
    if (value !== "" && value !== DEFAULT_FILTERS[key]) params.set(key, value);
  }
  return params.toString();
}

/** The full API query: every filter explicit, so server defaults can never drift from the UI. */
export function toApiQuery(filters) {
  const params = new URLSearchParams();
  for (const key of Object.keys(DEFAULT_FILTERS)) {
    const value = String(filters[key] ?? "");
    if (value !== "") params.set(key, value);
  }
  return params.toString();
}

/** Read filters from a query string; anything not in `allowed` falls back to the default. */
export function fromQuery(search, allowed = {}) {
  const params = new URLSearchParams(search);
  const result = { ...DEFAULT_FILTERS };
  for (const key of Object.keys(DEFAULT_FILTERS)) {
    const value = params.get(key);
    if (value === null) continue;
    const choices = allowed[key];
    if (choices && !choices.includes(value)) continue;
    result[key] = key === "days" ? normalizeDays(value) : value;
  }
  return result;
}

export function countActiveFilters(filters) {
  return Object.keys(DEFAULT_FILTERS).filter(
    (key) => String(filters[key] ?? "") !== DEFAULT_FILTERS[key],
  ).length;
}

// --------------------------------------------------------------------------- labels

const EXIT_REASONS = {
  opposite_cross: "Opposite crossover",
  square_off: "15:00 square-off",
};

export function exitReasonLabel(reason) {
  return EXIT_REASONS[reason] ?? String(reason ?? MISSING);
}

const MODE_SHORT = { itm5_buy: "ITM5 buy", atm_buy: "ATM buy", atm_sell: "ATM sell" };

export function modeShort(mode) {
  return MODE_SHORT[mode] ?? String(mode ?? MISSING);
}

export function directionLabel(direction) {
  return direction === "bullish" ? "Bullish" : direction === "bearish" ? "Bearish" : MISSING;
}

const OUTCOME_LABELS = { win: "Win", loss: "Loss", tie: "Even" };

export function outcomeLabel(outcome) {
  return OUTCOME_LABELS[outcome] ?? MISSING;
}

/** "NSE-NIFTY-12May26-24000-CE" -> "NIFTY 12 May 26 24000 CE", for reading at a glance. */
export function prettySymbol(symbol) {
  const match = /^NSE-([A-Z]+)-(\d{1,2})([A-Za-z]{3})(\d{2})-(\d+)-(CE|PE)$/.exec(symbol ?? "");
  if (!match) return String(symbol ?? MISSING);
  const [, name, day, month, year, strike, type] = match;
  return `${name} ${day} ${month} ${year} ${strike} ${type}`;
}
