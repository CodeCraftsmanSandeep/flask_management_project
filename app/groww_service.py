"""Login session for the Groww TOTP flow: log in once, cache the token, renew it when it lapses.

Design notes
------------
* The access token lives in memory only - never on disk and never in an API response.
* Groww limits token requests (5/s, 30/min, 150 per 24 h), so the token is cached and reused.
  Opening the page or pressing Refresh must never cost a login.
* The *first* login is an explicit user action (:meth:`GrowwService.connect`). After that the
  session renews itself (the TOTP flow needs no manual approval) until :meth:`disconnect`.
* A TOTP code is only valid for its 30 s window and servers commonly reject a replay, so we never
  send a code that is about to expire and never reuse the window of the last successful login.
"""

import base64
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, TypedDict

import pyotp

from .broker import BrokerClient, Gateway
from .credentials import TotpCredentials
from .errors import NotConnectedError, TokenRejectedError, TradingError
from .timeutil import IST

logger = logging.getLogger(__name__)

TOTP_INTERVAL = 30  # seconds per TOTP window
MIN_CODE_VALIDITY = 3.0  # never send a code with less than this many seconds left
EXPIRY_MARGIN = 60.0  # renew the token this long before it actually expires
FAILURE_COOLDOWN = 20.0  # automatic re-logins do not retry this soon after a failure
RESET_HOUR_IST = 6  # Groww expires access tokens every day at 06:00 IST
_TOTP_ATTEMPTS = 3
_MIN_SECRET_LENGTH_TO_SCRUB = 8


class SessionStatus(TypedDict):
    connected: bool
    connected_at: str | None
    expires_at: str | None
    login_count: int


def to_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, IST).isoformat(timespec="seconds")


def jwt_expiry(token: str) -> float | None:
    """Return the ``exp`` claim of a JWT (unix seconds), or ``None`` if it cannot be read.

    The signature is deliberately not verified: the claim is only used to decide *when to log in
    again*, never to make a trust decision.
    """
    try:
        payload_b64 = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4)))
        return float(claims["exp"])
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def next_daily_reset(now: float) -> float:
    """Unix time of the next 06:00 IST, when Groww invalidates the day's access tokens."""
    current = datetime.fromtimestamp(now, IST)
    reset = current.replace(hour=RESET_HOUR_IST, minute=0, second=0, microsecond=0)
    if reset <= current:
        reset += timedelta(days=1)
    return reset.timestamp()


@dataclass(slots=True)
class _Session:
    client: BrokerClient
    connected_at: float
    expires_at: float


