/*
 * The backtest "server" for the static website: answers the same four requests as /api/backtest/*
 * from JSON files written by scripts/build_site.py, doing in the browser what query.py,
 * metrics.py and present.py do on the server. tests/test_static_api.py runs both on the same
 * trades and requires identical JSON, so any change here must keep matching those three files.
 *
 * Money is exact: every amount is an integer count of 1/10^decimals rupee (BigInt), sums are exact,
 * and rounding to paise is round-half-even, which is what Python's Decimal does.
 */

import { ApiError } from "./api.js";

const TIMEFRAMES = [5, 15, 30];
const START_OPTIONS = ["09:30", "10:00", "10:30"];
const CUTOFF_OPTIONS = ["11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30"];
const MAX_CROSS_NO = 20;
const DAY_NAMES = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]; // index = weekday, Monday = 0
const OUTCOMES = ["all", "win", "loss", "tie"];
const MODES = ["itm5_buy", "atm_buy", "atm_sell"];
const STARTING_CAPITAL = 100000n;

const DATA = "data/";

// ------------------------------------------------------------------------ filters

function badFilter(message) {
  return new ApiError(message, "bad_filter", 400);
}

function parseTime(value, name) {
  if (!/^\d{2}:\d{2}(:\d{2})?$/.test(value)) throw badFilter(`${name} must look like 09:30`);
  return value.length === 5 ? `${value}:00` : value;
}

function validDay(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const moment = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(moment.getTime()) && moment.toISOString().slice(0, 10) === value;
}

function parseDay(value, name) {
  if (!validDay(value)) throw badFilter(`${name} must look like 2026-05-12`);
  return value;
}

function parseWhole(value, name) {
  if (!/^[+-]?\d+$/.test(value)) throw badFilter(`${name} must be a whole number`);
  return Number.parseInt(value, 10);
}

function parseDays(value) {
  const chosen = new Set();
  for (const part of value.toLowerCase().split(",")) {
    const index = DAY_NAMES.indexOf(part.trim());
    if (index < 0) throw badFilter(`days must be a comma-separated list of ${DAY_NAMES.join(", ")}`);
    chosen.add(index);
  }
  return chosen;
}

/** Same rules as query.parse_filters; the first value of a repeated key wins, empty means default. */
export function parseFilters(params) {
  const get = {};
  const seen = new Set();
  for (const [key, raw] of params) {
    if (seen.has(key)) continue;
    seen.add(key);
    const value = raw.trim();
    if (value) get[key] = value;
  }

  let mode = null;
  if ("mode" in get && get.mode !== "all") {
    if (!MODES.includes(get.mode)) throw badFilter(`mode must be one of all, ${MODES.join(", ")}`);
    mode = get.mode;
  }
  const filters = {
    timeframe: "timeframe" in get ? parseWhole(get.timeframe, "timeframe") : 15,
    mode,
    outcome: get.outcome ?? "all",
    start: "start" in get ? parseTime(get.start, "start") : `${START_OPTIONS[0]}:00`,
    cutoff: "cutoff" in get ? parseTime(get.cutoff, "cutoff") : `${CUTOFF_OPTIONS.at(-1)}:00`,
    cross: "cross" in get && get.cross !== "all" ? parseWhole(get.cross, "cross") : null,
    days: "days" in get ? parseDays(get.days) : new Set([0, 1, 2, 3, 4, 5, 6]),
    dateFrom: "from" in get ? parseDay(get.from, "from") : null,
    dateTo: "to" in get ? parseDay(get.to, "to") : null,
  };

  if (!TIMEFRAMES.includes(filters.timeframe)) {
    throw badFilter(`timeframe must be one of ${TIMEFRAMES.join(", ")}`);
  }
  if (!START_OPTIONS.some((t) => `${t}:00` === filters.start)) {
    throw badFilter(`start must be one of ${START_OPTIONS.join(", ")}`);
  }
  if (!CUTOFF_OPTIONS.some((t) => `${t}:00` === filters.cutoff)) {
    throw badFilter(`cutoff must be one of ${CUTOFF_OPTIONS.join(", ")}`);
  }
  if (!OUTCOMES.includes(filters.outcome)) throw badFilter(`outcome must be one of ${OUTCOMES.join(", ")}`);
  if (filters.cross !== null && !(filters.cross >= 1 && filters.cross <= MAX_CROSS_NO)) {
    throw badFilter(`cross must be between 1 and ${MAX_CROSS_NO}`);
  }
  if (filters.dateFrom && filters.dateTo && filters.dateFrom > filters.dateTo) {
    throw badFilter("'from' must not be after 'to'");
  }
  return filters;
}

