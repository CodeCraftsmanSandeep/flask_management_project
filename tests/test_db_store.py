from collections.abc import Iterator
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from app.db.connection import connect, session
from app.db.migrate import migrate, migration_files
from app.history.store import Store
from app.strategy.models import Candle

D = Decimal
DAY = date(2026, 5, 12)


def minute(day: date, hhmm: str) -> datetime:
    return datetime.fromisoformat(f"{day.isoformat()}T{hhmm}:00")


def mk(day: date, hhmm: str, price: str, *, volume: int = 10, oi: int | None = None) -> Candle:
    p = D(price)
    return Candle(minute(day, hhmm), p, p + 1, p - 1, p, volume, oi)


@pytest.fixture
def store(db_url: str) -> Iterator[Store]:
    conn = connect(db_url)
    try:
        yield Store(conn)
    finally:
        conn.close()


# ------------------------------------------------------------------- migrations


def test_migrations_are_recorded_and_idempotent(db_url: str) -> None:
    names = [p.name for p in migration_files()]
    assert names
    assert names == sorted(names)
    assert migrate(db_url) == []  # the template already applied everything
    with session(db_url) as conn:
        recorded = [r[0] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert recorded == names


def test_a_failing_migration_rolls_back_and_keeps_earlier_ones(db_url: str, tmp_path: Path) -> None:
    (tmp_path / "001_ok.sql").write_text("CREATE TABLE ok_table (id int);")
    (tmp_path / "002_bad.sql").write_text("CREATE TABLE half_table (id int); SELECT 1/0;")
    with pytest.raises(psycopg.Error):
        migrate(db_url, tmp_path)
    with session(db_url) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
        versions = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    assert "ok_table" in tables
    assert "half_table" not in tables
    assert "001_ok.sql" in versions
    assert "002_bad.sql" not in versions


def test_all_tables_exist(db_url: str) -> None:
    with session(db_url) as conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        }
    assert {"spot_candles", "option_candles", "expiries", "contracts", "fetch_log"} <= tables


# ----------------------------------------------------------------------- candles


def test_spot_round_trips_exactly(store: Store) -> None:
    candles = [mk(DAY, "09:15", "24000.15"), mk(DAY, "09:16", "24001.05", volume=7)]
    assert store.save_spot("NSE-NIFTY", candles) == 2
    loaded = store.load_spot("NSE-NIFTY", DAY, DAY)
    assert loaded == [
        Candle(c.start, c.open, c.high, c.low, c.close, c.volume, None) for c in candles
    ]
    assert loaded[0].open == D("24000.15")  # no float drift


def test_saving_twice_updates_instead_of_duplicating(store: Store) -> None:
    store.save_spot("NSE-NIFTY", [mk(DAY, "09:15", "100")])
    store.save_spot("NSE-NIFTY", [mk(DAY, "09:15", "200")])
    loaded = store.load_spot("NSE-NIFTY", DAY, DAY)
    assert [c.open for c in loaded] == [D(200)]
    assert store.count_candles("spot_candles") == 1


def test_spot_loads_whole_days_inclusive_and_oldest_first(store: Store) -> None:
    d1, d2, d3 = date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)
    store.save_spot(
        "NSE-NIFTY",
        [mk(d3, "15:29", "3"), mk(d1, "09:15", "1"), mk(d2, "09:15", "2"), mk(d2, "23:59", "9")],
    )
    got = store.load_spot("NSE-NIFTY", d1, d2)
    assert [c.open for c in got] == [D(1), D(2), D(9)]  # includes the very end of the last day
    assert store.load_spot("NSE-NIFTY", d3, d3)[0].open == D(3)
    assert store.load_spot("NSE-NIFTY", date(2026, 5, 14), date(2026, 5, 20)) == []


def test_symbols_do_not_mix(store: Store) -> None:
    store.save_spot("NSE-NIFTY", [mk(DAY, "09:15", "1")])
    store.save_spot("NSE-BANKNIFTY", [mk(DAY, "09:15", "2")])
    assert [c.open for c in store.load_spot("NSE-NIFTY", DAY, DAY)] == [D(1)]


