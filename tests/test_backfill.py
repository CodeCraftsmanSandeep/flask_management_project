"""The downloader end to end: fake Groww -> Backfill -> real (temporary) Postgres -> engine."""

from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.db.connection import connect
from app.errors import (
    ForbiddenError,
    NotConnectedError,
    RateLimitedError,
    UpstreamError,
    UpstreamTimeoutError,
)
from app.history.backfill import (
    CONVENTION_KEY,
    SPOT_SYMBOL,
    WARMUP_DAYS,
    Backfill,
    BackfillAbortedError,
    Progress,
    Throttle,
    chunk_days,
    month_starts,
)
from app.history.store import Store
from app.strategy.engine import run_backtest
from app.strategy.models import Mode
from tests.builders import session_minutes, spot_day
from tests.test_engine import (
    DAY,
    EXPIRIES,
    GOLDEN_CLOSES,
    GOLDEN_PRICES,
    PARAMS,
    S,
    ref_leg_total,
)

D = Decimal
TODAY = date(2026, 5, 20)
GOLDEN_SYMBOLS = set(GOLDEN_PRICES)


def iso(moment: datetime) -> str:
    return moment.isoformat()


def row(moment: datetime, price: Decimal | float, *, volume: int = 1) -> list[Any]:
    """A raw Groww row: JSON numbers (floats), never Decimals."""
    p = float(price)
    return [iso(moment), p, p, p, p, volume, None]


class FakeSource:
    """In-memory Groww. Rows are filtered by the requested window like the real API."""

    def __init__(self, *, shift_minutes: int = 0) -> None:
        self.spot_rows: list[list[Any]] = [
            row(c.start + timedelta(minutes=shift_minutes), c.close)
            for c in spot_day(DAY, GOLDEN_CLOSES)
        ]
        self.option_rows: dict[str, list[list[Any]]] = {
            symbol: [
                [iso(datetime.fromisoformat(f"{DAY}T{hhmm}:00")), p, p, p, p, 100, 5000]
                for hhmm, p in minutes.items()
            ]
            for symbol, minutes in GOLDEN_PRICES.items()
        }
        self.expiries: list[date] = [*EXPIRIES, date(2026, 5, 26), date(2026, 6, 2)]
        self.contracts: dict[date, list[str]] = {
            DAY: [*sorted(GOLDEN_SYMBOLS), f"{S}-99999-CE"],
            date(2026, 5, 19): [],
        }
        self.calls: list[tuple[str, ...]] = []
        self.fail: dict[str, list[Exception]] = {}  # method -> exceptions to raise, in order
        self.fail_forever: dict[str, Exception] = {}
        self.on_call: Callable[[], None] | None = None

    def _enter(self, name: str, *detail: object) -> None:
        self.calls.append((name, *map(str, detail)))
        if self.on_call:
            self.on_call()
        if name in self.fail_forever:
            raise self.fail_forever[name]
        if self.fail.get(name):
            raise self.fail[name].pop(0)

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        self._enter("candles", segment, symbol, start, end)
        assert exchange == "NSE"
        rows = self.spot_rows if segment == "CASH" else self.option_rows.get(symbol, [])
        return [r for r in rows if start <= datetime.fromisoformat(r[0]) <= end]

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]:
        self._enter("expiries", year, month)
        return [e.isoformat() for e in self.expiries if e.year == year and e.month == month]

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]:
        self._enter("contracts", expiry)
        return list(self.contracts.get(expiry, []))

    def count(self, name: str, *detail: str) -> int:
        return sum(1 for c in self.calls if c[0] == name and c[1 : 1 + len(detail)] == detail)


@pytest.fixture
def store(db_url: str) -> Iterator[Store]:
    conn = connect(db_url)
    try:
        yield Store(conn)
    finally:
        conn.close()


def make(
    source: FakeSource,
    store: Store,
    *,
    sleeps: list[float] | None = None,
    **options: Any,
) -> Backfill:
    log = sleeps if sleeps is not None else []
    return Backfill(
        source,
        store,
        throttle=Throttle(interval=0),
        sleep=log.append,
        today=lambda: TODAY,
        **options,
    )


def run(backfill: Backfill) -> None:
    backfill.run(DAY, DAY, [PARAMS])


# ----------------------------------------------------------------- the whole pipeline


