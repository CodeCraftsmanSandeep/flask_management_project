import assert from "node:assert/strict";
import test from "node:test";

import {
  DEFAULT_FILTERS,
  barForTime,
  clock,
  countActiveFilters,
  describeDays,
  exitReasonLabel,
  factor,
  fromQuery,
  moveSelection,
  normalizeDays,
  ordinal,
  percent,
  plain,
  prettySymbol,
  rupees,
  shortDate,
  snapTime,
  toApiQuery,
  toQuery,
  toggleDay,
  tone,
  uniqueAscending,
  wallSeconds,
} from "../../app/static/js/format.js";

const MINUS = "\u2212";

test("rupees uses Indian digit grouping and a real minus sign", () => {
  assert.equal(rupees(1234567), "\u20b912,34,567");
  assert.equal(rupees(-1234567), `${MINUS}\u20b912,34,567`);
  assert.equal(rupees(1234.5, { decimals: 2 }), "\u20b91,234.50");
  assert.equal(rupees(500, { sign: true }), "+\u20b9500");
  assert.equal(rupees(-500, { sign: true }), `${MINUS}\u20b9500`);
});

test("rupees never shows a negative zero", () => {
  assert.equal(rupees(-0.2), "\u20b90");
  assert.equal(rupees(-0.004, { decimals: 2 }), "\u20b90.00");
  assert.equal(rupees(0, { sign: true }), "\u20b90");
  assert.equal(rupees(0.3, { sign: true }), "\u20b90");
});

test("missing or non-finite values show a dash, never NaN", () => {
  for (const bad of [null, undefined, NaN, Infinity, "12", {}]) {
    assert.equal(rupees(bad), "\u2014");
    assert.equal(plain(bad), "\u2014");
    assert.equal(percent(bad), "\u2014");
    assert.equal(factor(bad), "\u2014");
  }
});

test("percent, factor and plain", () => {
  assert.equal(percent(0.5556), "55.6%");
  assert.equal(percent(1), "100.0%");
  assert.equal(percent(0), "0.0%");
  assert.equal(factor(1.4167), "1.42");
  assert.equal(factor(null, true), "\u221e");
  assert.equal(plain(24150.55, 2), "24,150.55");
  assert.equal(plain(100000), "1,00,000");
});

test("tone colours by sign and leaves zero and unknowns neutral", () => {
  assert.equal(tone(5), "pos");
  assert.equal(tone(-5), "neg");
  assert.equal(tone(0), "");
  assert.equal(tone(null), "");
});

test("dates and clock times are read from the text, not the browser time zone", () => {
  assert.equal(shortDate("2026-05-12"), "12 May 2026");
  assert.equal(shortDate("2026-05-12T09:45:00", false), "12 May");
  assert.equal(shortDate("2026-01-01"), "1 Jan 2026");
  assert.equal(shortDate("2026-13-01"), "\u2014");
  assert.equal(shortDate(null), "\u2014");
  assert.equal(clock("2026-05-12T09:45:00"), "09:45");
  assert.equal(clock("nonsense"), "\u2014");
});

test("wallSeconds treats wall-clock time as UTC so charts show exchange time", () => {
  assert.equal(wallSeconds("1970-01-01T00:01:00"), 60);
  assert.equal(wallSeconds("2026-05-12T09:15:00") - wallSeconds("2026-05-12T09:14:00"), 60);
  assert.equal(wallSeconds("2026-05-12 09:15"), wallSeconds("2026-05-12T09:15:00"));
  assert.equal(wallSeconds("garbage"), null);
  assert.equal(wallSeconds(undefined), null);
});

const bars5 = ["09:15", "09:20", "09:25", "09:30"].map((t) => [`2026-05-12T${t}:00`]);

test("a decision at a bar's close belongs to the bar that just closed", () => {
  assert.equal(barForTime(bars5, "2026-05-12T09:25:00", 5), wallSeconds("2026-05-12T09:20:00"));
  assert.equal(barForTime(bars5, "2026-05-12T09:20:00", 5), wallSeconds("2026-05-12T09:15:00"));
});

