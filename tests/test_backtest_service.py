"""The service on top of a real (temporary) Postgres, seeded through the real downloader."""

from dataclasses import replace
from datetime import date, time
from functools import partial

import pytest

from app.backtest.query import Filters as AppFilters
from app.backtest.runs import cache_key, default_params, trade_start
from app.backtest.service import BacktestService
from app.db.connection import connect, session
from app.errors import NoDataError, ResultsChangedError
from app.history.store import Store
from app.strategy.costs import DEFAULT_SCHEDULE
from app.strategy.engine import StrategyParams
from app.strategy.metrics import compute_metrics
from app.strategy.models import Mode
from tests.test_backfill import FakeSource, make, run
from tests.test_engine import DAY, PARAMS, S, golden

# The fixtures below hold 5-minute signals, so these tests ask for 5 minutes explicitly.
# (The app's own default timeframe is 15 minutes; a test in test_backtest_query.py pins that.)
Filters = partial(AppFilters, timeframe=5)


def params_for(timeframe: int) -> StrategyParams:
    """The golden scenario is built for 1/3 EMAs; production uses 9/21."""
    return replace(PARAMS, timeframe=timeframe)


@pytest.fixture
def seeded(db_url: str) -> str:
    conn = connect(db_url)
    try:
        run(make(FakeSource(), Store(conn)))
    finally:
        conn.close()
    return db_url


@pytest.fixture
def service(seeded: str) -> BacktestService:
    return BacktestService(seeded, params_for=params_for)


# ------------------------------------------------------------------ no data


def test_without_data_the_pages_get_a_clear_empty_state(db_url: str) -> None:
    service = BacktestService(db_url)
    assert service.meta()["has_data"] is False
    assert service.meta()["data_range"] is None
    with pytest.raises(NoDataError) as caught:
        service.trades(Filters())
    assert "Data page" in caught.value.message
    assert caught.value.http_status == 404
    status = service.data_status()
    assert status["has_data"] is False
    assert status["spot_rows"] == 0
    assert status["source"] is None


# ------------------------------------------------------------------- trades


def test_the_service_reproduces_the_golden_run_end_to_end(service: BacktestService) -> None:
    result = service.trades(Filters())
    expected = golden().trades
    assert len(result["trades"]) == len(expected) == 9
    kpis = result["kpis"]
    assert kpis["trades"] == 9
    assert kpis["net_pnl"] == float(sum(t.net_pnl for t in expected))
    assert kpis["gross_pnl"] == float(sum(t.gross_pnl for t in expected))
    reference = compute_metrics(expected)
    assert kpis["win_rate"] == round(reference.win_rate or 0, 4)
    assert kpis["max_drawdown"] == float(reference.max_drawdown)
    assert result["timeframe"] == 5
    assert result["range"] == {"first": "2026-05-12", "last": "2026-05-12"}