def test_downloaded_data_reproduces_the_hand_checked_golden_trades(store: Store) -> None:
    source = FakeSource()
    backfill = make(source, store)
    run(backfill)

    spot = store.load_spot(SPOT_SYMBOL, DAY - timedelta(days=WARMUP_DAYS), DAY)
    assert [c.start for c in spot] == session_minutes(DAY)
    required = backfill.plan_options(DAY, DAY, [PARAMS])
    assert required == {DAY: GOLDEN_SYMBOLS}

    result = run_backtest(
        spot,
        store.load_expiries("NIFTY"),
        store.load_option_book(required),
        PARAMS,
        start=DAY,
        end=DAY,
    )
    assert len(result.trades) == 9
    assert result.skips == []
    (t1,) = [
        t
        for t in result.trades
        if t.mode is Mode.ITM5_BUY and t.entry_decision == datetime(2026, 5, 12, 9, 45)
    ]
    assert (t1.option_entry_fill, t1.option_exit_fill) == (D("211.55"), D("188.55"))
    assert t1.gross_pnl == D(-1300)
    assert t1.slippage == D("195.00")
    assert t1.charges.total == D("76.91")
    assert t1.net_pnl == D("-1571.91")
    assert t1.charges.total == ref_leg_total("211.55", sell=False) + ref_leg_total(
        "188.55", sell=True
    )


def test_each_phase_reports_what_it_did(store: Store) -> None:
    source = FakeSource()
    backfill = make(source, store)
    run(backfill)
    phases = backfill.report.phases
    assert phases["spot"].fetched == 1  # May 12 had candles
    assert phases["spot"].empty == WARMUP_DAYS  # the 14 warm-up days before it: nothing in the fake
    assert phases["options"].fetched == 9
    assert phases["options"].errors == 0
    assert phases["contracts"].fetched == 1  # only May 12 has a listed chain; others unlisted
    assert backfill.report.problems == []
    assert not backfill.report.stopped


def test_expiries_are_stored_for_the_range_and_the_following_month(store: Store) -> None:
    run(make(FakeSource(), store))
    assert store.load_expiries("NIFTY") == [
        date(2026, 5, 12),
        date(2026, 5, 19),
        date(2026, 5, 26),
        date(2026, 6, 2),
    ]


def test_the_fetch_log_marks_data_days_ok_and_the_rest_empty(store: Store) -> None:
    run(make(FakeSource(), store))
    status = store.fetch_status("spot", SPOT_SYMBOL, DAY - timedelta(days=WARMUP_DAYS), DAY)
    assert status[DAY] == "ok"
    assert {s for d, s in status.items() if d != DAY} == {"empty"}
    assert len(status) == WARMUP_DAYS + 1


# --------------------------------------------------------------------------- resuming


def test_a_second_run_downloads_no_candles_again(store: Store) -> None:
    source = FakeSource()
    run(make(source, store))
    first = source.count("candles")
    assert first == 1 + 9  # one spot window + nine option days
    run(make(source, store))
    assert source.count("candles") == first
    assert source.count("contracts") == 1  # a past expiry's chain is cached, too


def test_only_the_current_month_is_asked_for_expiries_again(store: Store) -> None:
    source = FakeSource()
    run(make(source, store))
    before = source.count("expiries")
    run(make(source, store))
    # months before today's are final; May and June (current and future) can gain weeklies
    assert source.count("expiries") - before == 2


def test_failed_items_are_retried_on_the_next_run(store: Store) -> None:
    source = FakeSource()
    source.fail_forever["candles"] = UpstreamError("HTTP 500")
    backfill = make(source, store, max_consecutive_errors=100)
    run(backfill)  # spot failed, so there is nothing to plan options from
    assert backfill.report.phases["spot"].errors == 1
    assert store.count_candles("option_candles") == 0
    assert store.failures()[0][:2] == ("spot", SPOT_SYMBOL)

    del source.fail_forever["candles"]
    run(make(source, store))
    assert store.fetch_status("spot", SPOT_SYMBOL, DAY, DAY) == {DAY: "ok"}
    assert store.count_candles("option_candles") > 0


def test_progress_is_reported_for_every_phase(store: Store) -> None:
    seen: list[Progress] = []
    run(make(FakeSource(), store, progress=seen.append))
    assert {p.phase for p in seen} == {"expiries", "spot", "contracts", "options"}
    options = [p for p in seen if p.phase == "options"]
    assert [p.done for p in options] == list(range(9))
    assert all(p.total == 9 for p in options)