const weekday = (day) => (new Date(`${day}T00:00:00Z`).getUTCDay() + 6) % 7;
const timeOf = (moment) => moment.slice(11, 19);
const outcomeOf = (trade) => (trade.net > 0n ? "win" : trade.net < 0n ? "loss" : "tie");

/** query.select: the trades that pass, each with its id (position in the run) and entry number. */
export function select(trades, filters, { ignoreMode = false } = {}) {
  const inWindow = [];
  trades.forEach((trade, index) => {
    const entry = timeOf(trade.entry_decision);
    if (entry < filters.start || entry > filters.cutoff) return;
    if (!filters.days.has(weekday(trade.date))) return;
    if (filters.dateFrom && trade.date < filters.dateFrom) return;
    if (filters.dateTo && trade.date > filters.dateTo) return;
    inWindow.push([index, trade]);
  });

  const counters = new Map();
  const selected = [];
  for (const [index, trade] of inWindow) {
    const key = `${trade.mode}|${trade.date}`;
    const entryNo = (counters.get(key) ?? 0) + 1;
    counters.set(key, entryNo);
    if (filters.cross !== null && entryNo !== filters.cross) continue;
    if (filters.outcome !== "all" && outcomeOf(trade) !== filters.outcome) continue;
    if (!ignoreMode && filters.mode !== null && trade.mode !== filters.mode) continue;
    selected.push({ index, trade, entryNo });
  }
  return selected;
}

// ------------------------------------------------------------------- exact numbers

function parseMoney(text, decimals) {
  const negative = text.startsWith("-");
  const [whole, fraction = ""] = (negative ? text.slice(1) : text).split(".");
  if (fraction.length > decimals) throw new Error(`more than ${decimals} decimals in ${text}`);
  const value = BigInt(whole + fraction.padEnd(decimals, "0"));
  return negative ? -value : value;
}

/** n / d rounded to the nearest integer, ties to even (d > 0). */
function divideHalfEven(n, d) {
  const negative = n < 0n;
  const magnitude = negative ? -n : n;
  let quotient = magnitude / d;
  const twice = (magnitude % d) * 2n;
  if (twice > d || (twice === d && quotient % 2n === 1n)) quotient += 1n;
  return negative ? -quotient : quotient;
}

function centsToNumber(cents) {
  const negative = cents < 0n;
  const magnitude = negative ? -cents : cents;
  const text = `${magnitude / 100n}.${(magnitude % 100n).toString().padStart(2, "0")}`;
  return Number(negative ? `-${text}` : text);
}

/** Python's round(x, digits) for a float: the exact value of the double, ties to even. */
export function roundHalfEven(x, digits) {
  if (!Number.isFinite(x)) return x;
  const negative = x < 0;
  const [whole, fraction] = Math.abs(x).toFixed(100).split(".");
  const kept = fraction.slice(0, digits);
  const rest = fraction.slice(digits);
  const tail = /[1-9]/.test(rest.slice(1));
  const lastDigit = Number((digits > 0 ? kept : whole).slice(-1));
  const up = rest[0] > "5" || (rest[0] === "5" && (tail || lastDigit % 2 === 1));
  const scaled = BigInt(whole + kept) + (up ? 1n : 0n);
  const text = scaled.toString().padStart(digits + 1, "0");
  const result = digits > 0 ? `${text.slice(0, -digits)}.${text.slice(-digits)}` : text;
  return Number(negative ? `-${result}` : result);
}

// ----------------------------------------------------------------------- metrics

function compareExit(a, b) {
  // metrics.equity_curve: exit time, then entry time, then mode name
  for (const key of ["exit_fill", "entry_fill", "mode"]) {
    if (a[key] < b[key]) return -1;
    if (a[key] > b[key]) return 1;
  }
  return 0;
}

const sum = (values) => values.reduce((total, value) => total + value, 0n);

function equityCurve(trades) {
  let total = 0n;
  return [...trades].sort(compareExit).map((trade) => {
    total += trade.net;
    return [trade, total];
  });
}

