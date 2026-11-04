import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime

import pyotp
import pytest

from app.credentials import TotpCredentials
from app.errors import (
    CredentialsError,
    LoginFailedError,
    NotConnectedError,
    TokenRejectedError,
    UpstreamError,
)
from app.groww_service import (
    FAILURE_COOLDOWN,
    TOTP_INTERVAL,
    GrowwService,
    jwt_expiry,
    next_daily_reset,
    to_iso,
)
from app.timeutil import IST
from tests.fakes import SECRET, TOKEN, WINDOW_START, FakeClock, FakeGateway, make_jwt

TOKEN_LIFETIME = 12 * 3600  # FakeGateway default


def code_at(timestamp: float) -> str:
    return pyotp.TOTP(SECRET).at(int(timestamp))


# ----------------------------------------------------------------- connecting


def test_data_calls_require_connect_first(service: GrowwService, gateway: FakeGateway) -> None:
    with pytest.raises(NotConnectedError):
        service.get_profile()
    assert gateway.login_calls == []


def test_connect_logs_in_with_the_current_totp(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    status = service.connect()
    assert gateway.login_calls == [(TOKEN, code_at(clock.now))]
    assert status["connected"] is True
    assert status["login_count"] == 1


def test_totp_matches_the_rfc_6238_test_vector(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    clock.now = float(WINDOW_START)
    service.connect()
    assert gateway.login_calls[0][1] == "005924"  # RFC 6238 appendix B: 89005924, 6 digits


def test_one_login_serves_every_later_call(service: GrowwService, gateway: FakeGateway) -> None:
    service.connect()
    for _ in range(3):
        service.get_profile()
        service.get_funds()
        service.get_holdings()
        service.get_positions()
    service.connect()
    assert len(gateway.login_calls) == 1


def test_failed_login_leaves_the_service_disconnected(
    service: GrowwService, gateway: FakeGateway
) -> None:
    gateway.login_error = LoginFailedError("nope")
    with pytest.raises(LoginFailedError):
        service.connect()
    assert service.status()["connected"] is False
    with pytest.raises(NotConnectedError):
        service.get_profile()
    assert len(gateway.login_calls) == 1  # the data call did not start another login


def test_credential_problems_surface_without_any_login_attempt(
    clock: FakeClock, gateway: FakeGateway
) -> None:
    def broken() -> TotpCredentials:
        msg = "keys.txt missing"
        raise CredentialsError(msg)

    service = GrowwService(broken, gateway, clock=clock, sleep=clock.sleep)
    with pytest.raises(CredentialsError):
        service.connect()
    assert gateway.login_calls == []


def test_disconnect_forgets_the_session(service: GrowwService) -> None:
    service.connect()
    status = service.disconnect()
    assert status == {
        "connected": False,
        "connected_at": None,
        "expires_at": None,
        "login_count": 1,
    }
    with pytest.raises(NotConnectedError):
        service.get_profile()


# -------------------------------------------------------------------- expiry


def test_expiry_is_read_from_the_jwt(service: GrowwService, clock: FakeClock) -> None:
    status = service.connect()
    assert status["expires_at"] == to_iso(clock.now + TOKEN_LIFETIME)


def test_expiry_falls_back_to_the_daily_reset_for_opaque_tokens(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    gateway.opaque_tokens = True
    assert service.connect()["expires_at"] == to_iso(next_daily_reset(clock.now))


def test_expired_session_renews_itself(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    service.connect()
    clock.advance(TOKEN_LIFETIME)
    assert service.get_profile()["ucc"] == "FAKE-UCC"
    assert len(gateway.login_calls) == 2


@pytest.mark.parametrize(
    ("hour", "minute", "day"),
    [(5, 59, 3), (6, 0, 4), (23, 30, 4), (0, 5, 3)],
)
def test_next_daily_reset_is_06_00_ist(hour: int, minute: int, day: int) -> None:
    now = datetime(2026, 11, 3, hour, minute, tzinfo=IST).timestamp()
    reset = datetime.fromtimestamp(next_daily_reset(now), IST)
    assert (reset.day, reset.hour, reset.minute) == (day, 6, 0)


def test_jwt_expiry_reads_the_exp_claim() -> None:
    assert jwt_expiry(make_jwt(1_900_000_000)) == 1_900_000_000.0


@pytest.mark.parametrize("token", ["", "not-a-jwt", "a.b.c", "a.e30.c", "a.!!!.c"])
def test_jwt_expiry_is_none_for_unreadable_tokens(token: str) -> None:
    assert jwt_expiry(token) is None


# ------------------------------------------------------------ rejected tokens


def test_rejected_token_triggers_one_relogin_and_a_retry(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    service.connect()
    gateway.revoke_all()
    assert service.get_funds()["clear_cash"] == 1000.5
    assert len(gateway.login_calls) == 2
    assert gateway.login_calls[0][1] != gateway.login_calls[1][1]  # new TOTP window, not a replay
    assert len(clock.sleeps) == 1


def test_a_second_rejection_is_reported_instead_of_looping(
    service: GrowwService, gateway: FakeGateway
) -> None:
    service.connect()
    gateway.reject_all = True
    with pytest.raises(TokenRejectedError):
        service.get_profile()
    assert len(gateway.login_calls) == 2


def test_concurrent_requests_share_a_single_relogin(
    service: GrowwService, gateway: FakeGateway
) -> None:
    service.connect()
    gateway.revoke_all()
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.get_profile(), range(8)))
    assert all(result["ucc"] == "FAKE-UCC" for result in results)
    assert len(gateway.login_calls) == 2


def test_a_late_rejection_does_not_discard_a_newer_session(
    service: GrowwService, gateway: FakeGateway
) -> None:
    """Thread B holds an old client; thread A has already logged in again when B's call fails."""
    service.connect()
    stale_token = gateway.issued_tokens[0]
    gateway.revoke_all()

    stale_call_started = threading.Event()
    relogin_done = threading.Event()
    paused = False

    def pause_the_first_stale_call(token: str) -> None:
        nonlocal paused
        if token == stale_token and not paused:
            paused = True
            stale_call_started.set()
            assert relogin_done.wait(timeout=5)

    gateway.on_data_call = pause_the_first_stale_call
    with ThreadPoolExecutor(max_workers=1) as pool:
        late = pool.submit(service.get_profile)  # B: stuck mid-call with the stale client
        assert stale_call_started.wait(timeout=5)
        assert service.get_profile()["ucc"] == "FAKE-UCC"  # A: rejected, logs in again, succeeds
        relogin_done.set()
        assert late.result(timeout=5)["ucc"] == "FAKE-UCC"  # B: must reuse A's fresh session
    assert len(gateway.login_calls) == 2


def test_failed_automatic_relogin_is_not_hammered(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    service.connect()
    gateway.revoke_all()
    gateway.login_error = LoginFailedError("nope")

    with pytest.raises(LoginFailedError):
        service.get_profile()
    assert len(gateway.login_calls) == 2

    with pytest.raises(LoginFailedError, match="nope"):
        service.get_profile()
    assert len(gateway.login_calls) == 2  # cooldown: answered from the remembered failure

    clock.advance(FAILURE_COOLDOWN + 1)
    gateway.login_error = None
    assert service.get_profile()["ucc"] == "FAKE-UCC"
    assert len(gateway.login_calls) == 3


def test_explicit_connect_ignores_the_failure_cooldown(
    service: GrowwService, gateway: FakeGateway
) -> None:
    gateway.login_error = LoginFailedError("nope")
    with pytest.raises(LoginFailedError):
        service.connect()
    gateway.login_error = None
    assert service.connect()["connected"] is True


# ----------------------------------------------------------------- TOTP timing


def test_totp_waits_for_the_next_window_when_the_current_one_is_nearly_over(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    clock.now = WINDOW_START + 28.5  # only 1.5 s of validity left
    service.connect()
    assert clock.sleeps == [pytest.approx(1.75)]
    assert gateway.login_calls[0][1] == code_at(WINDOW_START + TOTP_INTERVAL)


def test_a_totp_window_is_not_reused_after_a_successful_login(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    service.connect()
    service.disconnect()
    service.connect()
    first, second = (call[1] for call in gateway.login_calls)
    assert first != second
    assert len(clock.sleeps) == 1


# --------------------------------------------------------------------- secrets


def test_secrets_are_scrubbed_from_error_messages(
    service: GrowwService, gateway: FakeGateway
) -> None:
    gateway.login_error = UpstreamError(f"boom {TOKEN} / {SECRET}")
    with pytest.raises(UpstreamError) as info:
        service.connect()
    assert TOKEN not in info.value.message
    assert SECRET not in info.value.message
    assert TOKEN not in str(info.value)
    assert "***" in info.value.message


# ------------------------------------------------------------------ market data


def test_market_data_calls_need_a_connection(service: GrowwService) -> None:
    with pytest.raises(NotConnectedError):
        service.get_expiries(exchange="NSE", underlying="NIFTY", year=2026)


def test_market_data_passes_through_the_session(
    service: GrowwService, gateway: FakeGateway
) -> None:
    gateway.expiries = ["2026-05-12", "2025-01-02"]
    gateway.contracts = {"2026-05-12": ["NSE-NIFTY-12May26-24000-CE"]}
    gateway.candles = {"NSE-NIFTY": [["2026-05-12T09:15:00", 1, 2, 0.5, 1.5, 3, None]]}
    service.connect()
    start, end = datetime(2026, 5, 12, 9, 15), datetime(2026, 5, 12, 15, 30)
    assert service.get_expiries(exchange="NSE", underlying="NIFTY", year=2026) == ["2026-05-12"]
    assert service.get_contracts(exchange="NSE", underlying="NIFTY", expiry=date(2026, 5, 12)) == [
        "NSE-NIFTY-12May26-24000-CE"
    ]
    rows = service.get_candles(
        exchange="NSE", segment="CASH", symbol="NSE-NIFTY", start=start, end=end
    )
    assert rows == [["2026-05-12T09:15:00", 1, 2, 0.5, 1.5, 3, None]]
    assert gateway.candle_calls == [("NSE", "CASH", "NSE-NIFTY", start, end)]
    assert len(gateway.login_calls) == 1  # one login serves every call


def test_a_rejected_token_during_a_candle_call_logs_in_again_once(
    service: GrowwService, gateway: FakeGateway, clock: FakeClock
) -> None:
    service.connect()
    gateway.revoke_all()
    clock.advance(31)  # next TOTP window, so the second login is allowed
    rows = service.get_candles(
        exchange="NSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        start=datetime(2026, 5, 12, 9, 15),
        end=datetime(2026, 5, 12, 9, 20),
    )
    assert rows == []
    assert len(gateway.login_calls) == 2
