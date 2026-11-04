"""Expected, user-facing failures.

Every error carries a stable machine-readable ``code`` and the HTTP status the web layer answers
with, so the routes never need to special-case individual failures.
"""


class TradingError(Exception):
    """Base class for failures we expect and can explain to the user."""

    code = "error"
    http_status = 500

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class CredentialsError(TradingError):
    """keys.txt (or the environment) is missing, unreadable or malformed."""

    code = "credentials_error"
    http_status = 500


class NotConnectedError(TradingError):
    """A data call was made before the user clicked Connect."""

    code = "not_connected"
    http_status = 401


class LoginFailedError(TradingError):
    """Groww rejected the TOTP login."""

    code = "login_failed"
    http_status = 401


class TokenRejectedError(TradingError):
    """Groww rejected the access token on a data call (expired or revoked)."""

    code = "session_expired"
    http_status = 401


class ForbiddenError(TradingError):
    """The token is valid but may not call this endpoint (e.g. no active API subscription)."""

    code = "forbidden"
    http_status = 403


class RateLimitedError(TradingError):
    """Groww answered 429."""

    code = "rate_limited"
    http_status = 429


class UpstreamTimeoutError(TradingError):
    """Groww (or the network in between) did not answer in time."""

    code = "upstream_timeout"
    http_status = 504


class UpstreamError(TradingError):
    """Groww (or the network in between) misbehaved."""

    code = "upstream_error"
    http_status = 502


class BadRequestError(TradingError):
    """The request itself is wrong (an unknown filter value, a malformed date, ...)."""

    code = "bad_request"
    http_status = 400


class NoDataError(TradingError):
    """There is nothing to show yet because no market data has been downloaded."""

    code = "no_data"
    http_status = 404


class ResultsChangedError(TradingError):
    """The cached results were recomputed since the page loaded its list."""

    code = "results_changed"
    http_status = 409
