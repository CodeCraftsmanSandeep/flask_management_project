import itertools
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest

from app.strategy.bars import (
    ema,
    find_crosses,
    in_session,
    resample,
    with_emas,
)
from app.strategy.models import Bar, Candle, Cross, Direction
from tests.builders import at, candle, dec, session_minutes

DAY = date(2026, 5, 12)
NEXT_DAY = date(2026, 5, 13)


def bars_from_closes(closes: Sequence[float], day: date = DAY) -> list[Bar]:
    """5-minute bars with the given closes (open=high=low=close)."""
    out = []
    for i, c in enumerate(closes):
        start = datetime.combine(day, at(day, "09:15").time()) + timedelta(minutes=5 * i)
        p = dec(c)
        out.append(Bar(start, start + timedelta(minutes=5), p, p, p, p, 0, 5))
    return out


# --------------------------------------------------------------------------- resample


def test_five_minute_bar_aggregates_ohlcv() -> None:
    minutes = session_minutes(DAY)[:5]
    candles = [
        Candle(minutes[0], dec(100), dec(103), dec(99), dec(101), 10),
        Candle(minutes[1], dec(101), dec(105), dec(100), dec(104), 20),
        Candle(minutes[2], dec(104), dec(104), dec(98), dec(99), 30),
        Candle(minutes[3], dec(99), dec(101), dec(97), dec(100), 40),
        Candle(minutes[4], dec(100), dec(102), dec(99), dec(102), 50),
    ]
    (bar,) = resample(candles, 5)
    assert bar.start == at(DAY, "09:15")
    assert bar.end == at(DAY, "09:20")
    assert (bar.open, bar.high, bar.low, bar.close) == (dec(100), dec(105), dec(97), dec(102))
    assert bar.volume == 150
    assert bar.minutes == 5


def test_bars_are_anchored_at_the_915_open_for_every_timeframe() -> None:
    candles = [candle(m, 100) for m in session_minutes(DAY)]
    five = resample(candles, 5)
    fifteen = resample(candles, 15)
    thirty = resample(candles, 30)
    assert [b.start.strftime("%H:%M") for b in five[:3]] == ["09:15", "09:20", "09:25"]
    assert [b.start.strftime("%H:%M") for b in fifteen[:3]] == ["09:15", "09:30", "09:45"]
    assert [b.start.strftime("%H:%M") for b in thirty[:3]] == ["09:15", "09:45", "10:15"]
    assert [b.end.strftime("%H:%M") for b in thirty[:3]] == ["09:45", "10:15", "10:45"]
    assert (len(five), len(fifteen), len(thirty)) == (75, 25, 13)


def test_last_thirty_minute_bar_is_the_partial_1515_bar() -> None:
    candles = [candle(m, 100) for m in session_minutes(DAY)]
    last = resample(candles, 30)[-1]
    assert last.start == at(DAY, "15:15")
    assert last.minutes == 15


def test_candles_outside_the_regular_session_are_ignored() -> None:
    candles = [
        candle(at(DAY, "09:14"), 1),
        candle(at(DAY, "09:15"), 100),
        candle(at(DAY, "15:29"), 100),
        candle(at(DAY, "15:30"), 1),
        candle(at(DAY, "18:15"), 1),  # e.g. a Muhurat session
    ]
    bars = resample(candles, 5)
    assert [b.start for b in bars] == [at(DAY, "09:15"), at(DAY, "15:25")]
    assert all(b.close == dec(100) for b in bars)


def test_a_missing_minute_still_yields_a_bar_and_is_counted() -> None:
    minutes = session_minutes(DAY)[:5]
    del minutes[2]
    (bar,) = resample([candle(m, 100) for m in minutes], 5)
    assert bar.minutes == 4