def test_saving_nothing_is_fine(store: Store) -> None:
    assert store.save_spot("NSE-NIFTY", []) == 0
    assert store.save_option("X", []) == 0


def test_option_book_holds_only_the_requested_pairs(store: Store) -> None:
    other = date(2026, 5, 13)
    store.save_option("A", [mk(DAY, "09:15", "10", oi=500), mk(DAY, "09:16", "11")])
    store.save_option("B", [mk(DAY, "09:15", "20")])
    store.save_option("A", [mk(other, "09:15", "30")])
    book = store.load_option_book({DAY: {"A"}, other: {"A", "B"}})
    assert set(book.candles("A", DAY)) == {minute(DAY, "09:15"), minute(DAY, "09:16")}
    assert book.candles("A", DAY)[minute(DAY, "09:15")].oi == 500
    assert book.candles("A", DAY)[minute(DAY, "09:16")].oi is None
    assert book.candles("B", DAY) == {}  # exists in the database, but was not requested for DAY
    assert book.candles("B", other) == {}  # requested, but has no data
    assert book.candles("A", other)[minute(other, "09:15")].open == D(30)
    assert book.candles("never-loaded", DAY) == {}


def test_empty_requirements_give_an_empty_book(store: Store) -> None:
    assert store.load_option_book({}).data == {}
    assert store.load_option_book({DAY: set()}).data == {}


# --------------------------------------------------------- expiries and contracts


def test_expiries_are_unique_and_sorted(store: Store) -> None:
    store.save_expiries("NIFTY", [date(2026, 5, 19), date(2026, 5, 12)])
    store.save_expiries("NIFTY", [date(2026, 5, 12), date(2026, 5, 26)])
    store.save_expiries("BANKNIFTY", [date(2026, 5, 27)])
    assert store.load_expiries("NIFTY") == [date(2026, 5, 12), date(2026, 5, 19), date(2026, 5, 26)]


def test_contracts_are_kept_per_expiry(store: Store) -> None:
    store.save_contracts("NIFTY", DAY, ["NSE-NIFTY-12May26-24000-CE", "NSE-NIFTY-12May26-24000-PE"])
    store.save_contracts("NIFTY", DAY, ["NSE-NIFTY-12May26-24000-CE"])  # repeat is harmless
    store.save_contracts("NIFTY", date(2026, 5, 19), ["NSE-NIFTY-19May26-24000-CE"])
    assert store.load_contracts("NIFTY", DAY) == {
        "NSE-NIFTY-12May26-24000-CE",
        "NSE-NIFTY-12May26-24000-PE",
    }
    assert store.load_contracts("NIFTY", date(2026, 6, 1)) == set()


# ---------------------------------------------------------------------- fetch log


def test_fetch_log_upserts_and_reports_by_range(store: Store) -> None:
    store.log_fetch("spot", "NSE-NIFTY", DAY, "error", detail="timeout")
    store.log_fetch("spot", "NSE-NIFTY", DAY, "ok", rows=375)  # a retry succeeded
    store.log_fetch("spot", "NSE-NIFTY", date(2026, 5, 13), "empty")
    store.log_fetch("option", "A", DAY, "ok", rows=300)
    assert store.fetch_status("spot", "NSE-NIFTY", DAY, DAY) == {DAY: "ok"}
    assert store.fetch_status("spot", "NSE-NIFTY", DAY, date(2026, 5, 13)) == {
        DAY: "ok",
        date(2026, 5, 13): "empty",
    }
    assert store.fetch_status("spot", "OTHER", DAY, DAY) == {}


def test_fetch_summary_and_failures(store: Store) -> None:
    store.log_fetch("spot", "NSE-NIFTY", DAY, "ok", rows=375)
    store.log_fetch("spot", "NSE-NIFTY", date(2026, 5, 13), "ok", rows=375)
    store.log_fetch("option", "A", DAY, "error", detail="HTTP 500")
    assert store.fetch_summary() == [("option", "error", 1, 0), ("spot", "ok", 2, 750)]
    assert store.failures() == [("option", "A", DAY, "HTTP 500")]


def test_fetch_log_rejects_unknown_values(store: Store) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        store.log_fetch("nonsense", "k", DAY, "ok")  # type: ignore[arg-type]