class GrowwService:
    """Thread-safe, read-only access to a Groww account via the TOTP login flow."""

    def __init__(
        self,
        load_credentials: Callable[[], TotpCredentials],
        gateway: Gateway,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._load_credentials = load_credentials
        self._gateway = gateway
        self._clock = clock
        self._sleep = sleep

        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._wants_session = False  # True from the first successful connect() until disconnect()
        self._login_count = 0
        self._last_totp_step: int | None = None
        self._last_failure: tuple[float, TradingError] | None = None
        self._secrets: tuple[str, ...] = ()

    # ------------------------------------------------------------------ public API

    def connect(self) -> SessionStatus:
        """Log in unless a live session already exists. Safe to call repeatedly."""
        with self._lock:
            if self._session is None or self._expired(self._session):
                self._login(implicit=False)
            self._wants_session = True
            return self._status()

    def disconnect(self) -> SessionStatus:
        """Forget the token. Data calls fail with ``not_connected`` until ``connect`` is called."""
        with self._lock:
            self._session = None
            self._wants_session = False
            self._last_failure = None
            self._secrets = ()
            return self._status()

    def status(self) -> SessionStatus:
        with self._lock:
            return self._status()

    def get_profile(self) -> dict[str, Any]:
        return self._call(lambda client: client.get_profile())

    def get_funds(self) -> dict[str, Any]:
        return self._call(lambda client: client.get_funds())

    def get_holdings(self) -> list[dict[str, Any]]:
        return self._call(lambda client: client.get_holdings())

    def get_positions(self) -> list[dict[str, Any]]:
        return self._call(lambda client: client.get_positions())

    # Market data (read-only). Throttling is the caller's job; see ``app.history.backfill``.

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        return self._call(
            lambda client: client.get_candles(
                exchange=exchange, segment=segment, symbol=symbol, start=start, end=end
            )
        )

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]:
        return self._call(
            lambda client: client.get_expiries(
                exchange=exchange, underlying=underlying, year=year, month=month
            )
        )

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]:
        return self._call(
            lambda client: client.get_contracts(
                exchange=exchange, underlying=underlying, expiry=expiry
            )
        )

    # ---------------------------------------------------------------- internals

    def _status(self) -> SessionStatus:
        session = self._session
        return {
            "connected": self._wants_session,
            "connected_at": to_iso(session.connected_at) if session else None,
            "expires_at": to_iso(session.expires_at) if session else None,
            "login_count": self._login_count,
        }

    def _expired(self, session: _Session) -> bool:
        return self._clock() >= session.expires_at - EXPIRY_MARGIN

    def _client(self) -> BrokerClient:
        with self._lock:
            session = self._session
            if session is not None and not self._expired(session):
                return session.client
            if not self._wants_session:
                msg = "Not connected to Groww yet. Click Connect first."
                raise NotConnectedError(msg)
            return self._login(implicit=True).client

    def _call[T](self, operation: Callable[[BrokerClient], T]) -> T:
        """Run *operation*; if Groww rejects the token, log in again once and retry."""
        client = self._client()
        try:
            with self._redacting():
                return operation(client)
        except TokenRejectedError:
            logger.info("Groww rejected the access token; logging in again")
            self._drop(client)
        client = self._client()
        with self._redacting():
            return operation(client)

    def _drop(self, client: BrokerClient) -> None:
        """Discard the session, unless another thread already replaced it with a fresh one."""
        with self._lock:
            if self._session is not None and self._session.client is client:
                self._session = None

    def _login(self, *, implicit: bool) -> _Session:
        """Perform the TOTP login. Must be called with ``self._lock`` held."""
        if implicit and self._last_failure is not None:
            failed_at, failure = self._last_failure
            if self._clock() - failed_at < FAILURE_COOLDOWN:
                raise type(failure)(failure.message)

        try:
            with self._redacting():
                credentials = self._load_credentials()
                self._secrets = (credentials.token, credentials.secret)
                code, step = self._next_totp(credentials.secret)
                access_token = self._gateway.fetch_access_token(credentials.token, code)
                self._secrets = (*self._secrets, access_token)
                client = self._gateway.open_client(access_token)
        except TradingError as exc:
            self._last_failure = (self._clock(), exc)
            raise

        now = self._clock()
        expires_at = jwt_expiry(access_token)
        if expires_at is None or expires_at <= now + 2 * EXPIRY_MARGIN:
            expires_at = next_daily_reset(now)

        self._last_totp_step = step
        self._last_failure = None
        self._login_count += 1
        self._session = _Session(client=client, connected_at=now, expires_at=expires_at)
        logger.info(
            "Logged in to Groww (login #%d this run, token valid until %s)",
            self._login_count,
            to_iso(expires_at),
        )
        return self._session

    def _next_totp(self, secret: str) -> tuple[str, int]:
        """Return ``(code, window)`` that is safe to send now, waiting for a new window if needed.

        A code is unsafe when its window is nearly over or was already used by the last login.
        """
        totp = pyotp.TOTP(secret, interval=TOTP_INTERVAL)
        for _ in range(_TOTP_ATTEMPTS):
            now = self._clock()
            step = int(now // TOTP_INTERVAL)
            remaining = TOTP_INTERVAL - (now % TOTP_INTERVAL)
            if step != self._last_totp_step and remaining >= MIN_CODE_VALIDITY:
                return totp.at(int(now)), step
            self._sleep(remaining + 0.25)
        msg = "Could not obtain a usable TOTP window; is the system clock behaving?"
        raise RuntimeError(msg)

    @contextmanager
    def _redacting(self) -> Iterator[None]:
        """Scrub known secrets from error messages before they can reach logs or the browser."""
        try:
            yield
        except TradingError as exc:
            exc.message = self._scrub(exc.message)
            exc.args = (exc.message,)
            raise

    def _scrub(self, text: str) -> str:
        for secret in self._secrets:
            if len(secret) >= _MIN_SECRET_LENGTH_TO_SCRUB:
                text = text.replace(secret, "***")
        return text