function metricsJson(trades, scale) {
  const rupees = (value) => (value === null ? null : centsToNumber(divideHalfEven(value * 100n, scale)));
  const rupeesOver = (value, count) => centsToNumber(divideHalfEven(value * 100n, scale * BigInt(count)));

  const nets = trades.map((trade) => trade.net);
  const wins = nets.filter((n) => n > 0n);
  const losses = nets.filter((n) => n < 0n);
  const count = nets.length;
  const grossWin = sum(wins);
  const grossLoss = -sum(losses);
  const net = sum(nets);

  const capital = STARTING_CAPITAL * scale;
  let peak = 0n;
  let drawdown = 0n;
  let drawdownPct = 0;
  let streak = 0;
  let longest = 0;
  for (const [trade, equity] of equityCurve(trades)) {
    if (equity > peak) peak = equity;
    const fall = peak - equity;
    if (fall > drawdown) drawdown = fall;
    const base = capital + peak;
    if (base > 0n) drawdownPct = Math.max(drawdownPct, Number(fall * 100n) / Number(base));
    streak = trade.net < 0n ? streak + 1 : 0;
    longest = Math.max(longest, streak);
  }

  const profitFactor = grossLoss > 0n ? Number(grossWin) / Number(grossLoss) : null;
  const payoff =
    wins.length && losses.length
      ? Number(grossWin * BigInt(losses.length)) / Number(grossLoss * BigInt(wins.length))
      : null;

  return {
    trades: count,
    wins: wins.length,
    losses: losses.length,
    ties: count - wins.length - losses.length,
    win_rate: count ? roundHalfEven(wins.length / count, 4) : null,
    gross_pnl: rupees(sum(trades.map((t) => t.gross))),
    slippage: rupees(sum(trades.map((t) => t.slippage))),
    charges: rupees(sum(trades.map((t) => t.charges))),
    net_pnl: rupees(net),
    profit_factor: profitFactor === null ? null : roundHalfEven(profitFactor, 3),
    profit_factor_infinite: wins.length > 0 && losses.length === 0,
    payoff_ratio: payoff === null ? null : roundHalfEven(payoff, 3),
    avg_win: wins.length ? rupeesOver(grossWin, wins.length) : null,
    avg_loss: losses.length ? rupeesOver(-grossLoss, losses.length) : null,
    expectancy: count ? rupeesOver(net, count) : null,
    max_drawdown: rupees(drawdown),
    max_drawdown_pct: count ? roundHalfEven(drawdownPct, 2) : null,
    best_trade: count ? rupees(nets.reduce((a, b) => (b > a ? b : a))) : null,
    worst_trade: count ? rupees(nets.reduce((a, b) => (b < a ? b : a))) : null,
    max_consecutive_losses: longest,
  };
}

// ------------------------------------------------------------------------- answers

function tradesAnswer(bundle, filters) {
  const scale = bundle.scale;
  const chosen = select(bundle.trades, filters);
  const everyMode = select(bundle.trades, filters, { ignoreMode: true });
  const chosenTrades = chosen.map((s) => s.trade);
  const rupees = (value) => centsToNumber(divideHalfEven(value * 100n, scale));

  const compare = {};
  for (const mode of MODES) {
    compare[mode] = metricsJson(
      everyMode.filter((s) => s.trade.mode === mode).map((s) => s.trade),
      scale,
    );
  }

  let total = 0n;
  const equity = [...chosenTrades].sort(compareExit).map((trade) => {
    total += trade.net;
    return [trade.exit_fill, rupees(total)];
  });

  return {
    run: bundle.run,
    timeframe: bundle.timeframe,
    range: bundle.range,
    computed: bundle.computed,
    kpis: metricsJson(chosenTrades, scale),
    compare,
    equity,
    trades: chosen.map(({ index, trade, entryNo }) => ({
      id: index,
      date: trade.date,
      entry: trade.entry_fill.slice(11, 16),
      exit: trade.exit_fill.slice(11, 16),
      mode: trade.mode,
      direction: trade.direction,
      side: trade.side,
      option: trade.option,
      net: rupees(trade.net),
      outcome: outcomeOf(trade),
      exit_reason: trade.exit_reason,
      whipsaw: trade.whipsaw,
      entry_no: entryNo,
    })),
  };
}

