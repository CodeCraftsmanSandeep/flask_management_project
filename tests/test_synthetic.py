"""The simulated data source: sane prices, reproducible, and usable by the real pipeline."""

import itertools
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from app.backtest.query import Filters
from app.backtest.service import BacktestService
from app.db.connection import connect
from app.history.backfill import CONVENTION_KEY, Backfill, Throttle
from app.history.store import Store
from app.history.synthetic import (
    DATA_SOURCE_KEY,
    SYNTHETIC_SOURCE,
    SyntheticSource,
    black_scholes,
    trading_days,
)
from app.strategy.engine import StrategyParams
from scripts.demo_data import (
    DEFAULT_DEMO_URL,
    check_is_demo_database,
    database_name,
    main,
    with_database,
)

FIRST = date(2026, 3, 2)  # a Monday
LAST = date(2026, 3, 13)
NSE = {"exchange": "NSE"}


def candles(
    source: SyntheticSource, day: date, *, segment: str = "CASH", symbol: str = "NSE-NIFTY"
) -> list[list[Any]]:
    return source.get_candles(
        segment=segment,
        symbol=symbol,
        start=datetime.combine(day, datetime.min.time()),
        end=datetime.combine(day, datetime.max.time()),
        **NSE,
    )


@pytest.fixture(scope="module")
def source() -> SyntheticSource:
    return SyntheticSource(FIRST, LAST, seed=11)


# ----------------------------------------------------------------------------- spot


def test_a_spot_day_is_a_complete_session(source: SyntheticSource) -> None:
    rows = candles(source, date(2026, 3, 4))
    assert len(rows) == 375
    assert rows[0][0] == "2026-03-04T09:15:00"
    assert rows[-1][0] == "2026-03-04T15:29:00"
    stamps = [datetime.fromisoformat(r[0]) for r in rows]
    assert all(b - a == timedelta(minutes=1) for a, b in itertools.pairwise(stamps))


def test_spot_candles_are_valid_and_on_the_tick(source: SyntheticSource) -> None:
    for row in candles(source, date(2026, 3, 5)):
        _, o, h, low, c, *_ = row
        assert low <= min(o, c)
        assert h >= max(o, c)
        assert low > 0
        for price in (o, h, low, c):
            assert abs(price / 0.05 - round(price / 0.05)) < 1e-6


def test_the_index_opens_each_day_where_it_closed_the_day_before(source: SyntheticSource) -> None:
    monday = candles(source, date(2026, 3, 2))
    tuesday = candles(source, date(2026, 3, 3))
    assert abs(tuesday[0][1] - monday[-1][4]) < 0.06


def test_weekends_have_no_candles(source: SyntheticSource) -> None:
    assert candles(source, date(2026, 3, 7)) == []
    assert candles(source, date(2026, 3, 8)) == []
    assert date(2026, 3, 7) not in trading_days(FIRST, LAST)


def test_the_same_seed_gives_the_same_data_and_another_seed_differs() -> None:
    a = SyntheticSource(FIRST, LAST, seed=3)
    b = SyntheticSource(FIRST, LAST, seed=3)
    c = SyntheticSource(FIRST, LAST, seed=4)
    day = date(2026, 3, 3)
    assert candles(a, day) == candles(b, day)
    assert candles(a, day) != candles(c, day)


def test_requests_are_filtered_by_the_window_like_the_real_api(source: SyntheticSource) -> None:
    rows = source.get_candles(
        segment="CASH",
        symbol="NSE-NIFTY",
        start=datetime(2026, 3, 3, 9, 30),
        end=datetime(2026, 3, 3, 9, 34),
        **NSE,
    )
    assert [r[0][11:16] for r in rows] == ["09:30", "09:31", "09:32", "09:33", "09:34"]


