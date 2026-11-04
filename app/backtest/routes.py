"""Pages and JSON for the backtest viewer and the data download.

Read-only with respect to Groww: the only state-changing endpoints start/stop the market-data
download. Results are revalidated with an ETag, so reopening a page is a cheap ``304``.
"""

import logging
from collections.abc import Mapping
from datetime import date
from typing import Any, cast

import psycopg
from flask import Blueprint, Response, current_app, jsonify, render_template, request
from flask.typing import ResponseReturnValue

from app.backtest.query import TIMEFRAMES, parse_filters
from app.backtest.service import BacktestService
from app.errors import BadRequestError
from app.history.jobs import DownloadJob

logger = logging.getLogger(__name__)

bp = Blueprint("backtest", __name__)

DATABASE_DOWN = "The local database is not running. Start it with `make db-start`."
DATABASE_STALE = "The database tables are missing or out of date. Run `make migrate`."


def _backtest() -> BacktestService:
    return cast(BacktestService, current_app.extensions["backtest"])


def _download() -> DownloadJob:
    return cast(DownloadJob, current_app.extensions["download"])


def _revalidated(payload: Mapping[str, Any]) -> Response:
    """JSON the browser may keep, but must re-check (a cheap 304 when nothing changed)."""
    response = jsonify(payload)
    response.cache_control.private = True
    response.cache_control.no_cache = True
    response.add_etag()
    response.make_conditional(request)  # turns this into a 304 when the ETag still matches
    return response


# ----------------------------------------------------------------------- pages


@bp.get("/backtest")
def backtest_page() -> str:
    return render_template("backtest.html", timeframes=TIMEFRAMES)


@bp.get("/data")
def data_page() -> str:
    return render_template("data.html")


# ------------------------------------------------------------------- backtest API


@bp.get("/api/backtest/meta")
def meta() -> Response:
    return _revalidated(_backtest().meta())


@bp.get("/api/backtest/trades")
def trades() -> Response:
    return _revalidated(_backtest().trades(parse_filters(request.args)))


@bp.get("/api/backtest/trade/<int:timeframe>/<int:index>")
def trade_detail(timeframe: int, index: int) -> Response:
    filters = parse_filters({**request.args.to_dict(), "timeframe": str(timeframe)})
    run = request.args.get("run") or None
    return _revalidated(_backtest().trade_detail(timeframe, index, run, filters))


def _day_arg(name: str) -> date | None:
    raw = request.args.get(name, "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        msg = f"{name} must look like 2026-05-12"
        raise BadRequestError(msg) from None


@bp.get("/api/backtest/skips")
def skips() -> Response:
    filters = parse_filters({"timeframe": request.args.get("timeframe", "")})
    return _revalidated(_backtest().skips(filters.timeframe, _day_arg("from"), _day_arg("to")))


# ---------------------------------------------------------------------- data API


@bp.get("/api/data/status")
def data_status() -> Response:
    return jsonify({**_backtest().data_status(), "job": _download().status()})


@bp.post("/api/data/download")
def data_download() -> Response:
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        msg = "The request body must be a JSON object."
        raise BadRequestError(msg)
    months = body.get("months", 12)
    timeframes = body.get("timeframes", list(TIMEFRAMES))
    end = body.get("end")
    if (
        isinstance(months, bool)
        or not isinstance(months, int)
        or not isinstance(timeframes, list)
        or not all(isinstance(t, int) and not isinstance(t, bool) for t in timeframes)
    ):
        msg = "months must be a whole number and timeframes a list of whole numbers."
        raise BadRequestError(msg)
    last: date | None = None
    if end is not None:
        try:
            last = date.fromisoformat(str(end))
        except ValueError:
            msg = "end must look like 2026-05-12"
            raise BadRequestError(msg) from None
    return jsonify(_download().start(months=months, timeframes=timeframes, end=last))


@bp.post("/api/data/stop")
def data_stop() -> Response:
    return jsonify(_download().stop())


# ------------------------------------------------------------------ database errors


@bp.app_errorhandler(psycopg.OperationalError)
def database_unavailable(exc: psycopg.OperationalError) -> ResponseReturnValue:
    logger.warning("database unavailable: %s", exc)
    body = {"error": {"code": "database_unavailable", "message": DATABASE_DOWN}}
    return jsonify(body), 503


@bp.app_errorhandler(psycopg.errors.UndefinedTable)
def database_not_migrated(exc: psycopg.errors.UndefinedTable) -> ResponseReturnValue:
    logger.warning("database not migrated: %s", exc)
    body = {"error": {"code": "database_not_migrated", "message": DATABASE_STALE}}
    return jsonify(body), 503
