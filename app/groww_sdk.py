"""The only module that talks to the Groww SDK (``growwapi``) and to ``requests``.

The rest of the app depends on :mod:`app.broker`, so SDK quirks stay in one place:

* ``GrowwAPI.__init__`` downloads and prints a changelog, with no timeout.
  -> disabled here.
* Data calls default to an *infinite* timeout.
  -> we always pass one.
* ``requests`` errors other than timeouts leak out of the SDK untouched.
  -> translated here.
* SDK exceptions are mapped onto the app's own :mod:`app.errors` hierarchy.
* ``get_access_token`` is annotated ``-> dict`` but returns the token string.
  -> both shapes are accepted.

This module is strictly read-only: it exposes no order placement or modification.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import requests
from growwapi import GrowwAPI
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
)

from .broker import BrokerClient
from .errors import (
    ForbiddenError,
    LoginFailedError,
    RateLimitedError,
    TokenRejectedError,
    TradingError,
    UpstreamError,
    UpstreamTimeoutError,
)

_LOGIN_HINT = (
    "Check the TOTP token and secret in keys.txt, and that this computer's clock is set "
    "automatically (TOTP codes depend on the time)."
)


def translate_error(exc: Exception, *, login: bool = False) -> TradingError:
    """Map an SDK / ``requests`` exception onto an app error with a safe message.

    Exceptions we do not recognise are re-raised untouched so real bugs are never disguised as
    "Groww problems".
    """
    if isinstance(exc, TradingError):
        return exc
    if isinstance(exc, GrowwAPIRateLimitException):
        what = "Groww login" if login else "Groww"
        return RateLimitedError(f"{what} rate limit reached. Wait a little and try again.")
    if isinstance(exc, GrowwAPITimeoutException | requests.Timeout):
        return UpstreamTimeoutError("Groww did not answer in time. Try again.")
    if isinstance(exc, GrowwAPIAuthenticationException):
        if login:
            return LoginFailedError(f"Groww rejected the login (HTTP 401). {_LOGIN_HINT}")
        return TokenRejectedError("Groww rejected the access token (it expired or was revoked).")
    if isinstance(exc, GrowwAPIAuthorisationException):
        if login:
            return LoginFailedError(f"Groww refused the login (HTTP 403). {_LOGIN_HINT}")
        return ForbiddenError(
            "Groww says this token may not call that endpoint. "
            "Check that your Trading API subscription is active."
        )
    if isinstance(exc, GrowwAPIException):
        if exc.code == "403" and not login:
            # Seen in practice: account calls work but every market-data call is refused.
            return ForbiddenError(
                "Groww refused this request (HTTP 403 - Access forbidden). If account pages "
                "work but market data does not, your API key or plan probably does not include "
                "live/historical market data. Check the Trading API subscription and "
                "permissions on groww.in/trade-api."
            )
        if login and exc.code == "400":
            return LoginFailedError(f"Groww rejected the login: {exc.msg}. {_LOGIN_HINT}")
        return UpstreamError(f"Groww API error {exc.code}: {exc.msg}")
    if isinstance(exc, requests.ConnectionError):
        return UpstreamError("Could not reach api.groww.in. Check your internet connection.")
    if isinstance(exc, requests.RequestException | KeyError | TypeError):
        return UpstreamError("Groww returned a response this app could not understand.")
    raise exc


def _expect_dict(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        msg = "Groww returned an unexpected response shape."
        raise UpstreamError(msg)
    return payload


def _expect_records(payload: Any, key: str) -> list[dict[str, Any]]:
    """Accept ``{key: [...]}`` or a bare list; a missing key means "no records"."""
    items = payload.get(key, []) if isinstance(payload, dict) else payload
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        msg = "Groww returned an unexpected response shape."
        raise UpstreamError(msg)
    return items


def _expect_rows(payload: Any, key: str) -> list[list[Any]]:
    """``{key: [[...], ...]}`` -> the rows; a missing key means "no rows"."""
    items = _expect_dict(payload).get(key, [])
    if not isinstance(items, list) or not all(isinstance(row, list | tuple) for row in items):
        msg = "Groww returned an unexpected response shape."
        raise UpstreamError(msg)
    return [list(row) for row in items]


def _expect_strings(payload: Any, key: str) -> list[str]:
    """``{key: ["a", "b"]}`` -> the strings; a missing key means "none"."""
    items = _expect_dict(payload).get(key, [])
    if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
        msg = "Groww returned an unexpected response shape."
        raise UpstreamError(msg)
    return items


_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class _QuietGrowwAPI(GrowwAPI):  # type: ignore[misc]
    """``GrowwAPI`` without the changelog download its constructor performs.

    The stock constructor makes an HTTP request with no timeout and prints to stdout. Overriding
    this private hook is safe across SDK updates: if it is ever renamed the override simply stops
    applying, and ``tests/test_groww_sdk.py`` fails to tell us.
    """

    def _display_changelog(self) -> None:
        return None


class _SdkClient:
    """:class:`~app.broker.BrokerClient` backed by a logged-in ``GrowwAPI`` object."""

    def __init__(self, api: GrowwAPI, timeout: int) -> None:
        self._api = api
        self._timeout = timeout

    @staticmethod
    def _call(request: Callable[[], Any]) -> Any:
        try:
            return request()
        except Exception as exc:
            raise translate_error(exc) from exc

    def get_profile(self) -> dict[str, Any]:
        return _expect_dict(self._call(lambda: self._api.get_user_profile(timeout=self._timeout)))

    def get_funds(self) -> dict[str, Any]:
        return _expect_dict(
            self._call(lambda: self._api.get_available_margin_details(timeout=self._timeout))
        )

    def get_holdings(self) -> list[dict[str, Any]]:
        payload = self._call(lambda: self._api.get_holdings_for_user(timeout=self._timeout))
        return _expect_records(payload, "holdings")

    def get_positions(self) -> list[dict[str, Any]]:
        payload = self._call(lambda: self._api.get_positions_for_user(timeout=self._timeout))
        return _expect_records(payload, "positions")

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        if start >= end:
            msg = "start must be before end"
            raise ValueError(msg)
        payload = self._call(
            lambda: self._api.get_historical_candles(
                exchange=exchange,
                segment=segment,
                groww_symbol=symbol,
                start_time=start.strftime(_TIME_FORMAT),
                end_time=end.strftime(_TIME_FORMAT),
                candle_interval=GrowwAPI.CANDLE_INTERVAL_MIN_1,
                timeout=self._timeout,
            )
        )
        return _expect_rows(payload, "candles")

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]:
        payload = self._call(
            lambda: self._api.get_expiries(
                exchange=exchange,
                underlying_symbol=underlying,
                year=year,
                month=month,
                timeout=self._timeout,
            )
        )
        return _expect_strings(payload, "expiries")

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]:
        payload = self._call(
            lambda: self._api.get_contracts(
                exchange=exchange,
                underlying_symbol=underlying,
                expiry_date=expiry.isoformat(),
                timeout=self._timeout,
            )
        )
        return _expect_strings(payload, "contracts")


class SdkGateway:
    """:class:`~app.broker.Gateway` implemented with the official ``growwapi`` package."""

    def __init__(self, timeout: int = 10) -> None:
        self._timeout = timeout

    def fetch_access_token(self, api_key: str, totp_code: str) -> str:
        try:
            result = GrowwAPI.get_access_token(api_key=api_key, totp=totp_code)
        except Exception as exc:
            raise translate_error(exc, login=True) from exc
        token = result.get("token") if isinstance(result, dict) else result
        if not isinstance(token, str) or not token:
            msg = "Groww returned an unexpected login response."
            raise UpstreamError(msg)
        return token

    def open_client(self, access_token: str) -> BrokerClient:
        return _SdkClient(_QuietGrowwAPI(access_token), self._timeout)