function count(values) {
  const counts = {};
  for (const value of values) counts[value] = (counts[value] ?? 0) + 1;
  return counts;
}

function skipsAnswer(bundle, from, to) {
  const inside = (day) => (from === null || day >= from) && (to === null || day <= to);
  const skips = bundle.skips.filter((s) => inside(s.date));
  const days = bundle.days.filter((d) => inside(d.date));
  return {
    run: bundle.run,
    timeframe: bundle.timeframe,
    skipped_trades: skips.map((s) => ({
      date: s.date,
      mode: s.mode,
      decision: s.decision,
      reason: s.reason,
      detail: s.detail,
    })),
    skip_reasons: count(skips.map((s) => s.reason)),
    day_counts: count(days.map((d) => d.kind)),
    incomplete_days: days
      .filter((d) => d.kind === "incomplete_spot")
      .map((d) => ({ date: d.date, detail: d.detail })),
  };
}

// --------------------------------------------------------------------------- files

async function readJson(path, signal) {
  let response;
  try {
    response = await fetch(path, { signal });
  } catch (error) {
    if (error?.name === "AbortError") throw error;
    throw new ApiError("Could not load the backtest data. Check your connection and try again.", "network", 0);
  }
  if (response.status === 404) throw new ApiError("That part of the backtest is not available.", "not_found", 404);
  if (!response.ok) throw new ApiError(`The server answered ${response.status}.`, "http_error", response.status);
  try {
    return await response.json();
  } catch {
    throw new ApiError("The backtest data could not be read.", "bad_json", response.status);
  }
}

const bundles = new Map();

function loadBundle(timeframe, signal) {
  if (!bundles.has(timeframe)) {
    const promise = readJson(`${DATA}tf-${timeframe}.json`, signal).then((bundle) => {
      const scale = 10n ** BigInt(bundle.decimals);
      return {
        ...bundle,
        scale,
        trades: bundle.trades.map((t) => ({
          ...t,
          gross: parseMoney(t.gross, bundle.decimals),
          slippage: parseMoney(t.slippage, bundle.decimals),
          charges: parseMoney(t.charges, bundle.decimals),
          net: parseMoney(t.net, bundle.decimals),
        })),
      };
    });
    promise.catch(() => bundles.delete(timeframe)); // a failed or aborted load may be retried
    bundles.set(timeframe, promise);
  }
  return bundles.get(timeframe);
}

/** Forget what was loaded (used by the tests). */
export function reset() {
  bundles.clear();
}

/** The same contract as api.getJson for the /api/backtest/* URLs. */
export async function staticGet(url, { signal } = {}) {
  const [path, query = ""] = url.split("?");
  const params = new URLSearchParams(query);

  if (path === "/api/backtest/meta") return readJson(`${DATA}meta.json`, signal);

  if (path === "/api/backtest/trades") {
    const filters = parseFilters(params);
    return tradesAnswer(await loadBundle(filters.timeframe, signal), filters);
  }

  const detail = /^\/api\/backtest\/trade\/(\d+)\/(\d+)$/.exec(path);
  if (detail) {
    const timeframe = Number(detail[1]);
    const index = Number(detail[2]);
    const filters = parseFilters(new URLSearchParams([...params, ["timeframe", String(timeframe)]]));
    const bundle = await loadBundle(filters.timeframe, signal);
    if (index >= bundle.trades.length) {
      throw new ApiError("That trade is not in the current results. Reload the page.", "results_changed", 409);
    }
    const answer = await readJson(`${DATA}detail/${timeframe}/${index}.json`, signal);
    const found = select(bundle.trades, filters, { ignoreMode: true }).find((s) => s.index === index);
    answer.trade.entry_no = found ? found.entryNo : 0;
    return answer;
  }

  if (path === "/api/backtest/skips") {
    const timeframe = parseFilters(new URLSearchParams([["timeframe", params.get("timeframe") ?? ""]])).timeframe;
    const dayArg = (name) => {
      const value = (params.get(name) ?? "").trim();
      if (!value) return null;
      if (!validDay(value)) throw new ApiError(`${name} must look like 2026-05-12`, "bad_request", 400);
      return value;
    };
    return skipsAnswer(await loadBundle(timeframe, signal), dayArg("from"), dayArg("to"));
  }

  throw new ApiError("This page shows a read-only copy of the backtest.", "not_available", 404);
}