def test_bars_restart_at_the_open_each_day() -> None:
    candles = [candle(m, 100) for d in (DAY, NEXT_DAY) for m in session_minutes(d)[:10]]
    bars = resample(candles, 5)
    assert [b.start for b in bars] == [
        at(DAY, "09:15"),
        at(DAY, "09:20"),
        at(NEXT_DAY, "09:15"),
        at(NEXT_DAY, "09:20"),
    ]


@pytest.mark.parametrize("timeframe", [0, -5, 376])
def test_invalid_timeframes_are_rejected(timeframe: int) -> None:
    with pytest.raises(ValueError, match="timeframe"):
        resample([], timeframe)


def test_unsorted_or_duplicate_candles_are_rejected() -> None:
    a, b = candle(at(DAY, "09:15"), 1), candle(at(DAY, "09:16"), 1)
    with pytest.raises(ValueError, match="strictly increasing"):
        resample([b, a], 5)
    with pytest.raises(ValueError, match="strictly increasing"):
        resample([a, a], 5)


def test_candles_not_on_a_whole_minute_are_rejected() -> None:
    with pytest.raises(ValueError, match="whole minute"):
        resample([candle(datetime(2026, 5, 12, 9, 15, 30), 1)], 5)


def test_in_session_boundaries() -> None:
    assert not in_session(at(DAY, "09:14"))
    assert in_session(at(DAY, "09:15"))
    assert in_session(at(DAY, "15:29"))
    assert not in_session(at(DAY, "15:30"))


# --------------------------------------------------------------------------- EMA


def test_ema_is_seeded_with_the_first_value() -> None:
    assert ema([10.0, 10.0, 10.0], 9) == [10.0, 10.0, 10.0]


def test_ema_matches_the_textbook_formula() -> None:
    # period 9 -> alpha = 0.2: 0.2 * 20 + 0.8 * 10 = 12
    assert ema([10.0, 20.0], 9) == pytest.approx([10.0, 12.0])
    # period 3 -> alpha = 0.5
    assert ema([10, 10, 10, 12, 12, 8, 8], 3) == pytest.approx([10, 10, 10, 11, 11.5, 9.75, 8.875])


def test_ema_of_period_one_is_the_input() -> None:
    assert ema([3.0, 1.0, 4.0], 1) == [3.0, 1.0, 4.0]


def test_ema_rejects_period_below_one() -> None:
    with pytest.raises(ValueError, match="period"):
        ema([1.0], 0)


def test_with_emas_requires_fast_shorter_than_slow() -> None:
    with pytest.raises(ValueError, match="shorter"):
        with_emas(bars_from_closes([1, 2]), 21, 9)


def test_emas_run_continuously_across_days_by_default() -> None:
    day1 = bars_from_closes([100.0, 100.0], DAY)
    day2 = bars_from_closes([200.0], NEXT_DAY)
    out = with_emas(day1 + day2, 1, 3)
    # slow EMA (alpha 0.5) carries yesterday's 100 into today: 0.5 * 200 + 0.5 * 100
    assert out[2].ema_slow == pytest.approx(150.0)


def test_emas_restart_each_morning_when_reset_daily() -> None:
    day1 = bars_from_closes([100.0, 100.0], DAY)
    day2 = bars_from_closes([200.0], NEXT_DAY)
    out = with_emas(day1 + day2, 1, 3, reset_daily=True)
    assert out[2].ema_slow == pytest.approx(200.0)  # reseeded with today's first close


# --------------------------------------------------------------------------- crosses


def crosses_of(closes: Sequence[float], fast: int = 1, slow: int = 3) -> list[Cross]:
    return find_crosses(with_emas(bars_from_closes(closes), fast, slow))


def test_hand_checked_bearish_cross() -> None:
    # fast = close, slow EMA(3): 10, 10, 10, 11, 11.5, 9.75, 8.875
    # diff:                       0   0   0  +1  +.5 -1.75 -.875
    # The first non-zero sign (bar 3) is not a cross; the flip at bar 5 is.
    (cross,) = crosses_of([10, 10, 10, 12, 12, 8, 8])
    assert cross.direction is Direction.BEARISH
    assert cross.bar_start == at(DAY, "09:40")
    assert cross.decision_time == at(DAY, "09:45")  # the bar's close, not its open
    assert cross.close == dec(8)
    assert cross.day_no == 1