def test_a_run_can_be_stopped_and_resumed(store: Store) -> None:
    source = FakeSource()
    stop = {"now": False}
    source.on_call = lambda: stop.__setitem__("now", source.count("candles") >= 4)
    backfill = make(source, store, should_stop=lambda: stop["now"])
    run(backfill)
    assert backfill.report.stopped
    partial = store.count_candles("option_candles")
    assert 0 < partial < 9 * 2  # some options were saved, not all

    stop["now"] = False
    source.on_call = None
    run(make(source, store))
    assert store.fetch_summary()
    assert len(store.load_option_book({DAY: GOLDEN_SYMBOLS}).data) == 9


# ------------------------------------------------------------- timestamp convention


def test_close_stamped_candles_are_normalised_to_minute_starts(store: Store) -> None:
    source = FakeSource(shift_minutes=1)
    # option rows are stamped the same way as spot (end of minute)
    for rows in source.option_rows.values():
        for row in rows:
            row[0] = iso(datetime.fromisoformat(row[0]) + timedelta(minutes=1))
    backfill = make(source, store)
    run(backfill)
    assert store.get_meta(CONVENTION_KEY) == "close"
    spot = store.load_spot(SPOT_SYMBOL, DAY, DAY)
    assert [c.start for c in spot] == session_minutes(DAY)
    book = store.load_option_book({DAY: GOLDEN_SYMBOLS})
    entry = book.candles(f"{S}-23800-PE", DAY)[datetime(2026, 5, 12, 9, 45)]
    assert entry.open == D(150)  # the 09:45 candle, not the 09:44 one

    result = run_backtest(spot, store.load_expiries("NIFTY"), book, PARAMS, start=DAY, end=DAY)
    assert len(result.trades) == 9
    assert result.skips == []


def test_an_unrecognisable_convention_stops_everything_and_saves_nothing(store: Store) -> None:
    source = FakeSource()
    source.spot_rows = [r for r in source.spot_rows if "09:20" <= r[0][11:16] <= "14:00"]
    with pytest.raises(BackfillAbortedError, match="Refusing to guess"):
        run(make(source, store))
    assert store.count_candles("spot_candles") == 0
    assert store.count_candles("option_candles") == 0
    assert source.count("candles", "FNO") == 0


def test_a_changed_convention_between_downloads_is_refused(store: Store) -> None:
    store.set_meta(CONVENTION_KEY, "close")
    with pytest.raises(BackfillAbortedError, match="changed from close to start"):
        run(make(FakeSource(), store))
    assert store.count_candles("spot_candles") == 0


def test_a_remembered_convention_covers_partial_data(store: Store) -> None:
    """A spot window with no recognisable edges is fine once the convention is known."""
    source = FakeSource()
    run(make(source, store))  # learns "start"
    assert store.get_meta(CONVENTION_KEY) == "start"
    other = date(2026, 5, 13)
    source.spot_rows = [r for r in spot_rows_for(other) if "10:00" <= r[0][11:16] <= "10:30"]
    backfill = make(source, store)
    backfill.sync_spot(other, other)
    assert backfill.report.phases["spot"].fetched == 1
    assert backfill.report.problems  # ...but the day is flagged incomplete


def spot_rows_for(day: date) -> list[list[Any]]:
    return [row(c.start, c.close) for c in spot_day(day, GOLDEN_CLOSES)]


# ----------------------------------------------------------------- data quality notes


def test_an_incomplete_spot_day_is_stored_but_flagged(store: Store) -> None:
    source = FakeSource()
    source.spot_rows = [r for r in source.spot_rows if r[0][11:16] != "11:00"]
    backfill = make(source, store)
    run(backfill)
    assert any("374/375 minutes; missing 11:00" in p for p in backfill.report.problems)
    assert store.fetch_status("spot", SPOT_SYMBOL, DAY, DAY) == {DAY: "ok"}
    row = store._conn.execute(
        "SELECT rows, detail FROM fetch_log WHERE kind = 'spot' AND day = %s", (DAY,)
    ).fetchone()
    assert row == (374, "374/375 minutes; missing 11:00")
    # the engine then treats the day as unusable and says so, instead of trading on it
    result = run_backtest(
        store.load_spot(SPOT_SYMBOL, DAY, DAY),
        store.load_expiries("NIFTY"),
        store.load_option_book({}),
        PARAMS,
        start=DAY,
        end=DAY,
    )
    assert [(d.kind, d.detail) for d in result.days] == [("incomplete_spot", "374/375 minutes")]
    assert result.trades == []


