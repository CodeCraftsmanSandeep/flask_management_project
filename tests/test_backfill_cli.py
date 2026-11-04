from datetime import date

import pytest

from app.history.jobs import months_back
from scripts.backfill import TIMEFRAMES, parse_args


@pytest.mark.parametrize(
    ("end", "months", "start"),
    [
        (date(2026, 5, 12), 12, date(2025, 5, 13)),
        (date(2026, 5, 12), 1, date(2026, 4, 13)),
        (date(2026, 3, 31), 1, date(2026, 3, 1)),  # February has no 31st: clamp to the 28th
        (date(2024, 3, 31), 1, date(2024, 3, 1)),  # leap year: the 29th, then +1 day
        (date(2026, 1, 15), 2, date(2025, 11, 16)),  # crosses a year boundary
        (date(2026, 12, 31), 12, date(2026, 1, 1)),
    ],
)
def test_months_back(end: date, months: int, start: date) -> None:
    assert months_back(end, months) == start


def test_defaults_cover_a_year_and_every_timeframe() -> None:
    args = parse_args([])
    assert args.months == 12
    assert args.end is None
    assert args.timeframes == list(TIMEFRAMES) == [5, 15, 30]


def test_options_are_parsed() -> None:
    args = parse_args(["--months", "3", "--end", "2026-05-12", "--timeframes", "5", "15"])
    assert (args.months, args.end, args.timeframes) == (3, date(2026, 5, 12), [5, 15])


def test_unsupported_timeframes_are_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--timeframes", "7"])
