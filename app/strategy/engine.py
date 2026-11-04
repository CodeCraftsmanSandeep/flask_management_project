"""The 9/21 EMA crossover options simulation. Every ambiguity is decided here, once.

Signal
    9 EMA crosses 21 EMA on *closed* spot bars (see :mod:`.bars`). The bar's close time is the
    **decision time**: the earliest moment the signal is known.

Fill (no look-ahead)
    A trade is filled at the **open of the option's 1-minute candle that starts at the decision
    time** (so a 5-minute bar covering 09:25-09:30 decides at 09:30 and fills at the 09:30 open),
    then slippage is applied. If that candle is missing the first later candle within
    ``fill_wait_minutes`` is used; if none exists the trade is skipped and listed, never faked.

Entry window
    A cross becomes an entry only if its decision time is within ``[start, cutoff]`` *inclusive*.
    Crosses outside are ignored for entries. A cross before ``start`` does not carry over into a
    trade at ``start``. The cutoff only blocks new entries: a trade open at the cutoff stays open.

Exit
    The next opposite crossover (filled at that decision time), or the mandatory square-off at
    ``square_off`` (15:00) filled at that minute's open. A cross whose decision time is 15:00 or
    later is not a cross at all: the square-off happens. There is no stop-loss and no target.

Immediate reversal
    Crosses alternate, so the next cross is always the opposite one. If it is inside the window it
    closes the trade and opens the reverse trade in the same minute (two separate orders, each
    with its own slippage and charges). Outside the window it only closes.

Because an in-window cross always opens a trade and a trade always ends at the next cross or the
square-off, the trades for *any* window are a subset of the trades for the widest window. The
engine therefore simulates the widest window once (the "population") and the window is just a
filter, which is what makes the UI instant. ``tests/test_engine_window.py`` proves the equivalence
against an independent state machine.

Modes (see :mod:`.options`), one position at a time per mode, flat by the square-off, 1 lot of 65.
"""

import hashlib
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Protocol

from .bars import MINUTES_PER_SESSION, find_crosses, in_session, resample, with_emas
from .costs import DEFAULT_SCHEDULE, CostSchedule
from .fills import TICK, buy_fill, sell_fill
from .models import (
    Candle,
    Cross,
    ExitReason,
    Mode,
    Side,
    SignalBar,
    Skip,
    SkipReason,
    Trade,
)
from .options import OptionLeg, atm_strike, groww_symbol, leg_for, pick_expiry

ENGINE_VERSION = "1"


@dataclass(frozen=True, slots=True)
class StrategyParams:
    timeframe: int = 5
    fast: int = 9
    slow: int = 21
    reset_daily: bool = False
    square_off: time = time(15, 0)
    earliest_entry: time = time(9, 30)  # widest window the population covers
    latest_entry: time = time(14, 30)
    lot_size: int = 65
    lots: int = 1
    slippage_pct: Decimal = Decimal("0.005")  # 0.5 % of premium, per leg
    slippage_points: Decimal = Decimal("0.5")  # fixed points per leg
    skip_expiry_day: bool = False
    fill_wait_minutes: int = 5
    whipsaw_bars: int = 2

    def __post_init__(self) -> None:
        problems = []
        if not 1 <= self.timeframe <= MINUTES_PER_SESSION:
            problems.append("timeframe must be 1..375 minutes")
        if not 1 <= self.fast < self.slow:
            problems.append("fast EMA period must be shorter than the slow one")
        if not self.earliest_entry <= self.latest_entry < self.square_off:
            problems.append("need earliest_entry <= latest_entry < square_off")
        if self.lot_size < 1 or self.lots < 1:
            problems.append("lot size and lots must be positive")
        if self.slippage_pct < 0 or self.slippage_points < 0:
            problems.append("slippage cannot be negative")
        if self.fill_wait_minutes < 0 or self.whipsaw_bars < 0:
            problems.append("fill_wait_minutes and whipsaw_bars cannot be negative")
        if problems:
            raise ValueError("; ".join(problems))

    @property
    def quantity(self) -> int:
        return self.lot_size * self.lots

    def fingerprint(self) -> str:
        return hashlib.sha256(repr(self).encode()).hexdigest()[:12]


class OptionBook(Protocol):
    """Where option candles come from (the database in production, a dict in tests)."""

    def candles(self, symbol: str, day: date) -> Mapping[datetime, Candle]:
        """1-minute candles for *symbol* on *day*, keyed by candle start. Empty if none."""
        ...


@dataclass(frozen=True, slots=True)
class DayOutcome:
    day: date
    kind: str  # "ok" | "no_signal" | "incomplete_spot"
    detail: str = ""


@dataclass(slots=True)
class SpotAnalysis:
    complete_days: dict[date, list[Candle]]
    incomplete_days: dict[date, int]  # day -> number of candles present
    signal_bars: list[SignalBar]
    crosses: list[Cross]


@dataclass(slots=True)
class BacktestResult:
    params: StrategyParams
    analysis: SpotAnalysis
    trades: list[Trade] = field(default_factory=list)
    skips: list[Skip] = field(default_factory=list)
    days: list[DayOutcome] = field(default_factory=list)


