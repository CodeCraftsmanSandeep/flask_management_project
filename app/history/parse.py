"""Turn Groww's raw candle rows into :class:`~app.strategy.models.Candle` objects.

A row is ``[timestamp, open, high, low, close, volume, oi]``. The timestamp may arrive as epoch
seconds, epoch milliseconds or an ISO string; all become naive IST wall-clock times. Which end of
the minute the stamp marks (start or close) is **not** assumed here: :mod:`app.history.quality`
checks it against the real session hours instead.

Parsing is strict about *types* (a row we cannot read is an upstream error, never silently
dropped) and lenient about *values* (odd-looking candles are kept and reported by the quality
checks, because deleting data quietly would bias a backtest).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.errors import UpstreamError
from app.strategy.models import Candle
from app.timeutil import IST, to_naive_ist

_MILLISECOND_THRESHOLD = 100_000_000_000  # epoch seconds stay below this until the year 5138


@dataclass(frozen=True, slots=True)
class ParsedCandles:
    candles: list[Candle]  # sorted by start, one per timestamp
    duplicates: int  # rows dropped because their timestamp repeated (the last row wins)


def _bad(detail: str) -> UpstreamError:
    return UpstreamError(f"Groww sent a candle this app could not read ({detail}).")


def parse_timestamp(value: Any) -> datetime:
    if isinstance(value, bool):
        raise _bad("timestamp is a boolean")
    if isinstance(value, int | float):
        seconds = value / 1000 if abs(value) >= _MILLISECOND_THRESHOLD else value
        try:
            return to_naive_ist(datetime.fromtimestamp(seconds, IST))
        except (OverflowError, OSError, ValueError) as exc:
            raise _bad("timestamp out of range") from exc
    if isinstance(value, str):
        try:
            return to_naive_ist(datetime.fromisoformat(value.strip()))
        except ValueError as exc:
            raise _bad("timestamp is not ISO formatted") from exc
    raise _bad(f"timestamp has type {type(value).__name__}")


def _price(value: Any, name: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise _bad(f"{name} is not a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise _bad(f"{name} is not a number") from exc
    if not number.is_finite() or number < 0:
        raise _bad(f"{name} is not a finite, non-negative number")
    return number


def _whole(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _bad(f"{name} is not a number")
    if value != value or value in (float("inf"), float("-inf")):  # NaN / infinity
        raise _bad(f"{name} is not finite")
    return int(value)


def parse_row(row: Sequence[Any]) -> Candle:
    if len(row) < 5:
        raise _bad(f"row has {len(row)} fields")
    volume = _whole(row[5], "volume") if len(row) > 5 else None
    oi = _whole(row[6], "open interest") if len(row) > 6 else None
    return Candle(
        start=parse_timestamp(row[0]),
        open=_price(row[1], "open"),
        high=_price(row[2], "high"),
        low=_price(row[3], "low"),
        close=_price(row[4], "close"),
        volume=volume or 0,
        oi=oi,
    )


def parse_candles(rows: Sequence[Sequence[Any]]) -> ParsedCandles:
    """Parse, sort by time and collapse repeated timestamps (the last occurrence wins)."""
    by_start: dict[datetime, Candle] = {}
    duplicates = 0
    for row in rows:
        candle = parse_row(row)
        if candle.start in by_start:
            duplicates += 1
        by_start[candle.start] = candle
    return ParsedCandles(sorted(by_start.values(), key=lambda c: c.start), duplicates)