test("a time inside a bar belongs to that bar", () => {
  assert.equal(barForTime(bars5, "2026-05-12T09:27:00", 5), wallSeconds("2026-05-12T09:25:00"));
});

test("the 15:00 square-off lands in the 14:45 bar of a 30-minute chart", () => {
  const bars30 = ["14:15", "14:45"].map((t) => [`2026-05-12T${t}:00`]);
  assert.equal(barForTime(bars30, "2026-05-12T15:00:00", 30), wallSeconds("2026-05-12T14:45:00"));
});

test("a time with no bar around it has no marker position", () => {
  assert.equal(barForTime(bars5, "2026-05-12T09:15:00", 5), null); // the open itself
  assert.equal(barForTime(bars5, "2026-05-12T11:00:00", 5), null);
  assert.equal(barForTime([], "2026-05-12T09:25:00", 5), null);
  assert.equal(barForTime(bars5, "bad", 5), null);
});

test("moveSelection moves one row, and clamps at both ends", () => {
  assert.equal(moveSelection(2, 5, "ArrowDown"), 3);
  assert.equal(moveSelection(2, 5, "ArrowUp"), 1);
  assert.equal(moveSelection(4, 5, "ArrowDown"), 4);
  assert.equal(moveSelection(0, 5, "ArrowUp"), 0);
  assert.equal(moveSelection(2, 5, "j"), 3);
  assert.equal(moveSelection(2, 5, "k"), 1);
});

test("moveSelection supports paging, Home and End", () => {
  assert.equal(moveSelection(2, 50, "PageDown"), 12);
  assert.equal(moveSelection(45, 50, "PageDown"), 49);
  assert.equal(moveSelection(12, 50, "PageUp"), 2);
  assert.equal(moveSelection(3, 50, "PageUp"), 0);
  assert.equal(moveSelection(7, 50, "Home"), 0);
  assert.equal(moveSelection(7, 50, "End"), 49);
});

test("moveSelection with nothing selected starts at the top", () => {
  assert.equal(moveSelection(-1, 5, "ArrowDown"), 0);
  assert.equal(moveSelection(-1, 5, "ArrowUp"), 0);
});

test("moveSelection on an empty list stays empty, and ignores other keys", () => {
  for (const key of ["ArrowDown", "ArrowUp", "Home", "End", "PageDown"]) {
    assert.equal(moveSelection(0, 0, key), -1);
  }
  assert.equal(moveSelection(2, 5, "a"), 2);
  assert.equal(moveSelection(2, 5, "Enter"), 2);
});

test("uniqueAscending sorts, keeps the last of equal times and drops bad points", () => {
  const points = [
    { time: 3, value: 30 },
    { time: 1, value: 10 },
    { time: 2, value: 20 },
    { time: 2, value: 25 },
    { time: NaN, value: 1 },
    { time: 4, value: null },
    null,
  ];
  assert.deepEqual(uniqueAscending(points), [
    { time: 1, value: 10 },
    { time: 2, value: 25 },
    { time: 3, value: 30 },
  ]);
  assert.deepEqual(uniqueAscending([]), []);
});

test("a default filter set gives a clean URL and a full API query", () => {
  assert.equal(toQuery(DEFAULT_FILTERS), "");
  assert.equal(countActiveFilters(DEFAULT_FILTERS), 0);
  const api = new URLSearchParams(toApiQuery(DEFAULT_FILTERS));
  assert.equal(api.get("timeframe"), "15");
  assert.equal(api.get("cutoff"), "14:30");
  assert.equal(api.has("from"), false);
});

test("only non-default filters reach the URL, and they round-trip", () => {
  const filters = { ...DEFAULT_FILTERS, timeframe: "5", mode: "atm_sell", from: "2026-01-02" };
  const query = toQuery(filters);
  assert.deepEqual(Object.fromEntries(new URLSearchParams(query)), {
    timeframe: "5",
    mode: "atm_sell",
    from: "2026-01-02",
  });
  assert.deepEqual(fromQuery(`?${query}`), filters);
  assert.equal(countActiveFilters(filters), 3);
});

