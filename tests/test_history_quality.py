from datetime import date, datetime, time, timedelta

import pytest

from app.history.quality import (
    Convention,
    assess,
    assess_day,
    detect_convention,
    regular_session_only,
    to_start_convention,
)
from app.strategy.engine import StrategyParams, analyse_spot
from app.strategy.models import Candle
from tests.builders import at, candle, dec, session_minutes

DAY = date(2026, 5, 12)
NEXT = date(2026, 5, 13)


def day_candles(day: date = DAY, shift: int = 0) -> list[Candle]:
    return [candle(m + timedelta(minutes=shift), 100) for m in session_minutes(day)]


# ---------------------------------------------------------- timestamp convention


def test_start_stamped_data_is_recognised() -> None:
    assert detect_convention(day_candles()) is Convention.START


def test_close_stamped_data_is_recognised() -> None:
    assert detect_convention(day_candles(shift=1)) is Convention.CLOSE


def test_several_days_agree() -> None:
    assert detect_convention(day_candles(DAY) + day_candles(NEXT)) is Convention.START
    assert detect_convention(day_candles(DAY, 1) + day_candles(NEXT, 1)) is Convention.CLOSE


def test_one_end_of_the_day_is_enough_evidence() -> None:
    def only(candles: list[Candle], at_time: time) -> list[Candle]:
        return [c for c in candles if c.start.time() == at_time]

    assert detect_convention(only(day_candles(), time(9, 15))) is Convention.START
    assert detect_convention(only(day_candles(), time(15, 29))) is Convention.START
    assert detect_convention(only(day_candles(shift=1), time(9, 16))) is Convention.CLOSE
    # a lone 15:30 stamp proves nothing: start-stamped data with a closing print ends there too
    assert detect_convention(only(day_candles(shift=1), time(15, 30))) is Convention.UNKNOWN


def test_conflicting_days_are_unknown() -> None:
    mixed = day_candles(DAY) + day_candles(NEXT, 1)
    assert detect_convention(mixed) is Convention.UNKNOWN


def test_an_extra_closing_print_at_15_30_does_not_hide_start_stamps() -> None:
    candles = [*day_candles(), candle(at(DAY, "15:30"), 100)]
    assert detect_convention(candles) is Convention.START


def test_a_missing_open_candle_is_not_mistaken_for_close_stamps() -> None:
    """Start-stamped data without its 09:15 candle begins 09:16 but still ends 15:29."""
    candles = day_candles()[1:]
    assert candles[0].start.time() == time(9, 16)
    assert detect_convention(candles) is Convention.UNKNOWN


def test_close_stamped_data_missing_its_last_candle_is_still_close() -> None:
    candles = day_candles(shift=1)[:-1]  # starts 09:16, ends 15:29: ambiguous with the case above
    assert detect_convention(candles) is Convention.UNKNOWN
    assert detect_convention(candles[:-30]) is Convention.CLOSE  # ends 14:59, only 09:16 evidence


@pytest.mark.parametrize("stamps", [[], ["10:00", "10:01"], ["09:20", "15:00"]])
def test_no_recognisable_edges_is_unknown(stamps: list[str]) -> None:
    assert detect_convention([candle(at(DAY, s), 1) for s in stamps]) is Convention.UNKNOWN


def test_normalising_close_stamps_gives_the_exact_session_minutes() -> None:
    fixed = to_start_convention(day_candles(shift=1), Convention.CLOSE)
    assert [c.start for c in fixed] == session_minutes(DAY)
    assert fixed[0].open == dec(100)  # prices untouched


def test_normalising_start_stamps_changes_nothing() -> None:
    original = day_candles()
    assert to_start_convention(original, Convention.START) == original


def test_unknown_convention_cannot_be_normalised() -> None:
    with pytest.raises(ValueError, match="unknown"):
        to_start_convention(day_candles(), Convention.UNKNOWN)


def test_regular_session_filter_drops_pre_open_and_post_close() -> None:
    extra = [candle(at(DAY, "09:14"), 1), candle(at(DAY, "15:30"), 1), candle(at(DAY, "16:00"), 1)]
    kept = regular_session_only(extra + day_candles())
    assert [c.start for c in kept] == session_minutes(DAY)


# ------------------------------------------------------------------ day quality


