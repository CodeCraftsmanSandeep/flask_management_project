"""Broker accounts: validation, safe storage, masked output, and the API on top."""

import json
import stat
from pathlib import Path
from typing import Any

import pytest
from flask.testing import FlaskClient

from app import create_app
from app.accounts import (
    BROKERS,
    MAX_ACCOUNTS,
    AccountBook,
    AccountNotFoundError,
    AccountsUnreadableError,
    BadAccountError,
    mask,
    public_view,
)
from app.config import Config
from app.groww_service import GrowwService

CSRF = {"X-Requested-With": "fetch"}
SECRET = "kite-secret-9f8e7d6c5b4a"
ZERODHA = {"api_key": "kite-key-1234567890ab", "api_secret": SECRET}


@pytest.fixture
def book(tmp_path: Path) -> AccountBook:
    return AccountBook(tmp_path / "secrets" / "accounts.json")


@pytest.fixture
def web(service: GrowwService, tmp_path: Path) -> FlaskClient:
    app = create_app(
        Config(), service=service, accounts=AccountBook(tmp_path / "secrets" / "accounts.json")
    )
    app.testing = True
    return app.test_client()


# ------------------------------------------------------------------------------ the book


def test_an_empty_book_lists_nothing_and_creates_no_file(book: AccountBook, tmp_path: Path) -> None:
    assert book.accounts() == []
    assert not (tmp_path / "secrets").exists()


def test_an_added_account_is_listed_and_survives_a_reload(
    book: AccountBook, tmp_path: Path
) -> None:
    added = book.add("zerodha", "  My Zerodha ", ZERODHA)
    assert added.label == "My Zerodha"
    again = AccountBook(tmp_path / "secrets" / "accounts.json").accounts()
    assert [a.id for a in again] == [added.id]
    assert again[0].credentials == ZERODHA


def test_the_file_is_owner_only_and_leaves_no_temp_files(book: AccountBook, tmp_path: Path) -> None:
    book.add("zerodha", "Z", ZERODHA)
    folder = tmp_path / "secrets"
    mode = stat.S_IMODE((folder / "accounts.json").stat().st_mode)
    assert mode == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert [p.name for p in folder.iterdir()] == ["accounts.json"]


def test_credentials_are_masked_in_the_public_view(book: AccountBook) -> None:
    account = book.add("zerodha", "Z", ZERODHA)
    shown = json.dumps(public_view(account))
    assert SECRET not in shown
    assert ZERODHA["api_key"] not in shown
    assert "ab" in shown  # the last four characters are still shown
    assert public_view(account)["credentials"]["api_secret"] == mask(SECRET)


def test_mask_never_shows_a_short_value(book: AccountBook) -> None:
    assert mask("short") == "\u2022\u2022\u2022\u2022"
    assert mask("x" * 11) == "\u2022\u2022\u2022\u2022"
    assert mask("abcdefghijkl") == "\u2022\u2022\u2022\u2022ijkl"


def test_a_plain_field_is_shown_as_typed(book: AccountBook) -> None:
    account = book.add("other", "Upstox", {"broker_name": "Upstox", "api_key": "k" * 20})
    view = public_view(account)["credentials"]
    assert view["broker_name"] == "Upstox"
    assert "k" * 20 not in json.dumps(view)


def test_removing_an_account_removes_only_that_one(book: AccountBook) -> None:
    keep = book.add("zerodha", "A", ZERODHA)
    drop = book.add("other", "B", {"broker_name": "X"})
    book.remove(drop.id)
    assert [a.id for a in book.accounts()] == [keep.id]
    with pytest.raises(AccountNotFoundError):
        book.remove(drop.id)


@pytest.mark.parametrize(
    ("broker", "label", "credentials", "message"),
    [
        ("nope", "A", {}, "broker must be one of"),
        (None, "A", {}, "broker must be one of"),
        ("zerodha", "", ZERODHA, "Give the account a name"),
        ("zerodha", "   ", ZERODHA, "Give the account a name"),
        ("zerodha", 5, ZERODHA, "must be text"),
        ("zerodha", "x" * 41, ZERODHA, "too long"),
        ("zerodha", "bad\nname", ZERODHA, "not allowed"),
        ("zerodha", "A", {"api_key": "k"}, "API secret is required"),
        ("zerodha", "A", {}, "API key is required"),
        ("zerodha", "A", None, "API key is required"),
        ("zerodha", "A", "text", "credentials must be an object"),
        ("zerodha", "A", {**ZERODHA, "extra": "x"}, "no field called 'extra'"),
        ("zerodha", "A", {**ZERODHA, "api_key": 12345}, "must be text"),
        ("zerodha", "A", {**ZERODHA, "api_key": "k" * 201}, "too long"),
        ("zerodha", "A", {**ZERODHA, "api_secret": "a\tb"}, "not allowed"),
        ("other", "A", {}, "Broker name is required"),
    ],
)
def test_bad_accounts_are_refused_with_a_clear_message(
    book: AccountBook, broker: Any, label: Any, credentials: Any, message: str
) -> None:
    with pytest.raises(BadAccountError) as caught:
        book.add(broker, label, credentials)
    assert message in caught.value.message
    assert caught.value.http_status == 400
    assert book.accounts() == []


def test_names_are_unique_ignoring_case(book: AccountBook) -> None:
    book.add("zerodha", "Main", ZERODHA)
    with pytest.raises(BadAccountError, match="already exists"):
        book.add("other", "main", {"broker_name": "X"})


def test_only_one_groww_account_uses_keys_txt(book: AccountBook) -> None:
    book.add("groww", "Groww", {})
    with pytest.raises(BadAccountError, match="Only 1 Groww account"):
        book.add("groww", "Groww 2", {})


