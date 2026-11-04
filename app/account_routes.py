"""The accounts API: list, add and remove broker accounts. Credentials never leave the server."""

from dataclasses import asdict
from typing import Any, cast

from flask import Blueprint, Response, current_app, jsonify, request

from .accounts import BROKERS, AccountBook, BadAccountError, public_view
from .groww_service import GrowwService

bp = Blueprint("accounts", __name__)


def _book() -> AccountBook:
    return cast(AccountBook, current_app.extensions["accounts"])


def _status(broker: str) -> str:
    """``connected`` / ``not_connected`` where the app can tell, otherwise ``saved``."""
    if broker == "groww":
        service = cast(GrowwService, current_app.extensions["groww"])
        return "connected" if service.status()["connected"] else "not_connected"
    return "saved"


@bp.get("/api/accounts")
def list_accounts() -> Response:
    brokers = [
        {
            "key": b.key,
            "name": b.name,
            "note": b.note,
            "max_accounts": b.max_accounts,
            "fields": [asdict(f) for f in b.fields],
        }
        for b in BROKERS.values()
    ]
    accounts = [{**public_view(a), "status": _status(a.broker)} for a in _book().accounts()]
    return jsonify({"brokers": brokers, "accounts": accounts})


@bp.post("/api/accounts")
def add_account() -> tuple[Response, int]:
    body: Any = request.get_json(silent=True)
    if not isinstance(body, dict):
        msg = "Send the account as a JSON object"
        raise BadAccountError(msg)
    account = _book().add(body.get("broker"), body.get("label"), body.get("credentials"))
    return jsonify({**public_view(account), "status": _status(account.broker)}), 201


@bp.delete("/api/accounts/<account_id>")
def remove_account(account_id: str) -> Response:
    _book().remove(account_id)
    return jsonify({"removed": account_id})
