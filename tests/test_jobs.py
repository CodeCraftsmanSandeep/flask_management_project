"""The background download: start/stop/status, and that failures are reported, not hidden."""

import threading
from collections.abc import Callable
from datetime import date
from functools import partial
from typing import Any

import pytest

from app.db.connection import session
from app.errors import BadRequestError, ForbiddenError, NotConnectedError
from app.history.backfill import SPOT_SYMBOL, Backfill, Throttle
from app.history.jobs import DownloadJob, JobRunningError, months_back
from app.history.store import Store
from tests.test_backfill import TODAY, FakeSource

LAST_DAY = date(2026, 5, 19)  # the day before the fake "today"
WAIT = 20  # seconds; only reached if something hangs


def make_job(
    db_url: str, source: FakeSource, *, connected: Callable[[], bool] = lambda: True
) -> DownloadJob:
    backfill = partial(
        Backfill,
        throttle=Throttle(interval=0),
        sleep=lambda _seconds: None,
        today=lambda: TODAY,
    )
    return DownloadJob(db_url, source, connected, today=lambda: TODAY, make_backfill=backfill)


def spot_range(db_url: str) -> tuple[date, date] | None:
    with session(db_url) as conn:
        return Store(conn).spot_range(SPOT_SYMBOL)


# ----------------------------------------------------------------------- validation


@pytest.mark.parametrize("months", [0, -1, 25])
def test_months_out_of_range_are_rejected(db_url: str, months: int) -> None:
    job = make_job(db_url, FakeSource())
    with pytest.raises(BadRequestError):
        job.start(months=months)
    assert job.status()["state"] == "idle"


@pytest.mark.parametrize("timeframes", [[], [7], [5, 7]])
def test_unsupported_timeframes_are_rejected(db_url: str, timeframes: list[int]) -> None:
    with pytest.raises(BadRequestError):
        make_job(db_url, FakeSource()).start(timeframes=timeframes)


@pytest.mark.parametrize("end", [TODAY, date(2026, 5, 21)])
def test_a_download_cannot_end_today_or_later(db_url: str, end: date) -> None:
    with pytest.raises(BadRequestError, match="before today"):
        make_job(db_url, FakeSource()).start(end=end)


def test_it_asks_the_user_to_connect_first(db_url: str) -> None:
    job = make_job(db_url, FakeSource(), connected=lambda: False)
    with pytest.raises(NotConnectedError, match="Connect"):
        job.start()
    assert job.status()["state"] == "idle"


# --------------------------------------------------------------------------- running


def test_a_finished_download_reports_what_it_did_and_stores_the_data(db_url: str) -> None:
    job = make_job(db_url, FakeSource())
    started = job.start(months=1, timeframes=[5])
    assert started["state"] in ("running", "done")
    assert started["request"] == {
        "months": 1,
        "timeframes": [5],
        "from": months_back(LAST_DAY, 1).isoformat(),
        "to": LAST_DAY.isoformat(),
    }
    assert job.wait(WAIT)

    status = job.status()
    assert status["state"] == "done"
    assert status["active"] is False
    assert status["error"] is None
    assert status["finished_at"]
    assert status["started_at"]
    assert status["summary"]["phases"]["spot"]["fetched"] >= 1
    assert spot_range(db_url) is not None


def test_it_can_run_again_after_finishing_and_the_second_run_reuses_stored_data(
    db_url: str,
) -> None:
    source = FakeSource()
    job = make_job(db_url, source)
    job.start(months=1, timeframes=[5])
    assert job.wait(WAIT)
    candle_requests = source.count("candles")
    assert candle_requests > 0

    job.start(months=1, timeframes=[5])
    assert job.wait(WAIT)
    assert job.status()["state"] == "done"
    assert job.status()["summary"]["phases"]["spot"]["cached"] > 0
    assert source.count("candles") == candle_requests  # nothing was downloaded twice


# ----------------------------------------------------------------------- stop / busy


def test_a_second_start_is_refused_while_running_and_stop_is_graceful(db_url: str) -> None:
    source = FakeSource()
    reached = threading.Event()
    gate = threading.Event()

    def block() -> None:
        reached.set()
        gate.wait(WAIT)

    source.on_call = block
    job = make_job(db_url, source)
    job.start(months=1, timeframes=[5])
    try:
        assert reached.wait(WAIT)
        assert job.status()["active"] is True
        with pytest.raises(JobRunningError, match="already running"):
            job.start(months=1)

        assert job.stop()["state"] == "stopping"
        assert job.status()["active"] is True  # still finishing its current request
    finally:
        gate.set()
    assert job.wait(WAIT)
    status = job.status()
    assert status["state"] == "stopped"
    assert status["error"] is None
    assert status["summary"] is not None


def test_stop_does_nothing_when_idle(db_url: str) -> None:
    job = make_job(db_url, FakeSource())
    assert job.stop()["state"] == "idle"


# --------------------------------------------------------------------------- failures


def test_a_groww_refusal_is_reported_in_plain_words(db_url: str) -> None:
    source = FakeSource()
    source.fail_forever["expiries"] = ForbiddenError("Groww will not release market data.")
    job = make_job(db_url, source)
    job.start(months=1, timeframes=[5])
    assert job.wait(WAIT)
    status = job.status()
    assert status["state"] == "failed"
    assert status["error"] == "Groww will not release market data."
    assert status["active"] is False


def test_unexpected_errors_do_not_leak_details_to_the_page(db_url: str) -> None:
    source = FakeSource()
    source.fail_forever["expiries"] = RuntimeError("password=hunter2 in /private/path")
    job = make_job(db_url, source)
    job.start(months=1, timeframes=[5])
    assert job.wait(WAIT)
    status: dict[str, Any] = job.status()
    assert status["state"] == "failed"
    assert "hunter2" not in str(status)
    assert "server log" in status["error"]


def test_a_failed_download_can_be_started_again(db_url: str) -> None:
    source = FakeSource()
    source.fail_forever["expiries"] = ForbiddenError("no")
    job = make_job(db_url, source)
    job.start(months=1, timeframes=[5])
    assert job.wait(WAIT)
    assert job.status()["state"] == "failed"

    del source.fail_forever["expiries"]
    job.start(months=1, timeframes=[5])
    assert job.wait(WAIT)
    status = job.status()
    assert status["state"] == "done"
    assert status["error"] is None  # the old error does not linger
