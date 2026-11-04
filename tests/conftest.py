import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from flask.testing import FlaskClient

from app import create_app
from app.accounts import AccountBook
from app.config import Config
from app.credentials import TotpCredentials
from app.db.migrate import migrate
from app.groww_service import GrowwService
from tests.fakes import SECRET, TOKEN, FakeClock, FakeGateway


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def gateway(clock: FakeClock) -> FakeGateway:
    return FakeGateway(clock)


@pytest.fixture
def service(clock: FakeClock, gateway: FakeGateway) -> GrowwService:
    return GrowwService(
        lambda: TotpCredentials(token=TOKEN, secret=SECRET),
        gateway,
        clock=clock,
        sleep=clock.sleep,
    )


@pytest.fixture
def client(service: GrowwService, tmp_path: Path) -> FlaskClient:
    # accounts live in a temp file: no test may ever touch the real secrets/accounts.json
    app = create_app(Config(), service=service, accounts=AccountBook(tmp_path / "accounts.json"))
    app.testing = True
    return app.test_client()


# ------------------------------------------------------------------ temporary databases
#
# DB tests never touch the real ``trading`` database: a migrated template is built once per test
# session and every test gets its own throw-away copy (CREATE DATABASE ... TEMPLATE is fast).

ADMIN_URL = os.environ.get("TEST_ADMIN_URL", "postgresql://trading@127.0.0.1:5433/postgres")


def _url_for(name: str) -> str:
    return ADMIN_URL.rsplit("/", 1)[0] + "/" + name


def _admin(sql: str) -> None:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as conn:
            conn.execute(sql)
    except psycopg.OperationalError as exc:
        pytest.fail(f"Postgres is not reachable for the DB tests ({exc}). Run `make db-start`.")


@pytest.fixture(scope="session")
def template_db() -> Iterator[str]:
    name = f"tpl_{uuid.uuid4().hex[:12]}"
    _admin(f'CREATE DATABASE "{name}"')
    try:
        migrate(_url_for(name))
        yield name
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def db_url(template_db: str) -> Iterator[str]:
    name = f"t_{uuid.uuid4().hex[:12]}"
    _admin(f'CREATE DATABASE "{name}" TEMPLATE "{template_db}"')
    try:
        yield _url_for(name)
    finally:
        _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
