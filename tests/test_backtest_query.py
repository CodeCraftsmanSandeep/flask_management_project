import json
from datetime import date, datetime, time, timedelta

import pytest

from app.backtest import codec
from app.backtest.query import (
    CUTOFF_OPTIONS,
    START_OPTIONS,
    BadFilterError,
    Filters,
    outcome_of,
    parse_filters,
    select,
)
from app.strategy.engine import DayOutcome
from app.strategy.models import Mode, Skip, SkipReason, Trade
from tests.builders import make_trade
from tests.test_engine import golden

D1 = date(2026, 5, 12)
D2 = date(2026, 5, 13)


def tr(hhmm: str, net: float, *, mode: Mode = Mode.ATM_BUY, day: date = D1) -> Trade:
    entry = datetime.combine(day, time.fromisoformat(hhmm))
    return make_trade(
        net, mode=mode, day=day, entry_at=entry, exit_at=entry + timedelta(minutes=20)
    )


# ------------------------------------------------------------------ parsing


def test_defaults_are_the_widest_window_and_every_trade() -> None:
    f = parse_filters({})
    assert f == Filters()
    assert (f.timeframe, f.mode, f.outcome, f.cross) == (15, None, "all", None)
    assert (f.start, f.cutoff) == (time(9, 30), time(14, 30))


def test_empty_and_all_values_mean_default() -> None:
    f = parse_filters({"mode": "all", "cross": "all", "outcome": "", "start": " ", "from": ""})
    assert f == Filters()


def test_a_full_query() -> None:
    f = parse_filters(
        {
            "timeframe": "15",
            "mode": "atm_sell",
            "outcome": "loss",
            "start": "10:00",
            "cutoff": "12:30",
            "cross": "2",
            "from": "2026-01-01",
            "to": "2026-02-01",
        }
    )
    assert f == Filters(
        15, Mode.ATM_SELL, "loss", time(10, 0), time(12, 30), 2, date(2026, 1, 1), date(2026, 2, 1)
    )


@pytest.mark.parametrize(
    "query",
    [
        {"timeframe": "7"},
        {"timeframe": "five"},
        {"mode": "nonsense"},
        {"outcome": "huge"},
        {"start": "09:45"},
        {"start": "9.30"},
        {"cutoff": "11:00"},
        {"cutoff": "15:00"},
        {"cross": "0"},
        {"cross": "21"},
        {"cross": "first"},
        {"from": "yesterday"},
        {"to": "2026-13-01"},
        {"from": "2026-02-01", "to": "2026-01-01"},
    ],
)
def test_bad_values_are_rejected_with_a_clear_400(query: dict[str, str]) -> None:
    with pytest.raises(BadFilterError) as caught:
        parse_filters(query)
    assert caught.value.http_status == 400
    assert caught.value.message


def test_every_offered_option_is_accepted() -> None:
    for start in START_OPTIONS:
        for cutoff in CUTOFF_OPTIONS:
            assert (
                parse_filters(
                    {"start": start.strftime("%H:%M"), "cutoff": cutoff.strftime("%H:%M")}
                ).start
                == start
            )


def test_the_offered_windows_match_the_specification() -> None:
    assert [t.strftime("%H:%M") for t in START_OPTIONS] == ["09:30", "10:00", "10:30"]
    assert [t.strftime("%H:%M") for t in CUTOFF_OPTIONS] == [
        "11:30",
        "12:00",
        "12:30",
        "13:00",
        "13:30",
        "14:00",
        "14:30",
    ]


# ---------------------------------------------------------------- selecting


def sample() -> list[Trade]:
    return [
        tr("09:35", +100),  # D1 entry 1
        tr("10:05", -50),  # D1 entry 2
        tr("11:00", 0),  # D1 entry 3 (a tie)
        tr("13:00", +10),  # D1 entry 4
        tr("09:40", -5, day=D2),  # D2 entry 1
        tr("12:10", +7, day=D2),  # D2 entry 2
        tr("09:35", +200, mode=Mode.ATM_SELL),  # D1 entry 1 of another mode
    ]


def ids(selected: list) -> list[int]:  # type: ignore[type-arg]
    return [s.index for s in selected]


def test_no_filters_keeps_everything_in_order() -> None:
    chosen = select(sample(), Filters())
    assert ids(chosen) == [0, 1, 2, 3, 4, 5, 6]


def test_entry_numbers_count_per_mode_and_day() -> None:
    chosen = select(sample(), Filters())
    assert [(s.index, s.entry_no) for s in chosen] == [
        (0, 1),
        (1, 2),
        (2, 3),
        (3, 4),
        (4, 1),
        (5, 2),
        (6, 1),
    ]


def test_the_window_bounds_are_inclusive() -> None:
    trades = [tr("10:00", 1), tr("10:30", 1), tr("12:00", 1), tr("12:01", 1)]
    chosen = select(trades, Filters(start=time(10, 0), cutoff=time(12, 0)))
    assert ids(chosen) == [0, 1, 2]


def test_entry_numbers_restart_inside_the_selected_window() -> None:
    """'1st cross' means the first trade *this window* takes, not the first of the day."""
    chosen = select(sample(), Filters(start=time(10, 0)))
    first = [s for s in chosen if s.entry_no == 1 and s.trade.mode is Mode.ATM_BUY]
    assert [(s.index, s.trade.trade_date) for s in first] == [(1, D1), (5, D2)]


def test_the_cross_filter_picks_the_nth_entry() -> None:
    assert ids(select(sample(), Filters(cross=1))) == [0, 4, 6]
    assert ids(select(sample(), Filters(cross=2))) == [1, 5]
    assert ids(select(sample(), Filters(cross=4))) == [3]
    assert select(sample(), Filters(cross=5)) == []


