import re
from pathlib import Path

import pytest
from flask.testing import FlaskClient

from app.errors import (
    ForbiddenError,
    LoginFailedError,
    RateLimitedError,
    TradingError,
    UpstreamError,
    UpstreamTimeoutError,
)
from tests.fakes import SECRET, TOKEN, FakeGateway

CSRF = {"X-Requested-With": "fetch"}
DATA_ENDPOINTS = ["/api/profile", "/api/funds", "/api/holdings", "/api/positions"]


def connect(client: FlaskClient) -> None:
    assert client.post("/api/connect", headers=CSRF).status_code == 200


# ----------------------------------------------------------------------- pages


def test_index_page_loads_with_security_headers(client: FlaskClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert b"Strategy Desk" in response.data
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_index_page_has_no_inline_script_style_or_handlers(client: FlaskClient) -> None:
    """The Content-Security-Policy forbids them, so they would silently break the page."""
    html = client.get("/").get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


UPWORK = "https://www.upwork.com/freelancers/~01d59b53fbdfc2b68b?viewMode=1"


def test_home_page_asks_the_two_scaling_questions(client: FlaskClient) -> None:
    html = client.get("/").get_data(as_text=True)
    assert "copying into 10 accounts on a single machine" in html
    assert "100 machines on VPS servers" in html


def test_home_page_has_no_sample_or_demo_wording(client: FlaskClient) -> None:
    text = client.get("/").get_data(as_text=True).lower()
    assert "sample" not in text
    assert "demo" not in text


@pytest.mark.parametrize("page", ["/", "/backtest", "/data"])
def test_every_page_links_out_to_the_upwork_profile_safely(client: FlaskClient, page: str) -> None:
    html = client.get(page).get_data(as_text=True)
    links = re.findall(r"<a [^>]*href=\"(https://www\.upwork\.com[^\"]*)\"[^>]*>", html)
    assert links
    assert set(links) == {UPWORK}
    for tag in re.findall(r"<a [^>]*upwork\.com[^>]*>", html):
        assert 'rel="noopener noreferrer"' in tag
        assert 'target="_blank"' in tag


def test_home_page_makes_no_profit_promises(client: FlaskClient) -> None:
    text = client.get("/").get_data(as_text=True).lower()
    for phrase in ("guaranteed", "risk-free", "risk free", "sure profit"):
        assert phrase not in text
    assert "multiplies losses as well as gains" in text


def test_the_contact_link_follows_the_configuration(tmp_path: Path) -> None:
    from app import create_app
    from app.config import Config

    app = create_app(Config(contact_url="https://example.com/me"))
    html = app.test_client().get("/", headers={"Host": "127.0.0.1"}).get_data(as_text=True)
    assert "https://example.com/me" in html
    assert "upwork.com" not in html


@pytest.mark.parametrize("asset", ["/static/app.js", "/static/style.css", "/static/landing.css"])
def test_static_assets_are_served(client: FlaskClient, asset: str) -> None:
    assert client.get(asset).status_code == 200


# ------------------------------------------------------------------ happy path


def test_status_before_connecting(client: FlaskClient) -> None:
    response = client.get("/api/status")
    assert response.get_json() == {
        "connected": False,
        "connected_at": None,
        "expires_at": None,
        "login_count": 0,
    }
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("path", DATA_ENDPOINTS)
def test_data_requires_connecting_first(client: FlaskClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "not_connected"


def test_connect_then_read_every_section(client: FlaskClient) -> None:
    connect(client)
    assert client.get("/api/status").get_json()["connected"] is True
    for path in DATA_ENDPOINTS:
        body = client.get(path).get_json()
        assert set(body) == {"data", "fetched_at"}
    assert client.get("/api/profile").get_json()["data"]["ucc"] == "FAKE-UCC"
    assert client.get("/api/funds").get_json()["data"]["clear_cash"] == 1000.5
    assert client.get("/api/holdings").get_json()["data"][0]["trading_symbol"] == "FAKE"
    assert client.get("/api/positions").get_json()["data"] == []


def test_disconnect_returns_to_the_not_connected_state(client: FlaskClient) -> None:
    connect(client)
    response = client.post("/api/disconnect", headers=CSRF)
    assert response.get_json()["connected"] is False
    assert client.get("/api/profile").status_code == 401


# --------------------------------------------------------------------- errors


def test_login_failure_is_reported_as_json(client: FlaskClient, gateway: FakeGateway) -> None:
    gateway.login_error = LoginFailedError("Groww rejected the login")
    response = client.post("/api/connect", headers=CSRF)
    assert response.status_code == 401
    assert response.get_json() == {
        "error": {"code": "login_failed", "message": "Groww rejected the login"}
    }
    assert client.get("/api/status").get_json()["connected"] is False


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (RateLimitedError("slow down"), 429, "rate_limited"),
        (UpstreamTimeoutError("too late"), 504, "upstream_timeout"),
        (UpstreamError("broken"), 502, "upstream_error"),
        (ForbiddenError("no plan"), 403, "forbidden"),
    ],
)
def test_groww_failures_map_to_http_statuses(
    client: FlaskClient, gateway: FakeGateway, error: TradingError, status: int, code: str
) -> None:
    connect(client)
    gateway.data_error = error
    response = client.get("/api/funds")
    assert response.status_code == status
    assert response.get_json() == {"error": {"code": code, "message": error.message}}


def test_unknown_api_path_returns_json(client: FlaskClient) -> None:
    response = client.get("/api/nope")
    assert response.status_code == 404
    assert response.get_json()["error"]["code"] == "not_found"


def test_wrong_method_returns_json(client: FlaskClient) -> None:
    response = client.get("/api/connect")
    assert response.status_code == 405
    assert response.get_json()["error"]["code"] == "method_not_allowed"


# ------------------------------------------------------------------- security


def test_post_requires_the_csrf_header(client: FlaskClient) -> None:
    response = client.post("/api/connect")
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "forbidden"
    assert client.get("/api/status").get_json()["login_count"] == 0  # nothing happened


@pytest.mark.parametrize("path", ["/", "/api/status"])
def test_untrusted_host_header_is_rejected(client: FlaskClient, path: str) -> None:
    """Defence against DNS rebinding: a hostile site resolving to 127.0.0.1."""
    assert client.get(path, headers={"Host": "evil.example"}).status_code == 400


@pytest.mark.parametrize("host", ["localhost", "localhost:5001", "127.0.0.1:5001", "[::1]:5001"])
def test_loopback_host_headers_are_accepted(client: FlaskClient, host: str) -> None:
    assert client.get("/api/status", headers={"Host": host}).status_code == 200


def test_no_response_ever_contains_credentials(client: FlaskClient, gateway: FakeGateway) -> None:
    bodies = [client.get("/").get_data(as_text=True)]
    bodies.append(client.post("/api/connect", headers=CSRF).get_data(as_text=True))
    bodies.append(client.get("/api/status").get_data(as_text=True))
    bodies.extend(client.get(path).get_data(as_text=True) for path in DATA_ENDPOINTS)
    everything = "\n".join(bodies)
    for secret in (TOKEN, SECRET, *gateway.issued_tokens):
        assert secret not in everything