def analyse_spot(spot: Sequence[Candle], params: StrategyParams) -> SpotAnalysis:
    """Group by day, drop incomplete days, then build bars, EMAs and crosses.

    A day is *complete* only if all 375 regular-session minutes are present. Incomplete days feed
    neither the EMAs nor the trades (a half-missing day would distort every later signal) and are
    reported instead.
    """
    by_day: dict[date, list[Candle]] = defaultdict(list)
    previous: datetime | None = None
    for candle in spot:
        if previous is not None and candle.start <= previous:
            msg = f"spot candles must be strictly increasing; {candle.start} follows {previous}"
            raise ValueError(msg)
        previous = candle.start
        if in_session(candle.start):
            by_day[candle.start.date()].append(candle)

    complete = {d: c for d, c in by_day.items() if len(c) == MINUTES_PER_SESSION}
    incomplete = {d: len(c) for d, c in by_day.items() if len(c) != MINUTES_PER_SESSION}
    good = [c for d in sorted(complete) for c in complete[d]]
    bars = with_emas(
        resample(good, params.timeframe), params.fast, params.slow, reset_daily=params.reset_daily
    )
    return SpotAnalysis(
        complete, incomplete, bars, find_crosses(bars, reset_daily=params.reset_daily)
    )


def is_entry_candidate(cross: Cross, params: StrategyParams) -> bool:
    return params.earliest_entry <= cross.decision_time.time() <= params.latest_entry


def contract_for(mode: Mode, cross: Cross, expiry: date) -> tuple[OptionLeg, str]:
    """The option leg and Groww symbol this cross trades in this mode."""
    leg = leg_for(mode, cross.direction, atm_strike(cross.close))
    return leg, groww_symbol(expiry, leg.strike, leg.option_type)


def required_contracts(
    analysis: SpotAnalysis,
    expiries: Sequence[date],
    params: StrategyParams,
    modes: Sequence[Mode] = tuple(Mode),
    start: date | None = None,
    end: date | None = None,
) -> dict[date, set[str]]:
    """Which option contracts need 1-minute data on which days. Drives the downloader."""
    needed: dict[date, set[str]] = defaultdict(set)
    for cross in analysis.crosses:
        day = cross.decision_time.date()
        if day not in analysis.complete_days or not is_entry_candidate(cross, params):
            continue
        if (start and day < start) or (end and day > end):
            continue
        expiry = pick_expiry(day, expiries, skip_expiry_day=params.skip_expiry_day)
        if expiry is None:
            continue
        for mode in modes:
            needed[day].add(contract_for(mode, cross, expiry)[1])
    return dict(needed)


def _first_at_or_after(
    candles: Mapping[datetime, Candle], moment: datetime, wait_minutes: int
) -> Candle | None:
    for step in range(wait_minutes + 1):
        found = candles.get(moment + timedelta(minutes=step))
        if found is not None:
            return found
    return None


def _last_before(candles: Mapping[datetime, Candle], moment: datetime) -> Candle | None:
    earlier = [start for start in candles if start < moment]
    return candles[max(earlier)] if earlier else None


def run_backtest(
    spot: Sequence[Candle],
    expiries: Sequence[date],
    book: OptionBook,
    params: StrategyParams,
    *,
    schedule: CostSchedule = DEFAULT_SCHEDULE,
    modes: Sequence[Mode] = tuple(Mode),
    start: date | None = None,
    end: date | None = None,
    analysis: SpotAnalysis | None = None,
) -> BacktestResult:
    """Simulate every mode over every complete day in ``[start, end]``.

    Days before *start* still feed the EMAs (warm-up) but are not traded. Pass *analysis* (from
    :func:`analyse_spot` on the same *spot* and *params*) to avoid computing it twice.
    """
    analysis = analysis or analyse_spot(spot, params)
    result = BacktestResult(params=params, analysis=analysis)
    expiry_list = sorted(set(expiries))

    crosses_by_day: dict[date, list[Cross]] = defaultdict(list)
    for cross in analysis.crosses:
        crosses_by_day[cross.decision_time.date()].append(cross)

    for day in sorted(set(analysis.complete_days) | set(analysis.incomplete_days)):
        if (start and day < start) or (end and day > end):
            continue
        if day in analysis.incomplete_days:
            have = analysis.incomplete_days[day]
            result.days.append(
                DayOutcome(day, "incomplete_spot", f"{have}/{MINUTES_PER_SESSION} minutes")
            )
            continue
        day_crosses = crosses_by_day.get(day, [])
        candidates = [c for c in day_crosses if is_entry_candidate(c, params)]
        if not candidates:
            result.days.append(DayOutcome(day, "no_signal", "no crossover inside 09:30-14:30"))
            continue
        result.days.append(DayOutcome(day, "ok"))
        expiry = pick_expiry(day, expiry_list, skip_expiry_day=params.skip_expiry_day)
        spot_by_minute = {c.start: c for c in analysis.complete_days[day]}
        for mode in modes:
            _simulate_mode(
                result, day, day_crosses, spot_by_minute, expiry, book, params, schedule, mode
            )

    result.trades.sort(key=lambda t: (t.entry_fill, t.mode.value))
    return result