def test_a_perfect_day() -> None:
    q = assess_day(DAY, day_candles())
    assert q.complete
    assert q.clean
    assert q.present == 375
    assert q.missing == ()
    assert q.describe() == ""


def test_missing_minutes_are_listed() -> None:
    candles = [c for c in day_candles() if c.start.time() not in (time(10, 0), time(10, 1))]
    q = assess_day(DAY, candles)
    assert not q.complete
    assert q.present == 373
    assert q.missing == (time(10, 0), time(10, 1))
    assert q.describe() == "373/375 minutes; missing 10:00, 10:01"


def test_long_gaps_are_abbreviated() -> None:
    candles = day_candles()[:300]
    text = assess_day(DAY, candles).describe()
    assert text.startswith("300/375 minutes; missing 14:15, 14:16, 14:17, 14:18, 14:19 +70 more")


def test_an_empty_day_is_incomplete() -> None:
    q = assess_day(DAY, [])
    assert q.present == 0
    assert len(q.missing) == 375
    assert not q.complete


def test_inconsistent_ohlc_and_non_positive_prices_are_counted_not_repaired() -> None:
    good = day_candles()
    inverted = Candle(at(DAY, "09:15"), dec(10), dec(5), dec(20), dec(10), 1)  # high < low
    outside = Candle(at(DAY, "09:16"), dec(50), dec(40), dec(30), dec(35), 1)  # open > high
    zero = Candle(at(DAY, "09:17"), dec(0), dec(1), dec(0), dec(1), 1)  # a zero price
    q = assess_day(DAY, [inverted, outside, zero, *good[3:]])
    assert q.bad_ohlc == 2
    assert q.non_positive == 1  # only the candle with a zero price
    assert not q.clean
    assert "inconsistent OHLC" in q.describe()


def test_out_of_session_candles_are_ignored_but_reported() -> None:
    q = assess_day(DAY, [*day_candles(), candle(at(DAY, "15:30"), 100)])
    assert q.complete
    assert q.out_of_session == 1
    assert "outside session hours" in q.describe()


def test_a_duplicated_timestamp_counts_once() -> None:
    candles = day_candles()
    assert assess_day(DAY, [*candles, candles[0]]).present == 375


def test_assess_splits_days() -> None:
    result = assess(day_candles(DAY) + day_candles(NEXT)[:100])
    assert [(q.day, q.present) for q in result] == [(DAY, 375), (NEXT, 100)]


def test_describe_matches_what_the_engine_would_exclude() -> None:
    candles = [c for c in day_candles() if c.start != datetime(2026, 5, 12, 11, 0)]
    assert not assess_day(DAY, candles).complete
    analysis = analyse_spot(candles, StrategyParams())
    assert DAY in analysis.incomplete_days
    assert DAY not in analysis.complete_days


def test_partial_days_ending_at_15_30_do_not_outvote_the_full_days() -> None:
    """Real feeds have a Muhurat session or a truncated last day; they must not conflict."""
    muhurat = [candle(at(NEXT, s), 100) for s in ("13:30", "14:00", "15:04", "15:30")]
    truncated = [candle(at(NEXT, s), 100) for s in ("15:25", "15:29", "15:30")]
    assert detect_convention([*day_candles(), *muhurat]) is Convention.START
    assert detect_convention([*day_candles(), *truncated]) is Convention.START
    assert detect_convention([*day_candles(shift=1), *muhurat]) is Convention.CLOSE


def test_a_partial_day_alone_is_not_enough_to_decide() -> None:
    partial = [candle(at(DAY, s), 100) for s in ("15:25", "15:28", "15:30")]
    assert detect_convention(partial) is Convention.UNKNOWN


def test_pre_open_and_post_close_prints_are_ignored() -> None:
    """A 09:14 print must not hide the 09:15 first candle, nor a 15:45 print the real last one."""
    extras = [candle(at(DAY, "09:07"), 100), candle(at(DAY, "09:14"), 100)]
    extras += [candle(at(DAY, "15:30"), 100), candle(at(DAY, "15:45"), 100)]
    assert detect_convention([*extras, *day_candles()]) is Convention.START
    assert detect_convention([*extras[:2], *day_candles(shift=1)]) is Convention.CLOSE
