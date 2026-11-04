"""Test doubles: a controllable clock and an in-memory stand-in for the Groww gateway.

Every secret in here is obviously fake. ``SECRET`` is the RFC 6238 test secret, which lets tests
compare against the published TOTP vectors.
"""

import base64
import json
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

from app.broker import BrokerClient
from app.errors import TokenRejectedError, TradingError

TOKEN = "fake-totp-token-" + "x" * 48
SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32 of "12345678901234567890"
WINDOW_START = 1_234_567_890  # a TOTP window boundary (RFC 6238 test time)
START = float(WINDOW_START + 5)  # 5 s into that window: plenty of validity left


class FakeClock:
    """``time.time`` / ``time.sleep`` replacement where sleeping just moves the clock forward."""

    def __init__(self, start: float = START) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _b64(obj: dict[str, Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def make_jwt(exp: float, jti: str = "1") -> str:
    return f"{_b64({'alg': 'none'})}.{_b64({'exp': int(exp), 'jti': jti})}.sig{jti}"


class FakeClient:
    def __init__(self, gateway: "FakeGateway", token: str) -> None:
        self._gateway = gateway
        self.token = token

    def _guard(self) -> None:
        gateway = self._gateway
        if gateway.on_data_call is not None:
            gateway.on_data_call(self.token)
        if gateway.reject_all or self.token in gateway.revoked:
            raise TokenRejectedError("token rejected")
        if gateway.data_error is not None:
            raise type(gateway.data_error)(gateway.data_error.message)

    def get_profile(self) -> dict[str, Any]:
        self._guard()
        return {
            "vendor_user_id": "fake-vendor-id",
            "ucc": "FAKE-UCC",
            "nse_enabled": True,
            "bse_enabled": True,
            "ddpi_enabled": False,
            "active_segments": ["CASH", "FNO"],
        }

    def get_funds(self) -> dict[str, Any]:
        self._guard()
        return {"clear_cash": 1000.5, "net_margin_used": 12.25, "collateral_available": 0.0}

    def get_holdings(self) -> list[dict[str, Any]]:
        self._guard()
        return [
            {"trading_symbol": "FAKE", "isin": "INE000000000", "quantity": 2, "average_price": 100}
        ]

    def get_positions(self) -> list[dict[str, Any]]:
        self._guard()
        return []

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        self._guard()
        self._gateway.candle_calls.append((exchange, segment, symbol, start, end))
        return list(self._gateway.candles.get(symbol, []))

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]:
        self._guard()
        return [e for e in self._gateway.expiries if e.startswith(str(year))]

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]:
        self._guard()
        return list(self._gateway.contracts.get(expiry.isoformat(), []))


class FakeGateway:
    """In-memory :class:`~app.broker.Gateway` that records logins and can be told to misbehave."""

    def __init__(self, clock: FakeClock, lifetime: float = 12 * 3600) -> None:
        self._clock = clock
        self._lifetime = lifetime
        self.login_calls: list[tuple[str, str]] = []
        self.issued_tokens: list[str] = []
        self.revoked: set[str] = set()
        self.reject_all = False  # every data call fails with TokenRejectedError
        self.opaque_tokens = False  # issue non-JWT tokens (no readable expiry)
        self.login_error: TradingError | None = None
        self.data_error: TradingError | None = None
        # canned market data for the history calls
        self.candles: dict[str, list[list[Any]]] = {}
        self.expiries: list[str] = []
        self.contracts: dict[str, list[str]] = {}
        self.candle_calls: list[tuple[str, str, str, datetime, datetime]] = []
        self.on_data_call: Callable[[str], None] | None = (
            None  # lets a test pause a call mid-flight
        )

    def fetch_access_token(self, api_key: str, totp_code: str) -> str:
        self.login_calls.append((api_key, totp_code))
        if self.login_error is not None:
            raise type(self.login_error)(self.login_error.message)
        number = len(self.issued_tokens) + 1
        if self.opaque_tokens:
            token = f"opaque-access-token-{number}"
        else:
            token = make_jwt(self._clock() + self._lifetime, jti=str(number))
        self.issued_tokens.append(token)
        return token

    def open_client(self, access_token: str) -> BrokerClient:
        return FakeClient(self, access_token)

    def revoke_all(self) -> None:
        """Simulate Groww invalidating every token issued so far (e.g. the 06:00 IST reset)."""
        self.revoked.update(self.issued_tokens)