def test_a_groww_account_stores_no_credentials(book: AccountBook) -> None:
    account = book.add("groww", "Groww", {})
    assert account.credentials == {}
    with pytest.raises(BadAccountError, match="no field called"):
        book.add("groww", "Other", {"totp_token": "x"})


def test_the_number_of_accounts_is_capped(book: AccountBook) -> None:
    for number in range(MAX_ACCOUNTS):
        book.add("other", f"Account {number}", {"broker_name": "X"})
    with pytest.raises(BadAccountError, match=f"At most {MAX_ACCOUNTS}"):
        book.add("other", "One too many", {"broker_name": "X"})


def test_an_unreadable_file_is_never_overwritten(book: AccountBook, tmp_path: Path) -> None:
    path = tmp_path / "secrets" / "accounts.json"
    path.parent.mkdir()
    path.write_text("{ this is not json", encoding="utf-8")
    with pytest.raises(AccountsUnreadableError) as caught:
        book.accounts()
    assert "this is not json" not in caught.value.message
    with pytest.raises(AccountsUnreadableError):
        book.add("zerodha", "A", ZERODHA)
    assert path.read_text(encoding="utf-8") == "{ this is not json"


@pytest.mark.parametrize(
    "content", ["[]", '{"accounts": 5}', '{"accounts": [{"id": "1"}]}', "null"]
)
def test_a_file_of_the_wrong_shape_is_reported_not_crashed_on(
    book: AccountBook, tmp_path: Path, content: str
) -> None:
    path = tmp_path / "secrets" / "accounts.json"
    path.parent.mkdir()
    path.write_text(content, encoding="utf-8")
    with pytest.raises(AccountsUnreadableError):
        book.accounts()


def test_every_broker_field_has_a_label() -> None:
    for broker in BROKERS.values():
        assert broker.name
        assert all(f.label for f in broker.fields)


# ------------------------------------------------------------------------------- the API


def test_the_list_describes_the_brokers_and_hides_secrets(web: FlaskClient) -> None:
    body = web.get("/api/accounts").get_json()
    assert [b["key"] for b in body["brokers"]] == ["zerodha", "groww", "other"]
    zerodha = body["brokers"][0]
    assert [f["name"] for f in zerodha["fields"]] == ["api_key", "api_secret"]
    assert body["accounts"] == []
    assert web.get("/api/accounts").headers["Cache-Control"] == "no-store"


def test_adding_an_account_returns_it_masked_with_a_status(web: FlaskClient) -> None:
    response = web.post(
        "/api/accounts",
        json={"broker": "zerodha", "label": "Main", "credentials": ZERODHA},
        headers=CSRF,
    )
    assert response.status_code == 201
    created = response.get_json()
    assert created["status"] == "saved"
    assert SECRET not in response.get_data(as_text=True)
    listed = web.get("/api/accounts")
    assert SECRET not in listed.get_data(as_text=True)
    assert [a["id"] for a in listed.get_json()["accounts"]] == [created["id"]]


def test_a_groww_account_reflects_the_live_connection(
    web: FlaskClient, service: GrowwService
) -> None:
    web.post("/api/accounts", json={"broker": "groww", "label": "Groww"}, headers=CSRF)
    assert web.get("/api/accounts").get_json()["accounts"][0]["status"] == "not_connected"
    assert web.post("/api/connect", headers=CSRF).status_code == 200
    assert web.get("/api/accounts").get_json()["accounts"][0]["status"] == "connected"


def test_changing_accounts_needs_the_csrf_header(web: FlaskClient) -> None:
    body = {"broker": "other", "label": "X", "credentials": {"broker_name": "X"}}
    assert web.post("/api/accounts", json=body).status_code == 403
    assert web.delete("/api/accounts/abc").status_code == 403
    assert web.get("/api/accounts").get_json()["accounts"] == []


@pytest.mark.parametrize("body", [None, [], "text", {"broker": "zerodha"}, {"label": "A"}])
def test_a_bad_request_is_a_400_and_stores_nothing(web: FlaskClient, body: Any) -> None:
    response = web.post("/api/accounts", json=body, headers=CSRF)
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_account"
    assert web.get("/api/accounts").get_json()["accounts"] == []


def test_a_body_that_is_not_json_is_a_400(web: FlaskClient) -> None:
    response = web.post(
        "/api/accounts", data="not json", headers={**CSRF, "Content-Type": "application/json"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "bad_account"


def test_removing_an_account_and_a_missing_one(web: FlaskClient) -> None:
    made = web.post(
        "/api/accounts",
        json={"broker": "other", "label": "X", "credentials": {"broker_name": "X"}},
        headers=CSRF,
    ).get_json()
    assert web.delete(f"/api/accounts/{made['id']}", headers=CSRF).get_json() == {
        "removed": made["id"]
    }
    missing = web.delete(f"/api/accounts/{made['id']}", headers=CSRF)
    assert missing.status_code == 404
    assert missing.get_json()["error"]["code"] == "account_not_found"
    assert web.get("/api/accounts").get_json()["accounts"] == []


def test_an_unreadable_accounts_file_is_a_clear_500_not_a_crash(
    service: GrowwService, tmp_path: Path
) -> None:
    path = tmp_path / "accounts.json"
    path.write_text("garbage", encoding="utf-8")
    app = create_app(Config(), service=service, accounts=AccountBook(path))
    app.testing = True
    response = app.test_client().get("/api/accounts")
    assert response.status_code == 500
    assert response.get_json()["error"]["code"] == "accounts_unreadable"
    assert "garbage" not in response.get_data(as_text=True)
