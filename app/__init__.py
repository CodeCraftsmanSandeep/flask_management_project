"""Strategy Desk: backtest a strategy on real NIFTY option data, next to your broker accounts."""

import logging

from flask import Flask
from flask.json.provider import DefaultJSONProvider

from .account_routes import bp as accounts_bp
from .accounts import AccountBook
from .backtest.routes import bp as backtest_bp
from .backtest.service import BacktestService
from .config import Config
from .credentials import load_credentials
from .groww_service import GrowwService
from .history.jobs import DownloadJob
from .routes import bp
from .security import register_security


def build_service(config: Config) -> GrowwService:
    """Wire the real thing: credentials from ``keys.txt`` + the official Groww SDK."""
    # Imported lazily: growwapi drags in pandas, which is slow to import and not needed by
    # tests that bring their own service.
    from .groww_sdk import SdkGateway

    keys_file = config.keys_file
    return GrowwService(
        lambda: load_credentials(keys_file),
        SdkGateway(timeout=config.request_timeout),
    )


def create_app(
    config: Config | None = None,
    *,
    service: GrowwService | None = None,
    backtest: BacktestService | None = None,
    download: DownloadJob | None = None,
    accounts: AccountBook | None = None,
) -> Flask:
    """Build the Flask app. Tests inject services wired to fakes; nothing else needs to.

    Nothing here opens a database connection: the database is only touched when a page asks for
    data, so the Account page still works with Postgres stopped.
    """
    config = config or Config.from_env()

    app = Flask(__name__)
    app.logger.setLevel(logging.INFO)
    if isinstance(app.json, DefaultJSONProvider):
        app.json.sort_keys = False  # keep the field order Groww sends
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # this API never needs a real request body

    groww = service or build_service(config)
    app.extensions["groww"] = groww
    app.extensions["backtest"] = backtest or BacktestService(config.database_url)
    app.extensions["download"] = download or DownloadJob(
        config.database_url, groww, lambda: groww.status()["connected"]
    )
    app.extensions["accounts"] = accounts or AccountBook(config.accounts_file)
    register_security(app, config)

    @app.context_processor
    def _site() -> dict[str, str]:
        return {"contact_url": config.contact_url}

    app.register_blueprint(bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(backtest_bp)
    return app
