"""The Data page's "Download" button: one background download at a time, with progress and Stop.

The job runs :class:`~app.history.backfill.Backfill` on a worker thread with its own database
connection. It only ever calls Groww's read-only market-data endpoints. The browser polls
:meth:`DownloadJob.status`; nothing here blocks a web request.
"""

import calendar
import logging
import threading
from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from app.backtest.query import TIMEFRAMES
from app.db.connection import connect
from app.errors import BadRequestError, NotConnectedError, TradingError
from app.history.backfill import Backfill, BackfillReport, MarketData, Progress
from app.history.store import Store
from app.strategy.engine import StrategyParams
from app.timeutil import IST

logger = logging.getLogger(__name__)

MAX_MONTHS = 24
MAX_PROBLEMS_SHOWN = 20


class JobRunningError(TradingError):
    """A download is already in progress."""

    code = "job_running"
    http_status = 409


class JobState(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    STOPPING = "stopping"
    DONE = "done"
    STOPPED = "stopped"
    FAILED = "failed"


def months_back(end: date, months: int) -> date:
    """First day of a window of *months* months that ends on *end* (inclusive).

    12 months ending 2026-05-12 start on 2025-05-13; the day is clamped for short months.
    """
    year, month_index = divmod(end.year * 12 + end.month - 1 - months, 12)
    month = month_index + 1
    day = min(end.day, calendar.monthrange(year, month)[1])
    return date(year, month, day) + timedelta(days=1)


def _summarise(report: BackfillReport) -> dict[str, Any]:
    return {
        "phases": {
            name: {
                "fetched": phase.fetched,
                "empty": phase.empty,
                "cached": phase.cached,
                "errors": phase.errors,
            }
            for name, phase in report.phases.items()
        },
        "problems": report.problems[:MAX_PROBLEMS_SHOWN],
        "problems_total": len(report.problems),
    }


class DownloadJob:
    def __init__(
        self,
        database_url: str,
        source: MarketData,
        is_connected: Callable[[], bool],
        *,
        today: Callable[[], date] = date.today,
        make_backfill: Callable[..., Backfill] = Backfill,
    ) -> None:
        self._url = database_url
        self._source = source
        self._is_connected = is_connected
        self._today = today
        self._make_backfill = make_backfill
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = JobState.IDLE
        self._progress = Progress("", 0, 0)
        self._request: dict[str, Any] | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._error: str | None = None
        self._summary: dict[str, Any] | None = None

    # ------------------------------------------------------------------ control

    def start(
        self, months: int = 12, timeframes: Sequence[int] = TIMEFRAMES, end: date | None = None
    ) -> dict[str, Any]:
        if not 1 <= months <= MAX_MONTHS:
            msg = f"months must be between 1 and {MAX_MONTHS}."
            raise BadRequestError(msg)
        chosen = sorted(set(timeframes))
        if not chosen or any(tf not in TIMEFRAMES for tf in chosen):
            msg = f"timeframes must be chosen from {', '.join(map(str, TIMEFRAMES))}."
            raise BadRequestError(msg)
        last = end or self._today() - timedelta(days=1)
        if last >= self._today():
            msg = "The last day must be before today: today's session is not finished."
            raise BadRequestError(msg)
        if not self._is_connected():
            msg = "Connect to Groww first (the Connect button), then start the download."
            raise NotConnectedError(msg)

        with self._lock:
            if self._state in (JobState.RUNNING, JobState.STOPPING):
                msg = "A download is already running."
                raise JobRunningError(msg)
            first = months_back(last, months)
            self._stop.clear()
            self._state = JobState.RUNNING
            self._progress = Progress("starting", 0, 0)
            self._request = {
                "months": months,
                "timeframes": chosen,
                "from": first.isoformat(),
                "to": last.isoformat(),
            }
            self._started_at = datetime.now(IST).isoformat(timespec="seconds")
            self._finished_at = None
            self._error = None
            self._summary = None
            self._thread = threading.Thread(
                target=self._run, args=(first, last, chosen), name="download", daemon=True
            )
            self._thread.start()
        return self.status()

    def stop(self) -> dict[str, Any]:
        """Ask the running download to stop after its current request (finished work is kept)."""
        with self._lock:
            if self._state is JobState.RUNNING:
                self._state = JobState.STOPPING
                self._stop.set()
        return self.status()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the worker ends (used by tests); True when it has."""
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ------------------------------------------------------------------- status

    def status(self) -> dict[str, Any]:
        with self._lock:
            progress = self._progress
            return {
                "state": self._state.value,
                "active": self._state in (JobState.RUNNING, JobState.STOPPING),
                "progress": {
                    "phase": progress.phase,
                    "done": progress.done,
                    "total": progress.total,
                    "message": progress.message,
                },
                "request": self._request,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "error": self._error,
                "summary": self._summary,
            }

    # ------------------------------------------------------------------- worker

    def _on_progress(self, progress: Progress) -> None:
        with self._lock:
            self._progress = progress

    def _run(self, first: date, last: date, timeframes: list[int]) -> None:
        state = JobState.DONE
        error: str | None = None
        summary: dict[str, Any] | None = None
        try:
            conn = connect(self._url)
            try:
                backfill = self._make_backfill(
                    self._source,
                    Store(conn),
                    progress=self._on_progress,
                    should_stop=self._stop.is_set,
                )
                params = [StrategyParams(timeframe=tf) for tf in timeframes]
                report = backfill.run(first, last, params)
            finally:
                conn.close()
            summary = _summarise(report)
            if report.stopped:
                state = JobState.STOPPED
        except TradingError as exc:
            state, error = JobState.FAILED, exc.message
        except Exception:
            logger.exception("download failed unexpectedly")
            state = JobState.FAILED
            error = "The download hit an unexpected problem. See the server log for details."
        with self._lock:
            self._state = state
            self._error = error
            self._summary = summary
            self._finished_at = datetime.now(IST).isoformat(timespec="seconds")