def test_costs_add_up_in_every_row_of_the_run(service: BacktestService) -> None:
    for row in service.trades(Filters())["trades"]:
        detail = service.trade_detail(5, row["id"], None, Filters())
        costs = detail["trade"]["costs"]
        parts = sum(costs[k] for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst"))
        assert round(parts, 2) == costs["charges_total"]
        assert round(costs["gross"] - costs["slippage"] - costs["charges_total"], 2) == costs["net"]
        assert costs["net"] == row["net"]


def test_the_compare_table_has_every_mode_and_ignores_the_mode_filter(
    service: BacktestService,
) -> None:
    result = service.trades(Filters(mode=Mode.ATM_SELL))
    assert result["kpis"]["trades"] == 3
    assert {m: c["trades"] for m, c in result["compare"].items()} == {
        "itm5_buy": 3,
        "atm_buy": 3,
        "atm_sell": 3,
    }
    # the numbers per mode agree with the independent engine run
    for mode in Mode:
        reference = compute_metrics([t for t in golden().trades if t.mode is mode])
        assert result["compare"][mode.value]["net_pnl"] == float(reference.net_pnl)


@pytest.mark.parametrize(
    ("filters", "count"),
    [
        (Filters(start=time(10, 0)), 3),  # only the 12:40 signal
        (Filters(cutoff=time(11, 30)), 6),  # 09:45 and 09:55
        (Filters(start=time(10, 0), cutoff=time(12, 0)), 0),
        (Filters(cross=2), 3),  # the 09:55 reversal
        (Filters(start=time(10, 0), cross=1), 3),  # numbering restarts inside the window
        (Filters(start=time(10, 0), cross=2), 0),
        (Filters(mode=Mode.ITM5_BUY, cross=3), 1),
        (Filters(date_from=date(2026, 5, 13)), 0),
    ],
)
def test_filters_narrow_the_cached_run(
    service: BacktestService, filters: AppFilters, count: int
) -> None:
    assert len(service.trades(filters)["trades"]) == count


def test_outcome_filters_partition_the_trades(service: BacktestService) -> None:
    counts = {o: len(service.trades(Filters(outcome=o))["trades"]) for o in ("win", "loss", "tie")}
    assert sum(counts.values()) == 9
    assert counts["win"] == sum(1 for t in golden().trades if t.net_pnl > 0)
    assert counts["loss"] == sum(1 for t in golden().trades if t.net_pnl < 0)


def test_the_list_rows_carry_what_the_ui_needs(service: BacktestService) -> None:
    row = service.trades(Filters(mode=Mode.ATM_BUY))["trades"][0]
    assert row == {
        "id": row["id"],
        "date": "2026-05-12",
        "entry": "09:45",
        "exit": "09:55",
        "mode": "atm_buy",
        "direction": "bearish",
        "side": "BUY",
        "option": "23800 PE",
        "net": row["net"],
        "outcome": row["outcome"],
        "exit_reason": "opposite_cross",
        "whipsaw": True,
        "entry_no": 1,
    }


def test_the_equity_curve_ends_at_the_net_pnl(service: BacktestService) -> None:
    result = service.trades(Filters())
    assert result["equity"][-1][1] == result["kpis"]["net_pnl"]
    assert len(result["equity"]) == 9


# -------------------------------------------------------------------- detail


def test_a_trade_detail_has_the_chart_data(service: BacktestService) -> None:
    rows = service.trades(Filters(mode=Mode.ITM5_BUY))["trades"]
    detail = service.trade_detail(5, rows[0]["id"], None, Filters())
    card = detail["trade"]
    assert card["symbol"] == f"{S}-24050-PE"
    assert (card["direction"], card["side"], card["dte"], card["quantity"]) == (
        "bearish",
        "BUY",
        0,
        65,
    )
    assert card["expiry"] == "2026-05-12"
    assert (card["option_entry_fill"], card["option_exit_fill"]) == (211.55, 188.55)
    assert card["costs"]["net"] == -1571.91
    assert card["costs"]["charges_total"] == 76.91
    assert card["cost_verified"] is True  # 2026-05-12 is inside the verified 2026-04-01 rates
    assert card["rates"]["effective_from"] == "2026-04-01"
    assert card["rates"]["stt_sell_pct"] == 0.15
    bars = detail["spot"]["bars"]
    assert len(bars) == 75
    assert bars[0][0] == "2026-05-12T09:15:00"
    assert all(len(b) == 7 for b in bars)
    assert (detail["spot"]["fast"], detail["spot"]["slow"]) == (1, 3)
    assert [c[0] for c in detail["option"]["candles"]] == [
        "2026-05-12T09:45:00",
        "2026-05-12T09:55:00",
    ]
    assert detail["option"]["candles"][0][1] == 210.0


def test_detail_rejects_a_stale_run_key_and_unknown_ids(service: BacktestService) -> None:
    rows = service.trades(Filters())["trades"]
    with pytest.raises(ResultsChangedError):
        service.trade_detail(5, rows[0]["id"], "not-the-current-run", Filters())
    with pytest.raises(ResultsChangedError):
        service.trade_detail(5, 999, None, Filters())
    with pytest.raises(ResultsChangedError):
        service.trade_detail(5, -1, None, Filters())


def test_detail_entry_number_follows_the_filters(service: BacktestService) -> None:
    rows = service.trades(Filters(start=time(10, 0), mode=Mode.ATM_BUY))["trades"]
    detail = service.trade_detail(5, rows[0]["id"], None, Filters(start=time(10, 0)))
    assert detail["trade"]["entry_no"] == 1
    assert detail["trade"]["signal_no"] == 3  # the third crossover of that day


# --------------------------------------------------------------------- cache


def test_results_are_cached_in_postgres_and_identical_when_reloaded(
    service: BacktestService,
) -> None:
    first = service.trades(Filters())
    assert first["computed"]["from_cache"] is False
    service.runs.forget_memory()
    second = service.trades(Filters())
    assert second["computed"]["from_cache"] is True
    assert second["run"] == first["run"]
    assert second["trades"] == first["trades"]
    assert second["kpis"] == first["kpis"]
    detail_a = service.trade_detail(5, 0, first["run"], Filters())
    service.runs.forget_memory()
    assert service.trade_detail(5, 0, first["run"], Filters()) == detail_a


def test_the_cache_is_per_timeframe_and_pruned_when_data_changes(
    service: BacktestService, seeded: str
) -> None:
    service.trades(Filters(timeframe=5))
    service.trades(Filters(timeframe=15))
    with session(seeded) as conn:
        assert conn.execute("SELECT count(*) FROM backtest_runs").fetchone() == (2,)
        key_before = conn.execute(
            "SELECT cache_key FROM backtest_runs WHERE timeframe = 5"
        ).fetchone()
    # any download/import changes the data version, hence the key
    with session(seeded) as conn:
        Store(conn).log_fetch("option", "NSE-NIFTY-12May26-99999-CE", DAY, "empty")
    fresh = service.trades(Filters(timeframe=5))
    with session(seeded) as conn:
        assert conn.execute("SELECT count(*) FROM backtest_runs").fetchone() == (2,)
        key_after = conn.execute(
            "SELECT cache_key FROM backtest_runs WHERE timeframe = 5"
        ).fetchone()
    assert key_after is not None
    assert key_after != key_before
    assert fresh["run"] == key_after[0]
    assert fresh["computed"]["from_cache"] is False


def test_the_cache_key_reflects_every_input() -> None:
    base = cache_key(default_params(5), DEFAULT_SCHEDULE, "v1")
    assert base == cache_key(default_params(5), DEFAULT_SCHEDULE, "v1")
    assert base != cache_key(default_params(15), DEFAULT_SCHEDULE, "v1")
    assert base != cache_key(default_params(5), DEFAULT_SCHEDULE, "v2")
    assert base != cache_key(replace(default_params(5), lot_size=75), DEFAULT_SCHEDULE, "v1")
    cheaper = type(DEFAULT_SCHEDULE)(
        [replace(r, brokerage_per_order=r.brokerage_per_order + 1) for r in DEFAULT_SCHEDULE.rows]
    )
    assert base != cache_key(default_params(5), cheaper, "v1")


def test_the_first_days_are_a_warm_up_unless_the_data_is_short() -> None:
    assert trade_start(date(2026, 1, 1), date(2026, 12, 31)) == date(2026, 1, 15)
    assert trade_start(date(2026, 5, 12), date(2026, 5, 12)) == date(2026, 5, 12)
    assert trade_start(date(2026, 5, 1), date(2026, 5, 10)) == date(2026, 5, 1)


# --------------------------------------------------------------------- skips


def test_missing_option_data_shows_up_as_a_listed_skip(db_url: str) -> None:
    source = FakeSource()
    del source.option_rows[f"{S}-24000-PE"]
    conn = connect(db_url)
    try:
        run(make(source, Store(conn)))
    finally:
        conn.close()
    service = BacktestService(db_url, params_for=params_for)
    result = service.trades(Filters())
    assert result["kpis"]["trades"] == 8
    skips = service.skips(5, None, None)
    assert skips["skip_reasons"] == {"no_option_data": 1}
    assert skips["skipped_trades"] == [
        {
            "date": "2026-05-12",
            "mode": "atm_buy",
            "decision": "12:40",
            "reason": "no_option_data",
            "detail": skips["skipped_trades"][0]["detail"],
        }
    ]
    assert skips["day_counts"] == {"ok": 1}


def test_skips_respect_the_date_range(db_url: str) -> None:
    source = FakeSource()
    source.option_rows.clear()
    conn = connect(db_url)
    try:
        run(make(source, Store(conn)))
    finally:
        conn.close()
    service = BacktestService(db_url, params_for=params_for)
    assert len(service.skips(5, None, None)["skipped_trades"]) == 9
    assert service.skips(5, date(2026, 5, 13), None)["skipped_trades"] == []
    assert service.skips(5, None, date(2026, 5, 11))["day_counts"] == {}


# ---------------------------------------------------------------- meta + data


def test_meta_describes_the_options_and_the_rules(service: BacktestService) -> None:
    meta = service.meta()
    assert meta["has_data"] is True
    assert meta["timeframes"] == [5, 15, 30]
    assert [m["value"] for m in meta["modes"]] == ["itm5_buy", "atm_buy", "atm_sell"]
    assert meta["starts"] == ["09:30", "10:00", "10:30"]
    assert meta["cutoffs"][0] == "11:30"
    assert meta["cutoffs"][-1] == "14:30"
    assert len(meta["cutoffs"]) == 7
    assert meta["strategy"]["lot_size"] == 65
    assert meta["strategy"]["square_off"] == "15:00"
    assert meta["strategy"]["slippage_points"] == 0.5
    assert meta["cost_schedule"][-1]["effective_from"] == "2026-04-01"
    assert any(not row["verified"] for row in meta["cost_schedule"])
    assert {r["title"] for r in meta["rules"]} >= {"Signal", "Exit and reversal", "Gross vs net"}


def test_data_status_summarises_the_downloads(service: BacktestService) -> None:
    status = service.data_status()
    assert status["has_data"] is True
    assert status["spot_range"] == ["2026-05-12", "2026-05-12"]
    assert status["spot_rows"] == 375
    assert status["option_rows"] == 18
    assert status["timestamp_convention"] == "start"
    kinds = {(row["kind"], row["status"]): row["days"] for row in status["fetch_summary"]}
    assert kinds[("spot", "ok")] == 1
    assert kinds[("option", "ok")] == 9
    assert status["failures"] == []