def test_out_of_session_rows_are_not_stored(store: Store) -> None:
    source = FakeSource()
    source.spot_rows.append(row(datetime(2026, 5, 12, 15, 30), 1))
    source.spot_rows.append(row(datetime(2026, 5, 12, 9, 14), 1))
    run(make(source, store))
    assert len(store.load_spot(SPOT_SYMBOL, DAY, DAY)) == 375


# ---------------------------------------------------------------- contract validation


def test_a_wrong_symbol_format_aborts_before_any_option_download(store: Store) -> None:
    source = FakeSource()
    source.contracts[DAY] = ["NIFTY26MAY24000CE", "NIFTY26MAY24100CE"]  # a different convention
    with pytest.raises(BackfillAbortedError, match="symbol format"):
        run(make(source, store))
    assert source.count("candles", "FNO") == 0


def test_an_unlisted_chain_does_not_block_downloads(store: Store) -> None:
    source = FakeSource()
    source.contracts[DAY] = []  # Groww no longer lists this (expired) chain
    backfill = make(source, store)
    run(backfill)
    assert backfill.report.phases["options"].fetched == 9


def test_a_symbol_missing_from_a_listed_chain_is_not_requested(store: Store) -> None:
    source = FakeSource()
    source.contracts[DAY] = sorted(GOLDEN_SYMBOLS - {f"{S}-24000-PE"})
    backfill = make(source, store)
    run(backfill)
    assert backfill.report.phases["options"].fetched == 8
    assert backfill.report.phases["options"].empty == 1
    assert source.count("candles", "FNO", f"{S}-24000-PE") == 0
    assert store.fetch_status("option", f"{S}-24000-PE", DAY, DAY) == {DAY: "empty"}


# ----------------------------------------------------------- failures and retries


def test_rate_limits_are_retried_with_growing_pauses(store: Store) -> None:
    source = FakeSource()
    source.fail["expiries"] = [RateLimitedError("slow down"), UpstreamTimeoutError("late")]
    sleeps: list[float] = []
    run(make(source, store, sleeps=sleeps))
    assert sleeps[:2] == [2.0, 5.0]
    assert store.load_expiries("NIFTY")


def test_a_request_rate_limited_three_times_succeeds_on_the_fourth_attempt(store: Store) -> None:
    source = FakeSource()
    source.fail["candles"] = [RateLimitedError("x")] * 3  # spot window survives 4 attempts
    sleeps: list[float] = []
    backfill = make(source, store, sleeps=sleeps)
    run(backfill)
    assert sleeps[:3] == [2.0, 5.0, 15.0]
    assert backfill.report.phases["spot"].fetched == 1  # the 4th attempt worked


def test_persistent_rate_limiting_is_recorded_as_an_item_error(store: Store) -> None:
    source = FakeSource()
    run(make(source, store))  # everything downloaded once
    option = f"{S}-23800-PE"
    with store._conn.cursor() as cur:  # forget one option day, as if it had failed before
        cur.execute("DELETE FROM fetch_log WHERE kind = 'option' AND key = %s", (option,))
    store._conn.commit()
    source.fail["candles"] = [RateLimitedError("busy")] * 4
    backfill = make(source, store)
    run(backfill)
    assert backfill.report.phases["options"].errors == 1
    assert store.fetch_status("option", option, DAY, DAY) == {DAY: "error"}
    run(make(source, store))  # next run repairs it
    assert store.fetch_status("option", option, DAY, DAY) == {DAY: "ok"}


def test_too_many_consecutive_failures_abort_the_run(store: Store) -> None:
    source = FakeSource()
    source.fail_forever["expiries"] = UpstreamError("HTTP 500")
    backfill = make(source, store, max_consecutive_errors=2)  # May and June both fail
    with pytest.raises(BackfillAbortedError, match="2 failures in a row"):
        run(backfill)
    assert backfill.report.phases["expiries"].errors == 2
    assert source.count("candles") == 0  # it never got as far as downloading candles


def test_a_success_resets_the_failure_streak(store: Store) -> None:
    source = FakeSource()
    source.fail["expiries"] = [UpstreamError("a")]  # May fails, June succeeds ...
    source.fail["candles"] = [UpstreamError("b")]  # ... then the spot window fails
    backfill = make(source, store, max_consecutive_errors=2)
    run(backfill)  # two failures in total, but never two in a row: no abort
    assert backfill.report.phases["expiries"].errors == 1
    assert backfill.report.phases["spot"].errors == 1