def _simulate_mode(
    result: BacktestResult,
    day: date,
    day_crosses: Sequence[Cross],
    spot_by_minute: Mapping[datetime, Candle],
    expiry: date | None,
    book: OptionBook,
    params: StrategyParams,
    schedule: CostSchedule,
    mode: Mode,
) -> None:
    square_off = datetime.combine(day, params.square_off)
    live_crosses = [c for c in day_crosses if c.decision_time < square_off]
    wait = params.fill_wait_minutes
    qty = params.quantity
    rates = schedule.rates_on(day)

    for index, cross in enumerate(live_crosses):
        if not is_entry_candidate(cross, params):
            continue

        def skip(reason: SkipReason, detail: str, cross: Cross = cross) -> None:
            result.skips.append(Skip(day, mode, cross.decision_time, reason, detail))

        if expiry is None:
            skip(SkipReason.NO_EXPIRY, "no expiry listed on or after this date")
            continue

        leg, symbol = contract_for(mode, cross, expiry)
        if index + 1 < len(live_crosses):
            exit_decision = live_crosses[index + 1].decision_time
            reason = ExitReason.OPPOSITE_CROSS
        else:
            exit_decision, reason = square_off, ExitReason.SQUARE_OFF

        option_candles = book.candles(symbol, day)
        entry_candle = _first_at_or_after(option_candles, cross.decision_time, wait)
        if entry_candle is None or entry_candle.start >= exit_decision:
            skip(SkipReason.NO_OPTION_DATA, f"no {symbol} candle near {cross.decision_time:%H:%M}")
            continue
        exit_candle = _first_at_or_after(option_candles, exit_decision, wait)
        stale = exit_candle is None
        if exit_candle is None:
            exit_candle = _last_before(option_candles, exit_decision)
        if exit_candle is None:  # cannot happen (the entry candle is earlier) but never guess
            skip(SkipReason.NO_OPTION_DATA, f"no {symbol} exit price")
            continue

        entry_raw = entry_candle.open
        exit_raw = exit_candle.close if stale else exit_candle.open
        if entry_raw < TICK or exit_raw < TICK:
            skip(SkipReason.BAD_PRICE, f"{symbol} price below the {TICK} tick")
            continue

        spot_in = _first_at_or_after(spot_by_minute, cross.decision_time, wait)
        spot_out = _first_at_or_after(spot_by_minute, exit_decision, wait) or _last_before(
            spot_by_minute, exit_decision
        )
        if spot_in is None or spot_out is None:
            skip(SkipReason.NO_SPOT_DATA, f"no NIFTY candle near {cross.decision_time:%H:%M}")
            continue

        buying = leg.side is Side.BUY
        pct, points = params.slippage_pct, params.slippage_points
        if buying:
            entry_fill = buy_fill(entry_raw, pct, points)
            exit_fill = sell_fill(exit_raw, pct, points)
            gross = (exit_raw - entry_raw) * qty
            slippage = ((entry_fill - entry_raw) + (exit_raw - exit_fill)) * qty
        else:
            entry_fill = sell_fill(entry_raw, pct, points)
            exit_fill = buy_fill(exit_raw, pct, points)
            gross = (entry_raw - exit_raw) * qty
            slippage = ((entry_raw - entry_fill) + (exit_fill - exit_raw)) * qty

        charges = schedule.leg_charges(
            day, sell=not buying, price=entry_fill, quantity=qty
        ) + schedule.leg_charges(day, sell=buying, price=exit_fill, quantity=qty)

        held = exit_decision - cross.decision_time
        whipsaw = reason is ExitReason.OPPOSITE_CROSS and held <= timedelta(
            minutes=params.whipsaw_bars * params.timeframe
        )
        result.trades.append(
            Trade(
                mode=mode,
                timeframe=params.timeframe,
                trade_date=day,
                cross_day_no=cross.day_no,
                direction=cross.direction,
                side=leg.side,
                symbol=symbol,
                option_type=leg.option_type,
                strike=leg.strike,
                expiry=expiry,
                dte=(expiry - day).days,
                quantity=qty,
                entry_decision=cross.decision_time,
                entry_fill=entry_candle.start,
                exit_decision=exit_decision,
                exit_fill=exit_decision if stale else exit_candle.start,
                exit_reason=reason,
                spot_entry=spot_in.open,
                spot_exit=spot_out.open,
                option_entry_raw=entry_raw,
                option_exit_raw=exit_raw,
                option_entry_fill=entry_fill,
                option_exit_fill=exit_fill,
                gross_pnl=gross,
                slippage=slippage,
                charges=charges,
                net_pnl=gross - slippage - charges.total,
                whipsaw=whipsaw,
                stale_exit=stale,
                cost_verified=rates.verified,
            )
        )