def test_other_instruments_and_exchanges_return_nothing(source: SyntheticSource) -> None:
    assert candles(source, date(2026, 3, 3), symbol="NSE-BANKNIFTY") == []
    assert candles(source, date(2026, 3, 3), segment="FNO", symbol="garbage") == []
    other = source.get_candles(
        exchange="BSE",
        segment="CASH",
        symbol="NSE-NIFTY",
        start=datetime(2026, 3, 3),
        end=datetime(2026, 3, 4),
    )
    assert other == []


# ------------------------------------------------------------------ expiries/contracts


def test_weekly_expiries_are_the_tuesdays_of_the_month(source: SyntheticSource) -> None:
    got = source.get_expiries(exchange="NSE", underlying="NIFTY", year=2026, month=3)
    assert got == ["2026-03-03", "2026-03-10", "2026-03-17", "2026-03-24", "2026-03-31"]
    assert source.get_expiries(exchange="NSE", underlying="BANKNIFTY", year=2026, month=3) == []
    assert source.get_expiries(exchange="NSE", underlying="NIFTY", year=2026) == []


def test_contract_lists_cover_the_traded_range_in_groww_format(source: SyntheticSource) -> None:
    symbols = source.get_contracts(exchange="NSE", underlying="NIFTY", expiry=date(2026, 3, 10))
    assert "NSE-NIFTY-10Mar26-24000-CE" in symbols or any(s.endswith("-CE") for s in symbols)
    assert all(s.startswith("NSE-NIFTY-10Mar26-") for s in symbols)
    assert len(symbols) == len(set(symbols))
    closes = [r[4] for d in trading_days(FIRST, LAST) for r in candles(source, d)]
    nearest = int(min(closes) // 50) * 50
    assert f"NSE-NIFTY-10Mar26-{nearest}-PE" in symbols


# ---------------------------------------------------------------------------- options


def at_the_money(source: SyntheticSource, day: date) -> int:
    return int(round(float(candles(source, day)[60][4]) / 50)) * 50  # noqa: RUF046


def minute_close(source: SyntheticSource, symbol: str, day: date, minute: int) -> float:
    return float(candles(source, day, segment="FNO", symbol=symbol)[minute][4])


def test_option_prices_behave_like_options(source: SyntheticSource) -> None:
    day, expiry = date(2026, 3, 4), "10Mar26"
    atm = at_the_money(source, day)
    calls = [
        minute_close(source, f"NSE-NIFTY-{expiry}-{atm + k}-CE", day, 60) for k in (-200, 0, 200)
    ]
    puts = [
        minute_close(source, f"NSE-NIFTY-{expiry}-{atm + k}-PE", day, 60) for k in (-200, 0, 200)
    ]
    assert calls[0] > calls[1] > calls[2]  # higher strike: cheaper call
    assert puts[0] < puts[1] < puts[2]  # higher strike: dearer put
    assert all(price >= 0.05 for price in calls + puts)


def test_put_call_parity_holds_roughly(source: SyntheticSource) -> None:
    day = date(2026, 3, 4)
    atm = at_the_money(source, day)
    spot = candles(source, day)[60][4]
    call = minute_close(source, f"NSE-NIFTY-10Mar26-{atm}-CE", day, 60)
    put = minute_close(source, f"NSE-NIFTY-10Mar26-{atm}-PE", day, 60)
    assert abs((call - put) - (spot - atm)) < 0.02 * (call + put) + 2


def test_an_option_candle_is_internally_consistent(source: SyntheticSource) -> None:
    rows = candles(source, date(2026, 3, 4), segment="FNO", symbol="NSE-NIFTY-10Mar26-24000-CE")
    assert len(rows) == 375
    for _, o, h, low, c, volume, _oi in rows:
        assert low <= min(o, c)
        assert h >= max(o, c)
        assert low >= 0.05
        assert volume > 0


def test_there_are_no_candles_after_the_expiry_day(source: SyntheticSource) -> None:
    assert (
        candles(source, date(2026, 3, 4), segment="FNO", symbol="NSE-NIFTY-03Mar26-24000-CE") == []
    )
    on_expiry = candles(
        source, date(2026, 3, 3), segment="FNO", symbol="NSE-NIFTY-03Mar26-24000-CE"
    )
    assert len(on_expiry) == 375


def test_expiry_day_options_end_near_their_intrinsic_value(source: SyntheticSource) -> None:
    day = date(2026, 3, 3)
    close = candles(source, day)[-1][4]
    strike = round(close / 50) * 50 - 300  # clearly in the money
    call = minute_close(source, f"NSE-NIFTY-03Mar26-{strike}-CE", day, 374)
    assert abs(call - (close - strike)) < 0.01 * (close - strike) + 1


def test_black_scholes_edge_cases() -> None:
    assert black_scholes(24000, 23900, 0, 0.14, call=True) == 100
    assert black_scholes(24000, 24100, 0, 0.14, call=True) == 0
    assert black_scholes(24000, 24100, 0, 0.14, call=False) == 100
    assert black_scholes(24000, 24000, 7 / 365, 0.0, call=True) == 0
    assert 0 < black_scholes(24000, 24000, 7 / 365, 0.14, call=True) < 24000 * 0.05
    deep = black_scholes(24000, 20000, 1 / 365, 0.14, call=True)
    assert abs(deep - 4000) < 1


# ----------------------------------------------------------------- the demo database


def test_the_demo_script_only_touches_databases_named_demo() -> None:
    check_is_demo_database(DEFAULT_DEMO_URL)
    check_is_demo_database("postgresql://trading@127.0.0.1:5433/my_Demo_2")
    for name in ("trading", "postgres", "template1"):
        with pytest.raises(SystemExit, match="demo"):
            check_is_demo_database(f"postgresql://trading@127.0.0.1:5433/{name}")


def test_database_names_are_taken_from_the_url() -> None:
    assert database_name(DEFAULT_DEMO_URL) == "trading_demo"
    assert with_database(DEFAULT_DEMO_URL, "postgres").endswith("/postgres")


def test_the_demo_script_rejects_silly_arguments(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--days", "2"]) == 2
    assert "between" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--database-url", "postgresql://trading@127.0.0.1:5433/trading"])


def test_simulated_data_flows_through_the_real_downloader_and_engine(db_url: str) -> None:
    """The same code path as real Groww data: parse, convention detection, store, backtest."""
    start, end = date(2026, 3, 16), date(2026, 4, 10)
    source = SyntheticSource(start - timedelta(days=40), end, seed=5)
    conn = connect(db_url)
    try:
        store = Store(conn)
        store.set_meta(DATA_SOURCE_KEY, SYNTHETIC_SOURCE)
        backfill = Backfill(
            source,
            store,
            throttle=Throttle(interval=0),
            sleep=lambda _s: None,
            today=lambda: end + timedelta(days=1),
        )
        report = backfill.run(start, end, [StrategyParams(timeframe=15)])
        assert store.get_meta(CONVENTION_KEY) == "start"
    finally:
        conn.close()

    assert all(phase.errors == 0 for phase in report.phases.values()), report.phases
    assert report.phases["options"].fetched > 0

    service = BacktestService(db_url)
    assert service.meta()["synthetic"] is True
    assert service.data_status()["synthetic"] is True
    assert service.data_status()["source"] == SYNTHETIC_SOURCE
    result = service.trades(Filters(timeframe=15))
    assert result["kpis"]["trades"] > 20
    assert not result["trades"][0]["date"] < start.isoformat()
    for mode in ("itm5_buy", "atm_buy", "atm_sell"):
        assert result["compare"][mode]["trades"] > 0, mode
    # Every trade's detail loads and its costs add up.
    for row in result["trades"][:5]:
        detail = service.trade_detail(15, row["id"], result["run"], Filters(timeframe=15))
        costs = detail["trade"]["costs"]
        charges = sum(costs[k] for k in ("brokerage", "stt", "exchange", "sebi", "stamp", "gst"))
        assert costs["net"] == pytest.approx(costs["gross"] - costs["slippage"] - charges, abs=0.06)
        assert detail["option"]["candles"]
