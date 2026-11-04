"""Download NIFTY spot, expiries and the option candles a backtest needs, resumably.

Order of work (each step only fetches what the ``fetch_log`` does not already show as done):

1. **Expiries** for every month in the range (plus the next month, because the "nearest expiry on
   or after the last trading day" may fall there).
2. **Spot** 1-minute candles in <=28-day requests (Groww allows 30), plus a warm-up before the
   start so the EMAs are already converged on the first traded day.
3. **Plan**: run the engine's signal detection on the stored spot to learn exactly which option
   contracts it would trade on which days (``required_contracts``). Nothing is downloaded "just in
   case": a 12-month run needs a few thousand small requests, not millions.
4. **Options** one request per (contract, day).

Safety properties:

* Read-only: only market-data endpoints are called.
* A gentle request rate (``Throttle``) with exponential backoff on 429s and timeouts.
* Failures of one item are logged (``status='error'``) and retried on the next run; fatal problems
  (not logged in, forbidden, ambiguous timestamp convention) stop the run immediately.
* Too many consecutive failures stop the run instead of hammering the API.
* Today's (unfinished) session is never stored as final.
"""

import logging
import time as time_module
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from functools import partial
from typing import Any, Protocol

from app.errors import (
    RateLimitedError,
    TradingError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.history.parse import parse_candles
from app.history.quality import (
    Convention,
    assess,
    detect_convention,
    regular_session_only,
    to_start_convention,
)
from app.history.store import FetchKind, FetchStatus, Store
from app.strategy.bars import SESSION_CLOSE, SESSION_OPEN
from app.strategy.engine import StrategyParams, analyse_spot, required_contracts
from app.strategy.models import Candle, Mode
from app.strategy.options import expiry_from_symbol

logger = logging.getLogger(__name__)

EXCHANGE = "NSE"
UNDERLYING = "NIFTY"
SPOT_SYMBOL = "NSE-NIFTY"
MAX_CHUNK_DAYS = 28  # Groww's 1-minute limit is 30 days per request
WARMUP_DAYS = 14  # calendar days of spot before the start (EMA warm-up across weekends/holidays)
CONVENTION_KEY = "candle_timestamp_convention"
_RETRY_DELAYS = (2.0, 5.0, 15.0)  # seconds before retries 1, 2 and 3


class MarketData(Protocol):
    """The slice of :class:`~app.groww_service.GrowwService` the downloader uses."""

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]: ...

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]: ...

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]: ...


class BackfillStopped(Exception):  # noqa: N818 - a control-flow signal, not an error
    """The caller asked the run to stop."""


class BackfillAbortedError(TradingError):
    """The run cannot continue safely (details in the message)."""

    code = "backfill_aborted"
    http_status = 500


@dataclass(frozen=True, slots=True)
class Progress:
    phase: str
    done: int
    total: int
    message: str = ""


@dataclass(slots=True)
class PhaseReport:
    fetched: int = 0  # requests that returned data
    empty: int = 0  # requests that returned nothing (weekend, holiday, no trades)
    cached: int = 0  # items skipped because they were already downloaded
    errors: int = 0


@dataclass(slots=True)
class BackfillReport:
    phases: dict[str, PhaseReport] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)  # data-quality notes worth showing
    stopped: bool = False

    def phase(self, name: str) -> PhaseReport:
        return self.phases.setdefault(name, PhaseReport())


