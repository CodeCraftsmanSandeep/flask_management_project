"""The broker accounts a user has added: who they are, which broker, and where the keys are kept.

Adding an account only *stores* it. Nothing here connects to a broker, reads a balance or places
an order; deploying a strategy on an account is a separate, later step with its own safeguards.

Storage is one JSON file (``secrets/accounts.json`` by default, git-ignored) created with
owner-only permissions and replaced atomically, so a crash can never leave half a file. Credentials
are kept there and nowhere else: they are never logged and the API only ever returns a masked tail.
"""

import contextlib
import json
import os
import re
import tempfile
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import BadRequestError, TradingError

MAX_ACCOUNTS = 20
MAX_LABEL = 40
MAX_VALUE = 200
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class BadAccountError(BadRequestError):
    """The account the user tried to add is not acceptable."""

    code = "bad_account"


class AccountNotFoundError(TradingError):
    code = "account_not_found"
    http_status = 404


class AccountsUnreadableError(TradingError):
    code = "accounts_unreadable"
    http_status = 500


@dataclass(frozen=True, slots=True)
class Field:
    name: str
    label: str
    secret: bool = True
    required: bool = True
    help: str = ""


@dataclass(frozen=True, slots=True)
class Broker:
    key: str
    name: str
    fields: tuple[Field, ...]
    note: str = ""
    max_accounts: int | None = None


BROKERS: dict[str, Broker] = {
    "zerodha": Broker(
        "zerodha",
        "Zerodha",
        (
            Field("api_key", "API key", help="From the Kite Connect developer console."),
            Field("api_secret", "API secret"),
        ),
        note=(
            "Zerodha asks you to log in again every trading day, so the daily login step comes "
            "with strategy deployment. Keys are stored now and not used yet."
        ),
    ),
    "groww": Broker(
        "groww",
        "Groww",
        (),
        note="Uses the TOTP token and secret in keys.txt, as before. One Groww account for now.",
        max_accounts=1,
    ),
    "other": Broker(
        "other",
        "Another broker",
        (
            Field(
                "broker_name", "Broker name", secret=False, help="For example Upstox or Angel One."
            ),
            Field("api_key", "API key", required=False),
            Field("api_secret", "API secret", required=False),
        ),
        note="Saved so you can keep all your accounts in one place. No connection is made yet.",
    ),
}


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    broker: str
    label: str
    created_at: str
    credentials: dict[str, str]


def mask(value: str) -> str:
    """Show that a value is set without showing it: the last four characters at most."""
    return (
        f"\u2022\u2022\u2022\u2022{value[-4:]}" if len(value) >= 12 else "\u2022\u2022\u2022\u2022"
    )


def public_view(account: Account) -> dict[str, Any]:
    """What the API may tell the page about an account: never a full secret."""
    broker = BROKERS[account.broker]
    secret_names = {f.name for f in broker.fields if f.secret}
    return {
        "id": account.id,
        "broker": account.broker,
        "broker_name": broker.name,
        "label": account.label,
        "created_at": account.created_at,
        "credentials": {
            name: mask(value) if name in secret_names else value
            for name, value in account.credentials.items()
        },
    }


def _clean(value: Any, what: str, *, limit: int) -> str:
    if not isinstance(value, str):
        msg = f"{what} must be text"
        raise BadAccountError(msg)
    text = value.strip()
    if _CONTROL.search(text):
        msg = f"{what} contains characters that are not allowed"
        raise BadAccountError(msg)
    if len(text) > limit:
        msg = f"{what} is too long (at most {limit} characters)"
        raise BadAccountError(msg)
    return text


class AccountBook:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ reading

    def accounts(self) -> list[Account]:
        with self._lock:
            return self._read()

    def _read(self) -> list[Account]:
        if not self._path.exists():
            return []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return [
                Account(
                    id=str(item["id"]),
                    broker=str(item["broker"]),
                    label=str(item["label"]),
                    created_at=str(item["created_at"]),
                    credentials={str(k): str(v) for k, v in item["credentials"].items()},
                )
                for item in raw["accounts"]
                if item["broker"] in BROKERS
            ]
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            # Never overwrite a file we cannot read: it may still hold the user's only copy.
            msg = (
                f"The accounts file {self._path.name} could not be read. Fix or move it and "
                "reload; nothing was changed."
            )
            raise AccountsUnreadableError(msg) from None

    # ------------------------------------------------------------------ changing

    def add(self, broker_key: Any, label: Any, credentials: Any) -> Account:
        if not isinstance(broker_key, str) or broker_key not in BROKERS:
            msg = "broker must be one of " + ", ".join(BROKERS)
            raise BadAccountError(msg)
        broker = BROKERS[broker_key]
        name = _clean(label, "The account name", limit=MAX_LABEL)
        if not name:
            msg = "Give the account a name"
            raise BadAccountError(msg)
        values = self._credentials(broker, credentials)

        with self._lock:
            accounts = self._read()
            if len(accounts) >= MAX_ACCOUNTS:
                msg = f"At most {MAX_ACCOUNTS} accounts can be added"
                raise BadAccountError(msg)
            if any(a.label.casefold() == name.casefold() for a in accounts):
                msg = f"An account called '{name}' already exists"
                raise BadAccountError(msg)
            if broker.max_accounts is not None:
                same = sum(1 for a in accounts if a.broker == broker.key)
                if same >= broker.max_accounts:
                    msg = f"Only {broker.max_accounts} {broker.name} account can be added for now"
                    raise BadAccountError(msg)
            account = Account(
                id=uuid.uuid4().hex[:10],
                broker=broker.key,
                label=name,
                created_at=datetime.now(UTC).isoformat(timespec="seconds"),
                credentials=values,
            )
            self._write([*accounts, account])
            return account

    def remove(self, account_id: str) -> None:
        with self._lock:
            accounts = self._read()
            kept = [a for a in accounts if a.id != account_id]
            if len(kept) == len(accounts):
                msg = "No such account"
                raise AccountNotFoundError(msg)
            self._write(kept)

    @staticmethod
    def _credentials(broker: Broker, given: Any) -> dict[str, str]:
        if given is None:
            given = {}
        if not isinstance(given, dict):
            msg = "credentials must be an object"
            raise BadAccountError(msg)
        known = {f.name: f for f in broker.fields}
        unknown = sorted(set(given) - set(known))
        if unknown:
            msg = f"{broker.name} has no field called {unknown[0]!r}"
            raise BadAccountError(msg)
        values: dict[str, str] = {}
        for spec in broker.fields:
            text = _clean(given.get(spec.name, ""), spec.label, limit=MAX_VALUE)
            if not text:
                if spec.required:
                    msg = f"{spec.label} is required for {broker.name}"
                    raise BadAccountError(msg)
                continue
            values[spec.name] = text
        return values

    def _write(self, accounts: list[Account]) -> None:
        folder = self._path.parent
        folder.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            folder.chmod(0o700)
        payload = {
            "version": 1,
            "accounts": [
                {
                    "id": a.id,
                    "broker": a.broker,
                    "label": a.label,
                    "created_at": a.created_at,
                    "credentials": a.credentials,
                }
                for a in accounts
            ],
        }
        # mkstemp creates the file 0600, so the secrets are never readable by others, even briefly.
        fd, tmp_name = tempfile.mkstemp(dir=folder, prefix=".accounts-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp_name).replace(self._path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
