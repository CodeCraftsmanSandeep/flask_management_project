"""HTTP routes: one HTML page plus a small, read-only JSON API."""

import logging
from datetime import datetime
from typing import Any, cast

from flask import Blueprint, Response, current_app, jsonify, render_template, request
from flask.typing import ResponseReturnValue
from werkzeug.exceptions import HTTPException

from .errors import TradingError
from .groww_service import GrowwService
from .timeutil import IST

logger = logging.getLogger(__name__)

bp = Blueprint("web", __name__)


def _service() -> GrowwService:
    return cast(GrowwService, current_app.extensions["groww"])


def _data(payload: Any) -> Response:
    """Wrap a Groww payload with the time it was fetched."""
    return jsonify({"data": payload, "fetched_at": datetime.now(IST).isoformat(timespec="seconds")})


@bp.get("/")
def index() -> str:
    return render_template("index.html")


@bp.get("/api/status")
def status() -> Response:
    return jsonify(_service().status())


@bp.post("/api/connect")
def connect() -> Response:
    return jsonify(_service().connect())


@bp.post("/api/disconnect")
def disconnect() -> Response:
    return jsonify(_service().disconnect())


@bp.get("/api/profile")
def profile() -> Response:
    return _data(_service().get_profile())


@bp.get("/api/funds")
def funds() -> Response:
    return _data(_service().get_funds())


@bp.get("/api/holdings")
def holdings() -> Response:
    return _data(_service().get_holdings())


@bp.get("/api/positions")
def positions() -> Response:
    return _data(_service().get_positions())


@bp.app_errorhandler(TradingError)
def handle_trading_error(exc: TradingError) -> ResponseReturnValue:
    return jsonify({"error": {"code": exc.code, "message": exc.message}}), exc.http_status


@bp.app_errorhandler(Exception)
def handle_unexpected(exc: Exception) -> ResponseReturnValue:
    """Anything we did not foresee: log it, and never leak internals to the page."""
    if isinstance(exc, HTTPException):  # pragma: no cover - the handler below is more specific
        return exc
    logger.exception("unhandled error on %s %s", request.method, request.path)
    if not request.path.startswith("/api/"):
        return "Something went wrong. Check the server log.", 500
    body = {"error": {"code": "internal_error", "message": "Something went wrong. See the log."}}
    return jsonify(body), 500


@bp.app_errorhandler(HTTPException)
def handle_http_error(exc: HTTPException) -> ResponseReturnValue:
    if not request.path.startswith("/api/"):
        return exc  # regular pages keep Werkzeug's standard HTML error page
    code = exc.name.lower().replace(" ", "_")
    body = {"error": {"code": code, "message": exc.description or exc.name}}
    return jsonify(body), exc.code or 500