test("fromQuery ignores values the page does not offer", () => {
  const allowed = { timeframe: ["5", "15", "30"], mode: ["all", "atm_buy"] };
  const result = fromQuery("?timeframe=7&mode=atm_buy&outcome=win&bogus=1", allowed);
  assert.equal(result.timeframe, "15");
  assert.equal(result.mode, "atm_buy");
  assert.equal(result.outcome, "win");
  assert.equal("bogus" in result, false);
});

test("labels fall back to the raw value rather than hiding it", () => {
  assert.equal(exitReasonLabel("square_off"), "15:00 square-off");
  assert.equal(exitReasonLabel("weird"), "weird");
});

test("prettySymbol makes option symbols readable and leaves others alone", () => {
  assert.equal(prettySymbol("NSE-NIFTY-12May26-24000-CE"), "NIFTY 12 May 26 24000 CE");
  assert.equal(prettySymbol("NSE-NIFTY"), "NSE-NIFTY");
  assert.equal(prettySymbol(null), "\u2014");
});

test("snapTime picks the candle at or just before a fill", () => {
  const times = [60, 120, 180];
  assert.equal(snapTime(times, 120), 120);
  assert.equal(snapTime(times, 150), 120);
  assert.equal(snapTime(times, 999), 180);
  assert.equal(snapTime(times, 10), 60); // before the first candle: use the first
  assert.equal(snapTime([], 10), null);
  assert.equal(snapTime(times, null), null);
});

test("ordinal handles the teens and the usual suffixes", () => {
  const cases = { 1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 8: "8th", 11: "11th", 12: "12th", 13: "13th", 21: "21st", 22: "22nd", 23: "23rd", 101: "101st", 111: "111th" };
  for (const [n, text] of Object.entries(cases)) assert.equal(ordinal(Number(n)), text);
});

test("every day is on by default, so a bare URL stays clean", () => {
  assert.equal(DEFAULT_FILTERS.days, "mon,tue,wed,thu,fri,sat,sun");
  assert.equal(toQuery(DEFAULT_FILTERS).includes("days"), false);
  assert.equal(new URLSearchParams(toApiQuery(DEFAULT_FILTERS)).get("days"), DEFAULT_FILTERS.days);
});

test("normalizeDays orders, lower-cases and falls back to every day", () => {
  assert.equal(normalizeDays("thu, MON"), "mon,thu");
  assert.equal(normalizeDays("sun,sat"), "sat,sun");
  assert.equal(normalizeDays(""), DEFAULT_FILTERS.days);
  assert.equal(normalizeDays(null), DEFAULT_FILTERS.days);
  assert.equal(normalizeDays("funday"), DEFAULT_FILTERS.days);
  assert.equal(normalizeDays("mon,mon,xyz"), "mon");
});

test("toggleDay switches one day and refuses to leave none", () => {
  assert.equal(toggleDay(DEFAULT_FILTERS.days, "mon"), "tue,wed,thu,fri,sat,sun");
  assert.equal(toggleDay("tue,wed", "mon"), "mon,tue,wed");
  assert.equal(toggleDay("wed", "sun"), "wed,sun");
  assert.equal(toggleDay("wed", "wed"), null);
});

test("a day list round-trips through the URL, and a bad one is ignored", () => {
  const filters = { ...DEFAULT_FILTERS, days: "tue,thu" };
  assert.equal(new URLSearchParams(toQuery(filters)).get("days"), "tue,thu");
  assert.equal(fromQuery(`?${toQuery(filters)}`).days, "tue,thu");
  assert.equal(fromQuery("?days=funday").days, DEFAULT_FILTERS.days);
  assert.equal(countActiveFilters(filters), 1);
});

test("describeDays says nothing when every day is on", () => {
  assert.equal(describeDays(DEFAULT_FILTERS.days), "");
  assert.equal(describeDays("mon,tue,wed,thu,fri"), "Mon\u2013Fri only");
  assert.equal(describeDays("mon,wed"), "Mon, Wed only");
});
