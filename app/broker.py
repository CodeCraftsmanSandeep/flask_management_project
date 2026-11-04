"""Interfaces the rest of the app depends on.

Keeping these separate from the SDK-backed implementation (``groww_sdk``) means the service and
the web layer can be tested without the Groww SDK, and the broker can be swapped later.
"""

from datetime import date, datetime
from typing import Any, Protocol


class BrokerClient(Protocol):
    """A logged-in, read-only view of the broker account and its market data."""

    def get_profile(self) -> dict[str, Any]: ...

    def get_funds(self) -> dict[str, Any]: ...

    def get_holdings(self) -> list[dict[str, Any]]: ...

    def get_positions(self) -> list[dict[str, Any]]: ...

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        """Raw 1-minute rows ``[timestamp, open, high, low, close, volume, oi]`` in [start, end].

        *start* and *end* are naive IST datetimes. Parsing the rows is :mod:`app.history.parse`'s
        job, so the broker adapter stays a thin, shape-checked pipe.
        """
        ...

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]:
        """Expiry dates (``YYYY-MM-DD``) of *underlying* in a year, or in one month of it."""
        ...

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]:
        """Groww symbols of every contract of *underlying* that expires on *expiry*."""
        ...


class Gateway(Protocol):
    """Turns TOTP credentials into an authenticated :class:`BrokerClient`."""

    def fetch_access_token(self, api_key: str, totp_code: str) -> str: ...

    def open_client(self, access_token: str) -> BrokerClient: ...
