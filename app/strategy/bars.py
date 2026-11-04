"""Resampling 1-minute candles, EMAs and EMA-crossover detection.

Rules (every one of them is covered by a test):

* **Anchoring.** Bars are anchored at the 09:15 open: a 5-minute bar is 09:15-09:20, 09:20-09:25
  and so on; a 30-minute bar is 09:15-09:45, 09:45-10:15, ... A bar is *closed* at its end time,
  and only closed bars are ever used.
* **EMA.** ``ema = alpha * close + (1 - alpha) * previous`` with ``alpha = 2 / (period + 1)``,
  seeded with the first close. By default the series runs continuously across days (like a chart
  platform); ``reset_daily=True`` restarts it every morning instead.
* **Cross.** The sign of ``EMA_fast - EMA_slow`` flips from the last *non-zero* sign. An exact tie
  keeps the previous sign, so touching and turning back is not a cross, and the very first
  non-zero sign (no previous sign) is not a cross either. Differences below ``TIE_EPSILON`` count
  as ties so floating-point dust cannot invent a crossover.
"""

from collections.abc import Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from .models import Bar, Candle, Cross, Direction, SignalBar

SESSION_OPEN = time(9, 15)
SESSION_CLOSE = time(15, 30)  # the last 1-minute candle opens at 15:29
MINUTES_PER_SESSION = 375
TIE_EPSILON = 1e-9

_OPEN_MINUTE = 9 * 60 + 15
_CLOSE_MINUTE = 15 * 60 + 30


def in_session(moment: datetime) -> bool:
    minute = moment.hour * 60 + moment.minute
    return _OPEN_MINUTE <= minute < _CLOSE_MINUTE


def resample(candles: Sequence[Candle], minutes: int) -> list[Bar]:
    """Aggregate 1-minute candles into *minutes*-minute bars anchored at 09:15.

    Candles outside the regular session are ignored. Input must be strictly increasing and on
    whole minutes. A bar missing some of its minutes is still produced (``Bar.minutes`` says how
    many were present); callers that care check completeness first.
    """
    if not 1 <= minutes <= MINUTES_PER_SESSION:
        msg = f"timeframe must be between 1 and {MINUTES_PER_SESSION} minutes, got {minutes}"
        raise ValueError(msg)

    bars: list[Bar] = []
    group: list[Candle] = []
    group_key: tuple[date, int] | None = None
    previous: datetime | None = None

    def flush() -> None:
        if group_key is None or not group:
            return
        day, index = group_key
        start = datetime.combine(day, SESSION_OPEN) + timedelta(minutes=index * minutes)
        bars.append(
            Bar(
                start=start,
                end=start + timedelta(minutes=minutes),
                open=group[0].open,
                high=max(c.high for c in group),
                low=min(c.low for c in group),
                close=group[-1].close,
                volume=sum(c.volume for c in group),
                minutes=len(group),
            )
        )

    for candle in candles:
        if previous is not None and candle.start <= previous:
            msg = f"candles must be strictly increasing; {candle.start} follows {previous}"
            raise ValueError(msg)
        previous = candle.start
        if candle.start.second or candle.start.microsecond:
            msg = f"candle {candle.start} is not on a whole minute"
            raise ValueError(msg)
        if not in_session(candle.start):
            continue
        minute = candle.start.hour * 60 + candle.start.minute
        key = (candle.start.date(), (minute - _OPEN_MINUTE) // minutes)
        if key != group_key:
            flush()
            group, group_key = [], key
        group.append(candle)
    flush()
    return bars


def ema(values: Sequence[float], period: int) -> list[float]:
    """Exponential moving average seeded with the first value."""
    if period < 1:
        msg = "EMA period must be at least 1"
        raise ValueError(msg)
    alpha = 2 / (period + 1)
    out: list[float] = []
    previous: float | None = None
    for value in values:
        previous = value if previous is None else alpha * value + (1 - alpha) * previous
        out.append(previous)
    return out


def with_emas(
    bars: Sequence[Bar], fast: int, slow: int, *, reset_daily: bool = False
) -> list[SignalBar]:
    """Attach EMA(fast) and EMA(slow) of the closes to every bar."""
    if fast >= slow:
        msg = "the fast EMA period must be shorter than the slow one"
        raise ValueError(msg)
    segments: list[list[Bar]] = []
    if reset_daily:
        for bar in bars:
            if segments and segments[-1][0].start.date() == bar.start.date():
                segments[-1].append(bar)
            else:
                segments.append([bar])
    else:
        segments.append(list(bars))

    out: list[SignalBar] = []
    for segment in segments:
        closes = [float(b.close) for b in segment]
        fast_values, slow_values = ema(closes, fast), ema(closes, slow)
        out.extend(
            SignalBar(
                start=b.start,
                end=b.end,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                minutes=b.minutes,
                ema_fast=f,
                ema_slow=s,
            )
            for b, f, s in zip(segment, fast_values, slow_values, strict=True)
        )
    return out


def find_crosses(bars: Sequence[SignalBar], *, reset_daily: bool = False) -> list[Cross]:
    """Every EMA crossover on closed bars, in time order."""
    crosses: list[Cross] = []
    last_sign = 0
    current_day: date | None = None
    day_no = 0
    for bar in bars:
        day = bar.start.date()
        if day != current_day:
            current_day, day_no = day, 0
            if reset_daily:
                last_sign = 0
        diff = bar.ema_fast - bar.ema_slow
        sign = (diff > TIE_EPSILON) - (diff < -TIE_EPSILON)
        if sign == 0:
            continue
        if last_sign and sign != last_sign:
            day_no += 1
            crosses.append(
                Cross(
                    bar_start=bar.start,
                    decision_time=bar.end,
                    direction=Direction.BULLISH if sign > 0 else Direction.BEARISH,
                    close=bar.close,
                    ema_fast=bar.ema_fast,
                    ema_slow=bar.ema_slow,
                    day_no=day_no,
                )
            )
        last_sign = sign
    return crosses


def to_decimal(value: float | str | Decimal) -> Decimal:
    """Convert API floats to Decimal via their shortest repr (0.1 -> Decimal('0.1'))."""
    return value if isinstance(value, Decimal) else Decimal(str(value))