@pytest.mark.parametrize("error", [NotConnectedError("log in"), ForbiddenError("no subscription")])
def test_fatal_errors_stop_the_run_immediately_and_are_not_logged_as_item_failures(
    store: Store, error: Exception
) -> None:
    source = FakeSource()
    source.fail_forever["expiries"] = error
    with pytest.raises(type(error)):
        run(make(source, store))
    assert store.failures() == []


def test_garbled_expiry_dates_are_an_item_error(store: Store) -> None:
    source = FakeSource()
    source.get_expiries = lambda **_: ["not-a-date"]  # type: ignore[method-assign]
    backfill = make(source, store, max_consecutive_errors=100)
    run(backfill)
    assert backfill.report.phases["expiries"].errors > 0


# ------------------------------------------------------------------- date handling


def test_todays_unfinished_session_is_never_requested(store: Store) -> None:
    source = FakeSource()
    backfill = make(source, store)
    backfill.run(DAY, TODAY, [PARAMS])  # end is clipped to yesterday
    spot_calls = [c for c in source.calls if c[:2] == ("candles", "CASH")]
    assert spot_calls
    assert all(datetime.fromisoformat(c[4]).date() < TODAY for c in spot_calls)


def test_a_range_that_is_only_today_is_refused(store: Store) -> None:
    with pytest.raises(BackfillAbortedError, match="nothing to download"):
        make(FakeSource(), store).run(TODAY, TODAY, [PARAMS])


def test_the_request_window_spans_the_regular_session(store: Store) -> None:
    source = FakeSource()
    run(make(source, store))
    option_call = next(c for c in source.calls if c[:2] == ("candles", "FNO"))
    assert option_call[3].endswith("09:15:00")
    assert option_call[4].endswith("15:30:00")


# ------------------------------------------------------------------- pure helpers


def test_month_starts() -> None:
    assert month_starts(date(2025, 11, 20), date(2026, 2, 3)) == [
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]
    assert month_starts(date(2026, 5, 31), date(2026, 5, 31)) == [date(2026, 5, 1)]
    assert month_starts(date(2026, 6, 1), date(2026, 5, 1)) == []


def days(*values: str) -> list[date]:
    return [date.fromisoformat(v) for v in values]


def test_chunk_days_groups_consecutive_days_up_to_the_span() -> None:
    assert chunk_days([]) == []
    assert chunk_days(days("2026-05-01")) == [(date(2026, 5, 1), date(2026, 5, 1))]
    run_of_ten = [date(2026, 5, 1) + timedelta(days=i) for i in range(10)]
    assert chunk_days(run_of_ten) == [(date(2026, 5, 1), date(2026, 5, 10))]


def test_chunk_days_splits_long_runs_and_gaps() -> None:
    long_run = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
    windows = chunk_days(long_run, max_span=28)
    assert windows[0] == (date(2026, 1, 1), date(2026, 1, 28))
    assert windows[1] == (date(2026, 1, 29), date(2026, 2, 25))
    assert windows[-1][1] == long_run[-1]
    assert all((b - a).days < 28 for a, b in windows)
    covered = [a + timedelta(days=i) for a, b in windows for i in range((b - a).days + 1)]
    assert covered == long_run  # nothing lost, nothing duplicated
    # a stored stretch in the middle is not re-downloaded
    assert chunk_days(days("2026-05-01", "2026-05-02", "2026-05-20")) == [
        (date(2026, 5, 1), date(2026, 5, 2)),
        (date(2026, 5, 20), date(2026, 5, 20)),
    ]


def test_throttle_spaces_requests() -> None:
    now = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now["t"] += seconds

    throttle = Throttle(0.25, clock=lambda: now["t"], sleep=sleep)
    throttle.wait()  # the first call never waits
    throttle.wait()
    now["t"] += 1.0  # plenty of time passes on its own
    throttle.wait()
    assert slept == [0.25]


def test_a_forbidden_market_data_plan_stops_at_the_first_request(store: Store) -> None:
    source = FakeSource()
    source.fail_forever["expiries"] = ForbiddenError("no market data on this plan")
    backfill = make(source, store)
    with pytest.raises(ForbiddenError):
        run(backfill)
    assert source.count("expiries") == 1  # one request, not fifteen
    assert store.failures() == []