def test_hiding_a_trade_does_not_renumber_the_others() -> None:
    wins = select(sample(), Filters(outcome="win"))
    assert [(s.index, s.entry_no) for s in wins] == [(0, 1), (3, 4), (5, 2), (6, 1)]


def test_outcome_filter() -> None:
    assert ids(select(sample(), Filters(outcome="win"))) == [0, 3, 5, 6]
    assert ids(select(sample(), Filters(outcome="loss"))) == [1, 4]
    assert ids(select(sample(), Filters(outcome="tie"))) == [2]


def test_outcome_of() -> None:
    assert [outcome_of(tr("10:00", n)) for n in (1, -1, 0)] == ["win", "loss", "tie"]


def test_mode_filter_and_the_compare_view_that_ignores_it() -> None:
    only_sell = Filters(mode=Mode.ATM_SELL)
    assert ids(select(sample(), only_sell)) == [6]
    assert ids(select(sample(), only_sell, ignore_mode=True)) == [0, 1, 2, 3, 4, 5, 6]


def test_date_range_is_inclusive() -> None:
    assert ids(select(sample(), Filters(date_from=D2))) == [4, 5]
    assert ids(select(sample(), Filters(date_to=D1))) == [0, 1, 2, 3, 6]
    assert ids(select(sample(), Filters(date_from=D2, date_to=D2))) == [4, 5]
    assert select(sample(), Filters(date_from=date(2027, 1, 1))) == []


def test_the_sample_days_are_a_tuesday_and_a_wednesday() -> None:
    assert (D1.weekday(), D2.weekday()) == (1, 2)


def test_weekday_filter_includes_only_the_chosen_days() -> None:
    assert ids(select(sample(), Filters(days=frozenset({1})))) == [0, 1, 2, 3, 6]  # Tuesday
    assert ids(select(sample(), Filters(days=frozenset({2})))) == [4, 5]  # Wednesday
    assert ids(select(sample(), Filters(days=frozenset({1, 2})))) == [0, 1, 2, 3, 4, 5, 6]
    assert select(sample(), Filters(days=frozenset({0, 3, 4}))) == []


def test_leaving_a_day_out_does_not_renumber_the_others() -> None:
    chosen = select(sample(), Filters(days=frozenset({2})))
    assert [(s.index, s.entry_no) for s in chosen] == [(4, 1), (5, 2)]


def test_weekday_filter_applies_to_the_compare_view_too() -> None:
    chosen = select(sample(), Filters(days=frozenset({2}), mode=Mode.ATM_SELL), ignore_mode=True)
    assert ids(chosen) == [4, 5]


def test_weekday_names_are_parsed_in_any_order_and_case() -> None:
    assert parse_filters({"days": "Thu, mon,TUE"}).days == frozenset({0, 1, 3})
    assert parse_filters({"days": "wed"}).days == frozenset({2})


def test_the_days_are_exactly_the_ones_named() -> None:
    """Monday to Friday does not quietly include the odd Saturday or Sunday session."""
    assert parse_filters({"days": "mon,tue,wed,thu,fri"}).days == frozenset(range(5))
    assert parse_filters({"days": "mon,tue,wed,thu,fri,sat,sun"}) == Filters()
    assert parse_filters({"days": "sun"}).days == frozenset({6})


def test_a_special_weekend_session_follows_its_own_day_button() -> None:
    sunday = date(2026, 2, 1)
    assert sunday.weekday() == 6
    trades = [tr("09:35", 1, day=sunday), tr("09:35", 1)]
    assert ids(select(trades, Filters())) == [0, 1]
    assert ids(select(trades, Filters(days=frozenset(range(5))))) == [1]
    assert ids(select(trades, Filters(days=frozenset({6})))) == [0]


@pytest.mark.parametrize("value", ["monday", "mon;tue", "mon,xyz", "0", "mon,,tue"])
def test_bad_day_lists_are_rejected(value: str) -> None:
    with pytest.raises(BadFilterError) as caught:
        parse_filters({"days": value})
    assert "days" in caught.value.message


def test_an_empty_day_set_is_refused() -> None:
    with pytest.raises(BadFilterError):
        Filters(days=frozenset())
    with pytest.raises(BadFilterError):
        Filters(days=frozenset({7}))


def test_filters_combine() -> None:
    chosen = select(sample(), Filters(outcome="win", cross=1, mode=Mode.ATM_BUY, date_to=D1))
    assert ids(chosen) == [0]


# --------------------------------------------------------------------- codec


def test_every_golden_trade_survives_a_json_round_trip_exactly() -> None:
    trades = golden().trades
    assert len(trades) == 9
    for trade in trades:
        wire = json.loads(json.dumps(codec.encode_trade(trade)))
        assert codec.decode_trade(wire) == trade


def test_money_is_stored_as_exact_strings() -> None:
    wire = codec.encode_trade(golden().trades[0])
    assert isinstance(wire["net_pnl"], str)
    assert isinstance(wire["charges"]["stt"], str)
    assert isinstance(wire["entry_fill"], str)
    assert wire["mode"] in {m.value for m in Mode}


def test_skips_and_days_round_trip() -> None:
    skip = Skip(D1, Mode.ATM_BUY, datetime(2026, 5, 12, 9, 45), SkipReason.NO_OPTION_DATA, "x")
    day = DayOutcome(D1, "incomplete_spot", "374/375 minutes")
    assert codec.decode_skip(json.loads(json.dumps(codec.encode_skip(skip)))) == skip
    assert codec.decode_day(json.loads(json.dumps(codec.encode_day(day)))) == day