def test_touching_and_turning_back_is_not_a_cross() -> None:
    # slow EMA(3) at bar 3 is 11; closing at 11 makes diff exactly 0 (a tie); then back up.
    assert crosses_of([10, 10, 10, 12, 11, 13]) == []


def test_tie_keeps_the_previous_sign_so_the_cross_lands_on_the_first_opposite_bar() -> None:
    # signs: 0, 0, 0, +, 0 (tie), - -> cross on bar 5, not on the tie bar 4.
    (cross,) = crosses_of([10, 10, 10, 12, 11, 9])
    assert cross.direction is Direction.BEARISH
    assert cross.bar_start == at(DAY, "09:40")


def test_flat_prices_never_cross() -> None:
    assert crosses_of([100.0] * 30) == []


def test_crosses_alternate_in_direction() -> None:
    closes = [10, 10, 10, 12, 12, 8, 8, 14, 14, 14, 6, 6, 6, 6]
    directions = [c.direction for c in crosses_of(closes)]
    assert directions
    assert all(a is not b for a, b in itertools.pairwise(directions))


def test_day_numbers_count_crosses_within_each_day() -> None:
    day1 = bars_from_closes([10, 10, 10, 12, 12, 8, 8, 14, 14, 14], DAY)
    day2 = bars_from_closes([14, 14, 3, 3, 3, 20, 20, 20], NEXT_DAY)
    crosses = find_crosses(with_emas(day1 + day2, 1, 3))
    by_day = {d: [c.day_no for c in crosses if c.bar_start.date() == d] for d in (DAY, NEXT_DAY)}
    assert by_day[DAY] == [1, 2]
    assert by_day[NEXT_DAY][0] == 1
    assert by_day[NEXT_DAY] == list(range(1, len(by_day[NEXT_DAY]) + 1))


def test_a_sign_flip_across_the_overnight_gap_is_a_cross_in_continuous_mode() -> None:
    day1 = bars_from_closes([10, 10, 10, 12, 12, 12], DAY)  # ends with fast above slow
    day2 = bars_from_closes([5, 5, 5], NEXT_DAY)  # gaps down: first bar flips the sign
    (cross,) = find_crosses(with_emas(day1 + day2, 1, 3))
    assert cross.bar_start == at(NEXT_DAY, "09:15")
    assert cross.direction is Direction.BEARISH
    assert cross.day_no == 1


def test_reset_daily_forgets_yesterdays_sign() -> None:
    day1 = bars_from_closes([10, 10, 10, 12, 12, 12], DAY)
    day2 = bars_from_closes([5, 5, 5], NEXT_DAY)
    bars = with_emas(day1 + day2, 1, 3, reset_daily=True)
    assert find_crosses(bars, reset_daily=True) == []


def test_tiny_floating_point_dust_is_a_tie_not_a_cross() -> None:
    bars = with_emas(bars_from_closes([100.0, 100.0, 100.0, 100.0]), 1, 3)
    # Fast is +1e-12 above slow, then -1e-12 below: below the tie epsilon both times.
    noisy = [replace(b, ema_fast=100 + 1e-12 * (1 if i % 2 else -1)) for i, b in enumerate(bars)]
    assert find_crosses(noisy) == []


def test_a_real_difference_just_above_epsilon_is_a_cross() -> None:
    bars = with_emas(bars_from_closes([100.0] * 4), 1, 3)
    flipping = [replace(b, ema_fast=100 + 1e-6 * (1 if i % 2 else -1)) for i, b in enumerate(bars)]
    assert len(find_crosses(flipping)) == 3