class Throttle:
    """Keeps at least *interval* seconds between requests."""

    def __init__(
        self,
        interval: float = 0.25,
        clock: Callable[[], float] = time_module.monotonic,
        sleep: Callable[[float], None] = time_module.sleep,
    ) -> None:
        self._interval = interval
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            remaining = self._interval - (now - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def month_starts(start: date, end: date) -> list[date]:
    """First day of every month touched by start..end."""
    months: list[date] = []
    current = date(start.year, start.month, 1)
    while current <= end:
        months.append(current)
        current = date(current.year + (current.month == 12), current.month % 12 + 1, 1)
    return months


def chunk_days(days: Sequence[date], max_span: int = MAX_CHUNK_DAYS) -> list[tuple[date, date]]:
    """Group sorted days into (first, last) windows no wider than *max_span* calendar days.

    Days that are far apart become separate windows, so a resume never re-downloads a long
    stretch that is already stored just because one day inside it is missing.
    """
    windows: list[tuple[date, date]] = []
    for day in sorted(days):
        if windows and (day - windows[-1][0]).days < max_span and (day - windows[-1][1]).days <= 1:
            windows[-1] = (windows[-1][0], day)
        else:
            windows.append((day, day))
    return windows


class Backfill:
    def __init__(
        self,
        source: MarketData,
        store: Store,
        *,
        throttle: Throttle | None = None,
        sleep: Callable[[float], None] = time_module.sleep,
        today: Callable[[], date] = date.today,
        progress: Callable[[Progress], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        max_consecutive_errors: int = 15,
    ) -> None:
        self._source = source
        self._store = store
        self._throttle = throttle or Throttle()
        self._sleep = sleep
        self._today = today
        self._progress = progress or (lambda _: None)
        self._should_stop = should_stop or (lambda: False)
        self._max_errors = max_consecutive_errors
        self._consecutive_errors = 0
        self._convention: Convention | None = None
        self.report = BackfillReport()

    # ----------------------------------------------------------------- entry point

    def run(
        self,
        start: date,
        end: date,
        params_list: Sequence[StrategyParams],
        modes: Sequence[Mode] = tuple(Mode),
    ) -> BackfillReport:
        """Download everything needed to backtest start..end for every params set."""
        end = min(end, self._today() - timedelta(days=1))  # today's session is not finished
        if start > end:
            msg = "nothing to download: the range ends before it starts (or is only today)"
            raise BackfillAbortedError(msg)
        try:
            self.sync_expiries(start, end)
            self.sync_spot(start - timedelta(days=WARMUP_DAYS), end)
            required = self.plan_options(start, end, params_list, modes)
            self.sync_contracts(required)
            self.sync_options(required)
        except BackfillStopped:
            self.report.stopped = True
        return self.report

    # ------------------------------------------------------------------- expiries

    def sync_expiries(self, start: date, end: date) -> None:
        today = self._today()
        months = month_starts(start, end + timedelta(days=31))  # include the following month
        status = self._store.fetch_status("expiries", UNDERLYING, months[0], months[-1])
        report = self.report.phase("expiries")
        for index, month in enumerate(months):
            self._check_stop()
            self._progress(Progress("expiries", index, len(months), month.strftime("%b %Y")))
            # Months that are over never change; the current and future ones can gain weeklies.
            if status.get(month) in ("ok", "empty") and month < today.replace(day=1):
                report.cached += 1
                continue
            self._guarded(
                report,
                "expiries",
                UNDERLYING,
                month,
                partial(self._fetch_expiries, month, report),
            )

    def _fetch_expiries(self, month: date, report: PhaseReport) -> None:
        raw = self._retry(
            lambda: self._source.get_expiries(
                exchange=EXCHANGE, underlying=UNDERLYING, year=month.year, month=month.month
            )
        )
        try:
            dates = [date.fromisoformat(item) for item in raw]
        except ValueError as exc:
            msg = "Groww sent an expiry date this app could not read."
            raise UpstreamError(msg) from exc
        self._store.save_expiries(UNDERLYING, dates)
        status: FetchStatus = "ok" if dates else "empty"
        self._store.log_fetch("expiries", UNDERLYING, month, status, rows=len(dates))
        if dates:
            report.fetched += 1
        else:
            report.empty += 1

    # ----------------------------------------------------------------------- spot

    def sync_spot(self, start: date, end: date) -> None:
        report = self.report.phase("spot")
        all_days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
        done = self._store.fetch_status("spot", SPOT_SYMBOL, start, end)
        missing = [d for d in all_days if done.get(d) not in ("ok", "empty")]
        report.cached += len(all_days) - len(missing)
        windows = chunk_days(missing)
        for index, (first, last) in enumerate(windows):
            self._check_stop()
            self._progress(Progress("spot", index, len(windows), f"{first} to {last}"))
            self._guarded(
                report,
                "spot",
                SPOT_SYMBOL,
                first,
                partial(self._fetch_spot, first, last, report),
            )

    def _fetch_spot(self, first: date, last: date, report: PhaseReport) -> None:
        candles = self._download("CASH", SPOT_SYMBOL, first, last, spot=True)
        by_day = {q.day: q for q in assess(candles)}
        self._store.save_spot(SPOT_SYMBOL, candles)
        for offset in range((last - first).days + 1):
            day = first + timedelta(days=offset)
            quality = by_day.get(day)
            if quality is None:
                self._store.log_fetch("spot", SPOT_SYMBOL, day, "empty")
                report.empty += 1
                continue
            note = quality.describe()
            self._store.log_fetch("spot", SPOT_SYMBOL, day, "ok", rows=quality.present, detail=note)
            report.fetched += 1
            if not quality.clean:
                self.report.problems.append(f"spot {day}: {note}")

    # ------------------------------------------------------------------- planning

    def plan_options(
        self,
        start: date,
        end: date,
        params_list: Sequence[StrategyParams],
        modes: Sequence[Mode] = tuple(Mode),
    ) -> dict[date, set[str]]:
        """The (day -> option symbols) the engine would trade, from the stored spot + expiries."""
        spot = self._store.load_spot(SPOT_SYMBOL, start - timedelta(days=WARMUP_DAYS), end)
        expiries = self._store.load_expiries(UNDERLYING)
        needed: dict[date, set[str]] = {}
        for params in params_list:
            analysis = analyse_spot(spot, params)
            for day, symbols in required_contracts(
                analysis, expiries, params, modes, start, end
            ).items():
                needed.setdefault(day, set()).update(symbols)
        return needed

    # ------------------------------------------------------------------ contracts

    def sync_contracts(self, required: Mapping[date, set[str]]) -> None:
        """Fetch the listed contracts of every expiry we will trade, to verify our symbols.

        If Groww lists contracts for an expiry and *none* of the symbols we built appear in the
        list, our symbol format is wrong and every option download would be wasted: abort.
        """
        expiries = sorted(
            {e for symbols in required.values() for e in map(expiry_from_symbol, symbols)}
        )
        report = self.report.phase("contracts")
        done = (
            self._store.fetch_status("contracts", UNDERLYING, expiries[0], expiries[-1])
            if expiries
            else {}
        )
        for index, expiry in enumerate(expiries):
            self._check_stop()
            self._progress(Progress("contracts", index, len(expiries), str(expiry)))
            if done.get(expiry) in ("ok", "empty") and expiry < self._today():
                report.cached += 1
            else:
                self._guarded(
                    report,
                    "contracts",
                    UNDERLYING,
                    expiry,
                    partial(self._fetch_contracts, expiry, report),
                )
            listed = self._store.load_contracts(UNDERLYING, expiry)
            ours = {
                s for syms in required.values() for s in syms if expiry_from_symbol(s) == expiry
            }
            if listed and ours and not (ours & listed):
                msg = (
                    f"None of the option symbols this app builds for the {expiry} expiry "
                    f"(for example {sorted(ours)[0]}) appear in Groww's contract list "
                    f"(for example {sorted(listed)[0]}). The symbol format needs fixing before "
                    "any option data is downloaded."
                )
                raise BackfillAbortedError(msg)

    def _fetch_contracts(self, expiry: date, report: PhaseReport) -> None:
        symbols = self._retry(
            lambda: self._source.get_contracts(
                exchange=EXCHANGE, underlying=UNDERLYING, expiry=expiry
            )
        )
        self._store.save_contracts(UNDERLYING, expiry, symbols)
        self._store.log_fetch(
            "contracts", UNDERLYING, expiry, "ok" if symbols else "empty", rows=len(symbols)
        )
        if symbols:
            report.fetched += 1
        else:
            report.empty += 1

    # -------------------------------------------------------------------- options

    def sync_options(self, required: Mapping[date, set[str]]) -> None:
        report = self.report.phase("options")
        pairs = sorted((day, symbol) for day, symbols in required.items() for symbol in symbols)
        listed: dict[date, set[str]] = {}
        for index, (day, symbol) in enumerate(pairs):
            self._check_stop()
            self._progress(Progress("options", index, len(pairs), f"{symbol} on {day}"))
            if self._store.fetch_status("option", symbol, day, day).get(day) in ("ok", "empty"):
                report.cached += 1
                continue
            expiry = expiry_from_symbol(symbol)
            if expiry not in listed:
                listed[expiry] = self._store.load_contracts(UNDERLYING, expiry)
            if listed[expiry] and symbol not in listed[expiry]:
                self._store.log_fetch(
                    "option", symbol, day, "empty", detail="not in Groww's contract list"
                )
                report.empty += 1
                continue
            self._guarded(
                report,
                "option",
                symbol,
                day,
                partial(self._fetch_option, symbol, day, report),
            )

    def _fetch_option(self, symbol: str, day: date, report: PhaseReport) -> None:
        candles = self._download("FNO", symbol, day, day, spot=False)
        self._store.save_option(symbol, candles)
        self._store.log_fetch(
            "option", symbol, day, "ok" if candles else "empty", rows=len(candles)
        )
        if candles:
            report.fetched += 1
        else:
            report.empty += 1

    # ------------------------------------------------------------------ plumbing

    def _download(
        self, segment: str, symbol: str, first: date, last: date, *, spot: bool
    ) -> list[Candle]:
        start = datetime.combine(first, SESSION_OPEN)
        end = datetime.combine(last, SESSION_CLOSE)
        rows = self._retry(
            lambda: self._source.get_candles(
                exchange=EXCHANGE, segment=segment, symbol=symbol, start=start, end=end
            )
        )
        parsed = parse_candles(rows)
        candles = parsed.candles
        if spot:
            self._learn_convention(candles)
        if candles:
            convention = self._require_convention()
            candles = regular_session_only(to_start_convention(candles, convention))
        return candles

    def _learn_convention(self, candles: Sequence[Candle]) -> None:
        """Detect (and remember) how Groww stamps minutes, from index candles."""
        if not candles:
            return
        stored = self._store.get_meta(CONVENTION_KEY)
        seen = detect_convention(candles)
        if self._convention is None and stored:
            self._convention = Convention(stored)
        if seen is Convention.UNKNOWN:
            if self._convention is None:
                msg = (
                    "Cannot tell whether Groww stamps candles with the start or the end of the "
                    "minute (the first/last timestamps of a day are neither 09:15/15:29 nor "
                    "09:16/15:30). Refusing to guess: a one-minute error would corrupt "
                    "every signal."
                )
                raise BackfillAbortedError(msg)
            return
        if self._convention is not None and seen is not self._convention:
            msg = (
                f"Groww's timestamp convention changed from {self._convention} to {seen} "
                "between downloads. Clear the downloaded data before continuing."
            )
            raise BackfillAbortedError(msg)
        self._convention = seen
        if stored != seen.value:
            self._store.set_meta(CONVENTION_KEY, seen.value)

    def _require_convention(self) -> Convention:
        if self._convention is None:
            stored = self._store.get_meta(CONVENTION_KEY)
            if not stored:
                msg = "Download the NIFTY spot candles first: the timestamp convention is unknown."
                raise BackfillAbortedError(msg)
            self._convention = Convention(stored)
        return self._convention

    def _retry[T](self, request: Callable[[], T]) -> T:
        """Throttle, call, and retry rate limits / timeouts with growing pauses."""
        attempt = 0
        while True:
            self._check_stop()
            self._throttle.wait()
            try:
                return request()
            except (RateLimitedError, UpstreamTimeoutError) as exc:
                if attempt >= len(_RETRY_DELAYS):
                    raise
                delay = _RETRY_DELAYS[attempt]
                logger.warning("%s; retrying in %.0fs", exc.message, delay)
                self._sleep(delay)
                attempt += 1

    def _guarded(
        self,
        report: PhaseReport,
        kind: FetchKind,
        key: str,
        day: date,
        action: Callable[[], None],
    ) -> None:
        """Run one download item; log its failure and carry on, unless it is fatal."""
        try:
            action()
        except (BackfillAbortedError, BackfillStopped):
            raise
        except (UpstreamError, RateLimitedError, UpstreamTimeoutError, ValueError) as exc:
            detail = exc.message if isinstance(exc, TradingError) else str(exc)
            logger.warning("%s %s %s failed: %s", kind, key, day, detail)
            self._store.log_fetch(kind, key, day, "error", detail=detail[:300])
            report.errors += 1
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._max_errors:
                msg = (
                    f"Stopped after {self._consecutive_errors} failures in a row "
                    f"(last: {detail}). Fix the cause and run again: finished work is kept."
                )
                raise BackfillAbortedError(msg) from exc
        else:
            self._consecutive_errors = 0

    def _check_stop(self) -> None:
        if self._should_stop():
            raise BackfillStopped
