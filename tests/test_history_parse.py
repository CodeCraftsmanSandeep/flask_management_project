from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.errors import UpstreamError
from app.history.parse import parse_candles, parse_row, parse_timestamp
from app.timeutil import IST

D = Decimal
OPEN = datetime(2026, 5, 12, 9, 15)  # NSE open, naive IST
EPOCH = datetime(2026, 5, 12, 9, 15, tzinfo=IST).timestamp()


@pytest.mark.parametrize(
    "value",
    [
        EPOCH,  # epoch seconds (float)
        int(EPOCH),  # epoch seconds (int)
        int(EPOCH * 1000),  # epoch milliseconds
        "2026-05-12T09:15:00",  # naive ISO is taken as IST already
        "2026-05-12 09:15:00",
        "2026-05-12T09:15:00+05:30",
        "2026-05-12T03:45:00+00:00",  # same instant in UTC -> converted to IST
        " 2026-05-12T09:15:00 ",
    ],
)
def test_every_documented_timestamp_shape_becomes_naive_ist(value: Any) -> None:
    assert parse_timestamp(value) == OPEN


def test_the_epoch_is_read_as_an_instant_not_as_local_time() -> None:
    utc = datetime(2026, 5, 12, 3, 45, tzinfo=UTC).timestamp()
    assert parse_timestamp(utc) == OPEN


@pytest.mark.parametrize("value", [None, True, [], {}, "yesterday", "", float("inf"), 10**30])
def test_unreadable_timestamps_are_upstream_errors(value: Any) -> None:
    with pytest.raises(UpstreamError):
        parse_timestamp(value)


def test_a_full_row() -> None:
    c = parse_row(["2026-05-12T09:15:00", 24000.5, 24010, 23995.25, 24005.1, 1234, 56])
    assert (c.start, c.open, c.high, c.low, c.close) == (
        OPEN,
        D("24000.5"),
        D(24010),
        D("23995.25"),
        D("24005.1"),
    )
    assert (c.volume, c.oi) == (1234, 56)


def test_prices_are_exact_decimals_of_what_was_sent() -> None:
    c = parse_row(["2026-05-12T09:15:00", 0.1, 0.2, 0.05, 0.30000000000000004])
    assert (c.open, c.high, c.low) == (D("0.1"), D("0.2"), D("0.05"))
    assert c.close == D("0.30000000000000004")


def test_volume_and_oi_are_optional() -> None:
    c = parse_row(["2026-05-12T09:15:00", 1, 2, 0.5, 1.5])
    assert (c.volume, c.oi) == (0, None)
    c = parse_row(["2026-05-12T09:15:00", 1, 2, 0.5, 1.5, None, None])
    assert (c.volume, c.oi) == (0, None)


def test_string_prices_are_accepted() -> None:
    assert parse_row(["2026-05-12T09:15:00", "1.5", "2", "1", "1.25"]).open == D("1.5")


@pytest.mark.parametrize(
    "row",
    [
        [],
        ["2026-05-12T09:15:00", 1, 2, 3],  # too short
        ["2026-05-12T09:15:00", None, 2, 1, 1],
        ["2026-05-12T09:15:00", "abc", 2, 1, 1],
        ["2026-05-12T09:15:00", float("nan"), 2, 1, 1],
        ["2026-05-12T09:15:00", float("inf"), 2, 1, 1],
        ["2026-05-12T09:15:00", -1, 2, 1, 1],
        ["2026-05-12T09:15:00", True, 2, 1, 1],
        ["2026-05-12T09:15:00", 1, 2, 1, 1, "many"],
        ["2026-05-12T09:15:00", 1, 2, 1, 1, float("nan")],
        ["2026-05-12T09:15:00", 1, 2, 1, 1, 1, float("inf")],
    ],
)
def test_malformed_rows_are_upstream_errors_never_skipped(row: list[Any]) -> None:
    with pytest.raises(UpstreamError):
        parse_row(row)


def test_odd_but_readable_candles_are_kept_for_the_quality_checks_to_report() -> None:
    c = parse_row(["2026-05-12T09:15:00", 10, 5, 20, 10])  # high < low: nonsense, but readable
    assert (c.high, c.low) == (D(5), D(20))


def test_candles_are_sorted_and_duplicates_collapse_to_the_last_row() -> None:
    rows = [
        ["2026-05-12T09:17:00", 3, 3, 3, 3],
        ["2026-05-12T09:15:00", 1, 1, 1, 1],
        ["2026-05-12T09:16:00", 2, 2, 2, 2],
        ["2026-05-12T09:16:00", 9, 9, 9, 9],
    ]
    parsed = parse_candles(rows)
    assert [c.open for c in parsed.candles] == [D(1), D(9), D(3)]
    assert parsed.duplicates == 1


def test_no_rows_is_an_empty_result() -> None:
    parsed = parse_candles([])
    assert parsed.candles == []
    assert parsed.duplicates == 0


def test_one_bad_row_fails_the_whole_batch() -> None:
    with pytest.raises(UpstreamError):
        parse_candles([["2026-05-12T09:15:00", 1, 1, 1, 1], ["nonsense"]])
