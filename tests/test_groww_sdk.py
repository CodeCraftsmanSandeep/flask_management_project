"""Tests for the SDK adapter. They use the real exception classes but never touch the network."""

from datetime import date, datetime
from typing import Any

import pytest
import requests
from growwapi import GrowwAPI
from growwapi.groww.exceptions import (
    GrowwAPIAuthenticationException,
    GrowwAPIAuthorisationException,
    GrowwAPIException,
    GrowwAPIRateLimitException,
    GrowwAPITimeoutException,
)

from app.errors import (
    ForbiddenError,
    LoginFailedError,
    RateLimitedError,
    TokenRejectedError,
    TradingError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.groww_sdk import SdkGateway, _QuietGrowwAPI, _SdkClient, translate_error

# ------------------------------------------------------------ error translation


@pytest.mark.parametrize(
    ("exc", "login", "expected"),
    [
        (GrowwAPIRateLimitException(), False, RateLimitedError),
        (GrowwAPIRateLimitException(), True, RateLimitedError),
        (GrowwAPITimeoutException(), False, UpstreamTimeoutError),
        (requests.Timeout(), True, UpstreamTimeoutError),
        (GrowwAPIAuthenticationException(), False, TokenRejectedError),
        (GrowwAPIAuthenticationException(), True, LoginFailedError),
        (GrowwAPIAuthorisationException(), False, ForbiddenError),
        (GrowwAPIAuthorisationException(), True, LoginFailedError),
        (GrowwAPIException("Groww API Error 400: bad", "400"), True, LoginFailedError),
        (GrowwAPIException("Groww API Error 400: bad", "400"), False, UpstreamError),
        (GrowwAPIException("boom", "500"), True, UpstreamError),
        (GrowwAPIException("Access forbidden for this request.", "403"), False, ForbiddenError),
        (GrowwAPIException("Access forbidden for this request.", "403"), True, UpstreamError),
        (requests.ConnectionError("offline"), False, UpstreamError),
        (requests.exceptions.JSONDecodeError("bad json", "", 0), False, UpstreamError),
        (KeyError("payload"), False, UpstreamError),
    ],
)
def test_translate_error(exc: Exception, login: bool, expected: type[TradingError]) -> None:
    assert type(translate_error(exc, login=login)) is expected


def test_a_400_during_login_passes_on_the_servers_explanation() -> None:
    error = translate_error(
        GrowwAPIException("Groww API Error 400: Invalid TOTP", "400"), login=True
    )
    assert "Invalid TOTP" in error.message


def test_login_failures_point_at_keys_and_the_clock() -> None:
    message = translate_error(GrowwAPIAuthenticationException(), login=True).message
    assert "keys.txt" in message
    assert "clock" in message


def test_unknown_exceptions_are_not_disguised_as_groww_problems() -> None:
    with pytest.raises(RuntimeError, match="a real bug"):
        translate_error(RuntimeError("a real bug"))


# ------------------------------------------------------------------ the gateway


@pytest.mark.parametrize(
    ("sdk_result", "expected"),
    [("token-a", "token-a"), ({"token": "token-b"}, "token-b")],
)
def test_access_token_accepts_the_documented_and_the_annotated_shape(
    monkeypatch: pytest.MonkeyPatch, sdk_result: Any, expected: str
) -> None:
    monkeypatch.setattr(GrowwAPI, "get_access_token", staticmethod(lambda **_: sdk_result))
    assert SdkGateway().fetch_access_token("api-key", "123456") == expected


def test_access_token_request_carries_the_token_and_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake(**kwargs: Any) -> str:
        seen.update(kwargs)
        return "token"

    monkeypatch.setattr(GrowwAPI, "get_access_token", staticmethod(fake))
    SdkGateway().fetch_access_token("the-api-key", "654321")
    assert seen == {"api_key": "the-api-key", "totp": "654321"}


@pytest.mark.parametrize("bad", [{}, {"token": ""}, 123, None, ""])
def test_unexpected_login_payloads_are_reported(monkeypatch: pytest.MonkeyPatch, bad: Any) -> None:
    monkeypatch.setattr(GrowwAPI, "get_access_token", staticmethod(lambda **_: bad))
    with pytest.raises(UpstreamError):
        SdkGateway().fetch_access_token("api-key", "123456")


def test_login_errors_are_translated(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(**_: Any) -> str:
        raise GrowwAPIAuthenticationException

    monkeypatch.setattr(GrowwAPI, "get_access_token", staticmethod(fail))
    with pytest.raises(LoginFailedError):
        SdkGateway().fetch_access_token("api-key", "123456")


def test_building_a_client_makes_no_network_request(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def fake_get(url: str, **_: Any) -> Any:
        calls.append(url)
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "get", fake_get)

    _QuietGrowwAPI("token")
    assert calls == []

    # Control: the stock constructor does phone home (an untimed changelog request). If this
    # assertion ever fails the SDK changed and the override in groww_sdk.py can be deleted.
    GrowwAPI("token")
    assert len(calls) == 1


# ----------------------------------------------------------------- the client


class FakeApi:
    """Records how the SDK object is called and returns canned payloads."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.payloads: dict[str, Any] = {
            "profile": {"ucc": "X"},
            "funds": {"clear_cash": 1},
            "holdings": {"holdings": [{"trading_symbol": "A"}]},
            "positions": {"positions": [{"trading_symbol": "B"}]},
        }
        self.error: Exception | None = None

    def _respond(self, name: str, kwargs: dict[str, Any]) -> Any:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return self.payloads[name]

    def get_user_profile(self, **kwargs: Any) -> Any:
        return self._respond("profile", kwargs)

    def get_available_margin_details(self, **kwargs: Any) -> Any:
        return self._respond("funds", kwargs)

    def get_holdings_for_user(self, **kwargs: Any) -> Any:
        return self._respond("holdings", kwargs)

    def get_positions_for_user(self, **kwargs: Any) -> Any:
        return self._respond("positions", kwargs)


def test_every_data_call_gets_an_explicit_timeout() -> None:
    api = FakeApi()
    client = _SdkClient(api, timeout=7)
    client.get_profile()
    client.get_funds()
    client.get_holdings()
    client.get_positions()
    assert len(api.calls) == 4
    assert all(kwargs == {"timeout": 7} for _, kwargs in api.calls)  # the SDK default is "forever"


def test_records_are_unwrapped_from_their_envelope() -> None:
    client = _SdkClient(FakeApi(), timeout=5)
    assert client.get_holdings() == [{"trading_symbol": "A"}]
    assert client.get_positions() == [{"trading_symbol": "B"}]


@pytest.mark.parametrize("payload", [{}, {"holdings": []}, []])
def test_no_records_is_an_empty_list(payload: Any) -> None:
    api = FakeApi()
    api.payloads["holdings"] = payload
    assert _SdkClient(api, timeout=5).get_holdings() == []


@pytest.mark.parametrize("payload", ["oops", {"holdings": "oops"}, {"holdings": [1, 2]}, None])
def test_malformed_records_are_reported(payload: Any) -> None:
    api = FakeApi()
    api.payloads["holdings"] = payload
    with pytest.raises(UpstreamError):
        _SdkClient(api, timeout=5).get_holdings()


def test_non_dict_profile_is_reported() -> None:
    api = FakeApi()
    api.payloads["profile"] = ["not", "a", "dict"]
    with pytest.raises(UpstreamError):
        _SdkClient(api, timeout=5).get_profile()


def test_sdk_errors_are_translated_on_data_calls() -> None:
    api = FakeApi()
    api.error = GrowwAPIAuthenticationException()
    with pytest.raises(TokenRejectedError):
        _SdkClient(api, timeout=5).get_funds()


# --------------------------------------------------------------- market-data calls


class HistoryApi:
    """Canned replies for the three market-data endpoints."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.candles: Any = {"candles": [["2026-05-12T09:15:00", 1, 2, 0.5, 1.5, 10, None]]}
        self.expiries: Any = {"expiries": ["2026-05-12", "2026-05-19"]}
        self.contracts: Any = {"contracts": ["NSE-NIFTY-12May26-24000-CE"]}
        self.error: Exception | None = None

    def _respond(self, name: str, kwargs: dict[str, Any], payload: Any) -> Any:
        self.calls.append((name, kwargs))
        if self.error is not None:
            raise self.error
        return payload

    def get_historical_candles(self, **kwargs: Any) -> Any:
        return self._respond("candles", kwargs, self.candles)

    def get_expiries(self, **kwargs: Any) -> Any:
        return self._respond("expiries", kwargs, self.expiries)

    def get_contracts(self, **kwargs: Any) -> Any:
        return self._respond("contracts", kwargs, self.contracts)


def candle_args() -> dict[str, Any]:
    return {
        "exchange": "NSE",
        "segment": "CASH",
        "symbol": "NSE-NIFTY",
        "start": datetime(2026, 5, 12, 9, 15),
        "end": datetime(2026, 5, 12, 15, 30),
    }


def test_candle_request_uses_the_documented_parameters() -> None:
    api = HistoryApi()
    rows = _SdkClient(api, timeout=9).get_candles(**candle_args())
    assert rows == [["2026-05-12T09:15:00", 1, 2, 0.5, 1.5, 10, None]]
    ((name, kwargs),) = api.calls
    assert name == "candles"
    assert kwargs == {
        "exchange": "NSE",
        "segment": "CASH",
        "groww_symbol": "NSE-NIFTY",
        "start_time": "2026-05-12 09:15:00",
        "end_time": "2026-05-12 15:30:00",
        "candle_interval": "1minute",
        "timeout": 9,
    }


def test_expiry_and_contract_requests_have_timeouts_and_clean_values() -> None:
    api = HistoryApi()
    client = _SdkClient(api, timeout=4)
    assert client.get_expiries(exchange="NSE", underlying="NIFTY", year=2026, month=5) == [
        "2026-05-12",
        "2026-05-19",
    ]
    assert client.get_contracts(exchange="NSE", underlying="NIFTY", expiry=date(2026, 5, 12)) == [
        "NSE-NIFTY-12May26-24000-CE"
    ]
    assert api.calls == [
        (
            "expiries",
            {
                "exchange": "NSE",
                "underlying_symbol": "NIFTY",
                "year": 2026,
                "month": 5,
                "timeout": 4,
            },
        ),
        (
            "contracts",
            {
                "exchange": "NSE",
                "underlying_symbol": "NIFTY",
                "expiry_date": "2026-05-12",
                "timeout": 4,
            },
        ),
    ]


def test_a_backwards_candle_window_is_refused_before_any_request() -> None:
    api = HistoryApi()
    args = candle_args()
    args["start"], args["end"] = args["end"], args["start"]
    with pytest.raises(ValueError, match="before"):
        _SdkClient(api, timeout=5).get_candles(**args)
    assert api.calls == []


@pytest.mark.parametrize("payload", [{}, {"candles": []}])
def test_no_candles_is_an_empty_list(payload: Any) -> None:
    api = HistoryApi()
    api.candles = payload
    assert _SdkClient(api, timeout=5).get_candles(**candle_args()) == []


@pytest.mark.parametrize("payload", ["oops", None, [1], {"candles": "x"}, {"candles": [1, 2]}])
def test_malformed_candles_are_reported(payload: Any) -> None:
    api = HistoryApi()
    api.candles = payload
    with pytest.raises(UpstreamError):
        _SdkClient(api, timeout=5).get_candles(**candle_args())


@pytest.mark.parametrize("payload", ["oops", None, {"expiries": "x"}, {"expiries": [1]}])
def test_malformed_expiries_are_reported(payload: Any) -> None:
    api = HistoryApi()
    api.expiries = payload
    with pytest.raises(UpstreamError):
        _SdkClient(api, timeout=5).get_expiries(exchange="NSE", underlying="NIFTY", year=2026)


@pytest.mark.parametrize("payload", ["oops", None, {"contracts": "x"}, {"contracts": [None]}])
def test_malformed_contracts_are_reported(payload: Any) -> None:
    api = HistoryApi()
    api.contracts = payload
    with pytest.raises(UpstreamError):
        _SdkClient(api, timeout=5).get_contracts(
            exchange="NSE", underlying="NIFTY", expiry=date(2026, 5, 12)
        )


def test_no_expiries_or_contracts_is_an_empty_list() -> None:
    api = HistoryApi()
    api.expiries = {}
    api.contracts = {}
    client = _SdkClient(api, timeout=5)
    assert client.get_expiries(exchange="NSE", underlying="NIFTY", year=2026) == []
    assert client.get_contracts(exchange="NSE", underlying="NIFTY", expiry=date(2026, 5, 12)) == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GrowwAPIRateLimitException(), RateLimitedError),
        (GrowwAPITimeoutException(), UpstreamTimeoutError),
        (GrowwAPIAuthenticationException(), TokenRejectedError),
        (GrowwAPIException("bad request", "400"), UpstreamError),
    ],
)
def test_market_data_errors_are_translated(error: Exception, expected: type[TradingError]) -> None:
    api = HistoryApi()
    api.error = error
    with pytest.raises(expected):
        _SdkClient(api, timeout=5).get_candles(**candle_args())


def test_the_client_exposes_no_order_methods() -> None:
    names = {n for n in dir(_SdkClient) if not n.startswith("_")}
    assert not {n for n in names if "order" in n or "place" in n or "modify" in n or "cancel" in n}


def test_a_403_on_market_data_explains_the_likely_cause() -> None:
    api = HistoryApi()
    api.error = GrowwAPIException("Access forbidden for this request.", "403")
    with pytest.raises(ForbiddenError) as caught:
        _SdkClient(api, timeout=5).get_candles(**candle_args())
    assert "market data" in caught.value.message
    assert caught.value.http_status == 403
