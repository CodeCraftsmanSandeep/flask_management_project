"""Exact, lossless conversion of engine objects to and from JSON-safe dictionaries.

Used for the Postgres result cache. Money stays a ``Decimal`` (stored as a string), moments are ISO
strings, enums are their values: loading a cached run gives back *identical* objects, which the
round-trip tests assert, so the cache can never change a number.
"""

import types
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

from app.strategy.engine import DayOutcome
from app.strategy.models import Skip, Trade


def encode(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: encode(getattr(value, f.name)) for f in fields(value)}
    return value


def decode(kind: Any, value: Any) -> Any:
    origin = get_origin(kind)
    if origin in (Union, types.UnionType):
        options = [a for a in get_args(kind) if a is not type(None)]
        return None if value is None else decode(options[0], value)
    if kind is Decimal:
        return Decimal(value)
    if kind is datetime:
        return datetime.fromisoformat(value)
    if kind is date:
        return date.fromisoformat(value)
    if isinstance(kind, type) and issubclass(kind, Enum):
        return kind(value)
    if isinstance(kind, type) and is_dataclass(kind):
        hints = get_type_hints(kind)
        return kind(**{f.name: decode(hints[f.name], value[f.name]) for f in fields(kind)})
    return value


def encode_trade(trade: Trade) -> dict[str, Any]:
    return encode(trade)  # type: ignore[no-any-return]


def decode_trade(data: dict[str, Any]) -> Trade:
    return decode(Trade, data)  # type: ignore[no-any-return]


def encode_skip(skip: Skip) -> dict[str, Any]:
    return encode(skip)  # type: ignore[no-any-return]


def decode_skip(data: dict[str, Any]) -> Skip:
    return decode(Skip, data)  # type: ignore[no-any-return]


def encode_day(day: DayOutcome) -> dict[str, Any]:
    return encode(day)  # type: ignore[no-any-return]


def decode_day(data: dict[str, Any]) -> DayOutcome:
    return decode(DayOutcome, data)  # type: ignore[no-any-return]


__all__ = [
    "decode",
    "decode_day",
    "decode_skip",
    "decode_trade",
    "encode",
    "encode_day",
    "encode_skip",
    "encode_trade",
]
