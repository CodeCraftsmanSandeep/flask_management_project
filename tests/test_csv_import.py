"""CSV import: files from any provider end up in exactly the tables the Groww download fills."""

import csv
import gzip
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.db.connection import connect
from app.history.backfill import CONVENTION_KEY, Backfill, Throttle
from app.history.csv_import import (
    CsvImporter,
    ImportDataError,
    ImportReport,
    TimestampMode,
    _is_nifty,
    parse_date,
    parse_stamp,
    read_expiries,
)
from app.history.store import Store
from app.history.synthetic import DATA_SOURCE_KEY, SYNTHETIC_SOURCE, SyntheticSource
from app.strategy.engine import StrategyParams
from app.strategy.options import expiry_from_symbol
from scripts.import_csv import main

FIRST = date(2026, 3, 2)
LAST = date(2026, 3, 13)
DAY = date(2026, 3, 9)  # a Monday
SYMBOL = "NSE-NIFTY-10Mar26-23000-CE"
OHLC = ("open", "high", "low", "close")


# ------------------------------------------------------------------------------- helpers


def write_csv(path: Path, header: list[str], rows: list[Any], *, delimiter: str = ",") -> Path:
    if path.suffix == ".gz":
        handle = gzip.open(path, "wt", newline="")  # noqa: SIM115
    else:
        handle = path.open("w", newline="")
    with handle:
        writer = csv.writer(handle, delimiter=delimiter)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def minutes(
    day: date, price: int = 23000, *, offset: int = 0, count: int = 375
) -> list[tuple[Any, ...]]:
    """(timestamp, open, high, low, close, volume) for one session of constant-ish prices."""
    first = datetime.combine(day, time(9, 15)) + timedelta(minutes=offset)
    return [
        (str(first + timedelta(minutes=i)), price, price + 1, price - 1, price, 0)
        for i in range(count)
    ]


def option_rows(day: date, symbol: str = SYMBOL, price: int = 100) -> list[tuple[Any, ...]]:
    return [(symbol, *row, 5) for row in minutes(day, price)]


SPOT_HEADER = ["timestamp", *OHLC, "volume"]
OPTION_HEADER = ["symbol", "timestamp", *OHLC, "volume", "oi"]


def mini(tmp_path: Path, *, day: date = DAY) -> dict[str, Path]:
    """One clean session of index + one option contract + the expiry list."""
    spot = write_csv(tmp_path / "spot.csv", SPOT_HEADER, minutes(day))
    options = write_csv(tmp_path / "options.csv", OPTION_HEADER, option_rows(day))
    expiries = tmp_path / "expiries.csv"
    expiries.write_text("expiry\n2026-03-10\n")
    return {"spot": spot, "options": options, "expiries": expiries}


def do_import(
    db_url: str,
    *,
    lenient: bool = False,
    delimiter: str = ",",
    label: str = "test",
    **run: Any,
) -> ImportReport:
    conn = connect(db_url)
    try:
        importer = CsvImporter(Store(conn), label=label, delimiter=delimiter, lenient=lenient)
        return importer.run(**run)
    finally:
        conn.close()


def import_mini(db_url: str, files: dict[str, Path], **extra: Any) -> ImportReport:
    return do_import(
        db_url,
        spot_files=[files["spot"]],
        option_files=[files["options"]],
        expiries_file=files["expiries"],
        **extra,
    )


def scalar(db_url: str, sql: str) -> Any:
    with connect(db_url) as conn:
        row = conn.execute(sql).fetchone()
    return row[0] if row else None


# ------------------------------------------------------- the same data, downloaded vs imported


@dataclass(frozen=True)
class Snapshot:
    spot: list[tuple[Any, ...]]
    options: list[tuple[Any, ...]]
    expiries: list[date]


def snapshot(db_url: str) -> Snapshot:
    with connect(db_url) as conn:
        spot = conn.execute(
            "SELECT ts, open, high, low, close, volume FROM spot_candles ORDER BY ts"
        ).fetchall()
        options = conn.execute(
            "SELECT symbol, ts, open, high, low, close, volume, oi FROM option_candles "
            "ORDER BY symbol, ts"
        ).fetchall()
        expiries = [r[0] for r in conn.execute("SELECT expiry FROM expiries ORDER BY expiry")]
    return Snapshot(spot, options, expiries)


def reset(db_url: str) -> None:
    with connect(db_url) as conn:
        conn.execute("TRUNCATE spot_candles, option_candles, fetch_log, expiries, contracts, meta")
        conn.commit()


@pytest.fixture
def downloaded(db_url: str) -> Snapshot:
    """A small real pipeline run (simulated source); the database is left holding it."""
    conn = connect(db_url)
    try:
        Backfill(
            SyntheticSource(FIRST, LAST, seed=5),
            Store(conn),
            throttle=Throttle(interval=0),
            sleep=lambda _s: None,
            today=lambda: LAST + timedelta(days=1),
        ).run(date(2026, 3, 9), LAST, [StrategyParams(timeframe=5)])
    finally:
        conn.close()
    shot = snapshot(db_url)
    assert shot.spot
    assert shot.options
    return shot


def spot_rows(shot: Snapshot, *, shift: timedelta = timedelta(0), utc: bool = False) -> list[Any]:
    rows = []
    for ts, o, h, low, c, v in shot.spot:
        moment = ts + shift
        stamp = (
            (moment - timedelta(hours=5, minutes=30)).isoformat() + "+00:00" if utc else str(moment)
        )
        rows.append((stamp, o, h, low, c, v))
    return rows


def test_a_symbol_file_gives_the_same_tables_as_the_download(
    db_url: str, tmp_path: Path, downloaded: Snapshot
) -> None:
    reset(db_url)
    spot = write_csv(tmp_path / "spot.csv", SPOT_HEADER, spot_rows(downloaded))
    options = write_csv(
        tmp_path / "options.csv.gz",
        OPTION_HEADER,
        [(s, ts, o, h, low, c, v, oi) for s, ts, o, h, low, c, v, oi in downloaded.options],
    )
    expiries = write_csv(tmp_path / "expiries.csv", ["expiry"], [(e,) for e in downloaded.expiries])

    report = do_import(db_url, spot_files=[spot], option_files=[options], expiries_file=expiries)

    assert snapshot(db_url) == downloaded
    assert report.convention == "start"
    assert report.option_rows == len(downloaded.options)
    assert scalar(db_url, "SELECT value FROM meta WHERE key = 'data_source'") == "csv_import"
    assert (
        scalar(db_url, "SELECT value FROM meta WHERE key = 'candle_timestamp_convention'")
        == "start"
    )
    assert CONVENTION_KEY == "candle_timestamp_convention"


def test_other_layouts_work_too_utc_times_split_date_time_columns_tab_delimited(
    db_url: str, tmp_path: Path, downloaded: Snapshot
) -> None:
    reset(db_url)
    spot = write_csv(
        tmp_path / "spot.tsv", SPOT_HEADER, spot_rows(downloaded, utc=True), delimiter="\t"
    )
    options = write_csv(
        tmp_path / "options.tsv",
        ["Expiry", "Strike", "Type", "Date", "Time", "O", "H", "L", "C", "Vol", "Open Interest"],
        [
            (
                expiry_from_symbol(s).strftime("%d-%m-%Y"),
                s.split("-")[3],
                "CALL" if s.endswith("CE") else "PUT",
                ts.date().isoformat(),
                ts.strftime("%H:%M"),
                o,
                h,
                low,
                c,
                v,
                oi,
            )
            for s, ts, o, h, low, c, v, oi in downloaded.options
        ],
        delimiter="\t",
    )
    expiries = tmp_path / "expiries.txt"
    expiries.write_text("\n".join(f"{e:%d/%m/%Y}" for e in downloaded.expiries) + "\n")

    do_import(
        db_url,
        delimiter="\t",
        spot_files=[spot],
        option_files=[options],
        expiries_file=expiries,
    )

    assert snapshot(db_url) == downloaded


def test_end_of_minute_stamps_are_detected_and_shifted_back(
    db_url: str, tmp_path: Path, downloaded: Snapshot
) -> None:
    reset(db_url)
    minute = timedelta(minutes=1)
    spot = write_csv(tmp_path / "spot.csv", SPOT_HEADER, spot_rows(downloaded, shift=minute))
    options = write_csv(
        tmp_path / "options.csv",
        OPTION_HEADER,
        [
            (s, ts + minute, o, h, low, c, v, oi)
            for s, ts, o, h, low, c, v, oi in downloaded.options
        ],
    )
    expiries = write_csv(tmp_path / "e.csv", ["expiry"], [(e,) for e in downloaded.expiries])

    report = do_import(db_url, spot_files=[spot], option_files=[options], expiries_file=expiries)

    assert report.convention == "close"
    assert snapshot(db_url) == downloaded


def test_the_download_log_records_what_was_imported(
    db_url: str, tmp_path: Path, downloaded: Snapshot
) -> None:
    reset(db_url)
    files = mini(tmp_path)
    import_mini(db_url, files, label="vendor-x")

    with connect(db_url) as conn:
        rows = conn.execute("SELECT kind, key, day, status, rows, detail FROM fetch_log").fetchall()
    kinds = {r[0] for r in rows}
    assert kinds == {"spot", "option", "expiries"}
    spot = next(r for r in rows if r[0] == "spot")
    assert (spot[1], spot[2], spot[3], spot[4]) == ("NSE-NIFTY", DAY, "ok", 375)
    option = next(r for r in rows if r[0] == "option")
    assert (option[1], option[2], option[3], option[4]) == (SYMBOL, DAY, "ok", 375)
    assert all("vendor-x" in r[5] for r in rows)


def test_the_import_labels_are_read_back_and_described(db_url: str, tmp_path: Path) -> None:
    reset(db_url)
    import_mini(db_url, mini(tmp_path), label="huggingface")
    with connect(db_url) as conn:
        labels = Store(conn).import_labels()
    assert labels == ["huggingface"]

    from app.backtest.service import BacktestService

    status = BacktestService(db_url).data_status()
    assert status["data_source"]["kind"] == "huggingface"
    assert "thetrademarkk/india-index-options-1m" in status["data_source"]["title"]
    assert BacktestService(db_url).meta()["data_source"] == status["data_source"]


# ---------------------------------------------------------------- nothing half-written


def test_a_dry_run_reads_everything_and_writes_nothing(db_url: str, tmp_path: Path) -> None:
    report = import_mini(db_url, mini(tmp_path), dry_run=True)

    assert report.dry_run
    assert report.spot_rows == 375
    assert report.option_rows == 375
    assert report.option_contracts == 1
    assert report.expiries == 1
    assert scalar(db_url, "SELECT count(*) FROM spot_candles") == 0
    assert scalar(db_url, "SELECT count(*) FROM option_candles") == 0
    assert scalar(db_url, "SELECT count(*) FROM meta") == 0


def test_a_bad_option_row_stops_everything_before_anything_is_written(
    db_url: str, tmp_path: Path
) -> None:
    files = mini(tmp_path)
    rows = option_rows(DAY)
    rows[-1] = (SYMBOL, "2026-03-09 15:29:00", "oops", 1, 1, 1, 0, 0)
    write_csv(files["options"], OPTION_HEADER, rows)

    with pytest.raises(
        ImportDataError, match=r"options\.csv line 376: open 'oops' is not a number"
    ):
        import_mini(db_url, files)

    assert scalar(db_url, "SELECT count(*) FROM spot_candles") == 0
    assert scalar(db_url, "SELECT count(*) FROM expiries") == 0


def test_skip_bad_rows_keeps_the_rest_and_says_what_was_skipped(
    db_url: str, tmp_path: Path
) -> None:
    files = mini(tmp_path)
    rows = option_rows(DAY)
    rows[10] = (SYMBOL, rows[10][1], "", 1, 1, 1, 0, 0)
    write_csv(files["options"], OPTION_HEADER, rows)

    report = import_mini(db_url, files, lenient=True)

    assert report.bad_rows == 1
    assert report.option_rows == 374
    assert any("1 unreadable option rows" in w and "line 12" in w for w in report.warnings)


def test_a_repeat_import_changes_nothing(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    import_mini(db_url, files)
    first = snapshot(db_url)
    import_mini(db_url, files)
    assert snapshot(db_url) == first


# ------------------------------------------------------------------------------ the time stamps


def test_unclear_timestamps_are_never_guessed(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    # 09:20 .. 15:24: neither 09:15/15:29 nor 09:16/15:30, so no evidence either way
    write_csv(files["spot"], SPOT_HEADER, minutes(DAY, offset=5, count=365))
    with pytest.raises(ImportDataError, match="--timestamps"):
        import_mini(db_url, files)

    report = import_mini(db_url, files, timestamps="start")
    assert report.convention == "start"
    assert report.incomplete_spot_days == 1


def test_an_explicit_choice_that_contradicts_the_data_is_refused(
    db_url: str, tmp_path: Path
) -> None:
    with pytest.raises(ImportDataError, match="contradicts the data"):
        import_mini(db_url, mini(tmp_path), timestamps="close")


def test_the_stored_convention_cannot_be_mixed_with_another(db_url: str, tmp_path: Path) -> None:
    with connect(db_url) as conn:
        Store(conn).set_meta(CONVENTION_KEY, "close")
    with pytest.raises(ImportDataError, match="already stored uses the 'close' convention"):
        import_mini(db_url, mini(tmp_path))


def test_an_options_only_import_uses_the_stored_convention(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    do_import(db_url, spot_files=[files["spot"]], expiries_file=files["expiries"])
    report = do_import(db_url, option_files=[files["options"]])
    assert report.convention == "start"
    assert report.option_rows == 375


def test_an_options_only_import_without_any_known_convention_needs_a_choice(
    db_url: str, tmp_path: Path
) -> None:
    files = mini(tmp_path)
    with connect(db_url) as conn:
        Store(conn).save_expiries("NIFTY", [date(2026, 3, 10)])
    with pytest.raises(ImportDataError, match="--timestamps"):
        do_import(db_url, option_files=[files["options"]])
    report = do_import(db_url, option_files=[files["options"]], timestamps="start")
    assert report.option_rows == 375


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-03-09 09:15", datetime(2026, 3, 9, 9, 15)),
        ("2026-03-09T09:15:00", datetime(2026, 3, 9, 9, 15)),
        ("2026-03-09T09:15:00+05:30", datetime(2026, 3, 9, 9, 15)),
        ("2026-03-09T03:45:00+00:00", datetime(2026, 3, 9, 9, 15)),
        ("2026-03-09T03:45:00Z", datetime(2026, 3, 9, 9, 15)),
        ("09-03-2026 09:15", datetime(2026, 3, 9, 9, 15)),
        ("09/03/2026 09:15:00", datetime(2026, 3, 9, 9, 15)),
        ("09-Mar-2026 09:15", datetime(2026, 3, 9, 9, 15)),
        ("1773027900", datetime(2026, 3, 9, 9, 15)),
        ("1773027900000", datetime(2026, 3, 9, 9, 15)),
    ],
)
def test_times_in_common_formats_become_ist_wall_clock(text: str, expected: datetime) -> None:
    assert parse_stamp(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "2026-03-09",  # a date alone would silently become midnight
        "09:15",
        "2026-03-09 09:15:30",  # not on a whole minute
        "2026-13-09 09:15",
        "20260309",  # looks like epoch seconds but lands in 1970
        "yesterday",
    ],
)
def test_unusable_times_are_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="time"):
        parse_stamp(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-03-10", date(2026, 3, 10)),
        ("2026-03-10 15:30:00", date(2026, 3, 10)),
        ("10-03-2026", date(2026, 3, 10)),
        ("10/03/2026", date(2026, 3, 10)),
        ("10-Mar-2026", date(2026, 3, 10)),
        ("10-MAR-26", date(2026, 3, 10)),
    ],
)
def test_dates_are_read_day_first_unless_iso(text: str, expected: date) -> None:
    assert parse_date(text) == expected


def test_an_ambiguous_or_broken_date_is_rejected() -> None:
    for text in ("03/10/26", "2026-02-30", "soon"):
        with pytest.raises(ValueError, match="date"):
            parse_date(text)


# -------------------------------------------------------------------------- reading problems


def test_a_missing_column_is_named_with_the_columns_that_were_found(
    db_url: str, tmp_path: Path
) -> None:
    files = mini(tmp_path)
    write_csv(files["spot"], ["timestamp", "open", "high", "low"], [("2026-03-09 09:15", 1, 2, 0)])
    with pytest.raises(ImportDataError, match=r"missing column\(s\) close.*Columns present"):
        import_mini(db_url, files)


def test_option_files_must_say_which_contract_each_row_is(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    write_csv(files["options"], ["timestamp", *OHLC], [("2026-03-09 09:15", 1, 2, 0, 1)])
    with pytest.raises(ImportDataError, match="cannot tell which contract"):
        import_mini(db_url, files)


def test_a_provider_symbol_we_cannot_parse_is_explained(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    write_csv(
        files["options"],
        OPTION_HEADER,
        [("NIFTY2631023000CE", "2026-03-09 09:15", 1, 2, 0, 1, 0, 0)],
    )
    with pytest.raises(ImportDataError, match="give expiry, strike and option_type columns"):
        import_mini(db_url, files)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("23000.12345", "more than 4 decimal places"),
        ("-5", "non-negative"),
        ("nan", "finite"),
        ("1,5", "not a number"),
    ],
)
def test_prices_must_be_exact_finite_and_not_negative(
    db_url: str, tmp_path: Path, value: str, message: str
) -> None:
    files = mini(tmp_path)
    rows = minutes(DAY)
    rows[3] = (rows[3][0], value, rows[3][2], rows[3][3], rows[3][4], 0)
    write_csv(files["spot"], SPOT_HEADER, rows)
    with pytest.raises(ImportDataError, match=message):
        import_mini(db_url, files)


def test_a_file_that_is_not_there_or_is_empty_is_explained(db_url: str, tmp_path: Path) -> None:
    with pytest.raises(ImportDataError, match="File not found"):
        do_import(
            db_url, spot_files=[tmp_path / "nope.csv"], timestamps="start", derive_expiries=True
        )
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    with pytest.raises(ImportDataError, match="is empty"):
        do_import(db_url, spot_files=[empty], timestamps="start", derive_expiries=True)


def test_a_binary_file_is_reported_not_crashed_on(db_url: str, tmp_path: Path) -> None:
    junk = tmp_path / "spot.csv"
    junk.write_bytes(b"\xff\xfe\x00\x81\x82" * 50)
    with pytest.raises(ImportDataError, match="cannot be read"):
        do_import(db_url, spot_files=[junk], timestamps="start", derive_expiries=True)


# ----------------------------------------------------------------------------- other indices


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("NIFTY", True),
        ("Nifty 50", True),
        ("NSE:NIFTY50", True),
        ("NSE-NIFTY", True),
        ("NIFTY_50", True),
        ("BANKNIFTY", False),
        ("NIFTY BANK", False),
        ("FINNIFTY", False),
        ("NIFTYNXT50", False),
        ("SENSEX", False),
    ],
)
def test_only_the_nifty_50_counts_as_nifty(text: str, expected: bool) -> None:
    assert _is_nifty(text) is expected


def test_rows_of_other_indices_are_skipped_and_counted(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    write_csv(
        files["spot"],
        ["symbol", *SPOT_HEADER],
        [("NIFTY 50", *r) for r in minutes(DAY)]
        + [("NIFTY BANK", *r) for r in minutes(DAY, 51000)],
    )
    write_csv(
        files["options"],
        ["underlying", "expiry", "strike", "option_type", *SPOT_HEADER],
        [("NIFTY", "2026-03-10", 23000, "CE", *r) for r in minutes(DAY, 100)]
        + [("BANKNIFTY", "2026-03-10", 51000, "CE", *r) for r in minutes(DAY, 300)],
    )

    report = import_mini(db_url, files)

    assert report.skipped_other_instruments == 375 + 375
    assert report.option_contracts == 1
    assert scalar(db_url, "SELECT max(high) FROM spot_candles") == Decimal("23001")
    assert scalar(db_url, "SELECT count(DISTINCT symbol) FROM option_candles") == 1


def test_a_symbol_of_another_index_is_skipped(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    write_csv(
        files["options"],
        OPTION_HEADER,
        option_rows(DAY) + option_rows(DAY, "NSE-BANKNIFTY-10Mar26-51000-CE"),
    )
    report = import_mini(db_url, files)
    assert report.option_contracts == 1
    assert report.skipped_other_instruments == 375


def test_strikes_far_from_the_index_are_flagged(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    write_csv(files["options"], OPTION_HEADER, option_rows(DAY, "NSE-NIFTY-10Mar26-51000-CE"))
    report = import_mini(db_url, files, dry_run=True)
    assert any("far outside NIFTY's range" in w for w in report.warnings)


# --------------------------------------------------------------------------------- expiries


def test_expiries_must_come_from_somewhere_explicit(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    with pytest.raises(ImportDataError, match="--expiries FILE"):
        do_import(db_url, spot_files=[files["spot"]], option_files=[files["options"]])


def test_expiries_from_the_option_files_come_with_a_warning(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    report = do_import(
        db_url, spot_files=[files["spot"]], option_files=[files["options"]], derive_expiries=True
    )
    assert report.expiries == 1
    assert any("derived from the option files" in w for w in report.warnings)
    assert scalar(db_url, "SELECT expiry FROM expiries") == date(2026, 3, 10)


def test_choosing_both_expiry_sources_is_refused(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    with pytest.raises(ImportDataError, match="either"):
        import_mini(db_url, files, derive_expiries=True)


def test_expiries_already_stored_are_enough_for_a_later_import(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    import_mini(db_url, files)
    report = do_import(db_url, spot_files=[files["spot"]], option_files=[files["options"]])
    assert report.option_rows == 375


def test_coverage_gaps_are_reported(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    files["expiries"].write_text("2026-03-03\n")  # nothing on or after the trade day
    report = import_mini(db_url, files, dry_run=True)
    assert any("no expiry on or after" in w for w in report.warnings)
    assert any("not in the expiry list" in w for w in report.warnings)


def test_spot_days_without_option_candles_and_incomplete_days_are_reported(
    db_url: str, tmp_path: Path
) -> None:
    files = mini(tmp_path)
    next_day = DAY + timedelta(days=1)
    write_csv(files["spot"], SPOT_HEADER, minutes(DAY) + minutes(next_day, count=300))
    report = import_mini(db_url, files, dry_run=True)
    assert report.spot_days == 2
    assert report.incomplete_spot_days == 1
    assert any("do not have all 375 minutes" in w for w in report.warnings)


def test_expiry_files_accept_headers_day_first_dates_and_reject_junk(tmp_path: Path) -> None:
    plain = tmp_path / "a.txt"
    plain.write_text("10/03/2026\n17/03/2026\n\n10/03/2026\n")
    assert read_expiries(plain) == [date(2026, 3, 10), date(2026, 3, 17)]

    headed = tmp_path / "b.csv"
    headed.write_text("name,expiry_date\nNIFTY,2026-03-17\nNIFTY,2026-03-10\n")
    assert read_expiries(headed) == [date(2026, 3, 10), date(2026, 3, 17)]

    junk = tmp_path / "c.txt"
    junk.write_text("expiry\n2026-03-10\nsoon\n")
    with pytest.raises(ImportDataError, match="line 3"):
        read_expiries(junk)

    nothing = tmp_path / "d.txt"
    nothing.write_text("expiry\n")
    with pytest.raises(ImportDataError, match="no expiry dates"):
        read_expiries(nothing)


# ------------------------------------------------------------------------------- safety


def test_simulated_data_never_gets_real_data_mixed_in(db_url: str, tmp_path: Path) -> None:
    with connect(db_url) as conn:
        Store(conn).set_meta(DATA_SOURCE_KEY, SYNTHETIC_SOURCE)
    with pytest.raises(ImportDataError, match="simulated demo data"):
        import_mini(db_url, mini(tmp_path))


def test_the_data_source_label_of_existing_data_is_kept(db_url: str, tmp_path: Path) -> None:
    with connect(db_url) as conn:
        Store(conn).set_meta(DATA_SOURCE_KEY, "something_else")
    import_mini(db_url, mini(tmp_path))
    assert scalar(db_url, "SELECT value FROM meta WHERE key = 'data_source'") == "something_else"


def test_nothing_to_import_is_an_error(db_url: str) -> None:
    with pytest.raises(ImportDataError, match="Nothing to import"):
        do_import(db_url)


def test_an_import_can_be_cut_into_many_batches(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    conn = connect(db_url)
    try:
        importer = CsvImporter(Store(conn), batch_size=50)
        report = importer.run(
            spot_files=[files["spot"]],
            option_files=[files["options"]],
            expiries_file=files["expiries"],
        )
    finally:
        conn.close()
    assert report.option_rows == 375
    assert scalar(db_url, "SELECT count(*) FROM option_candles") == 375


def test_candles_outside_the_session_are_dropped_and_counted(db_url: str, tmp_path: Path) -> None:
    files = mini(tmp_path)
    extra = [
        (SYMBOL, f"{DAY} 15:30:00", 1, 2, 0, 1, 0, 0),
        (SYMBOL, f"{DAY} 09:00:00", 1, 2, 0, 1, 0, 0),
    ]
    write_csv(files["options"], OPTION_HEADER, option_rows(DAY) + extra)
    report = import_mini(db_url, files)
    assert report.dropped_out_of_session == 2
    assert report.option_rows == 375


@pytest.mark.parametrize("mode", ["auto", "start"])
def test_timestamp_modes_are_accepted(db_url: str, tmp_path: Path, mode: TimestampMode) -> None:
    assert import_mini(db_url, mini(tmp_path), timestamps=mode).convention == "start"


# ---------------------------------------------------------------------------- the command


def test_the_command_checks_then_imports(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = mini(tmp_path)
    args = [
        "--spot", str(files["spot"]),
        "--options", str(files["options"]),
        "--expiries", str(files["expiries"]),
        "--database-url", db_url,
    ]  # fmt: skip

    assert main([*args, "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "Dry run: nothing was written." in out
    assert "options  375 candles, 1 contracts" in out
    assert scalar(db_url, "SELECT count(*) FROM option_candles") == 0

    assert main(args) == 0
    assert "Imported." in capsys.readouterr().out
    assert scalar(db_url, "SELECT count(*) FROM option_candles") == 375


def test_an_unreachable_database_is_explained_not_a_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files = mini(tmp_path)
    code = main(
        ["--spot", str(files["spot"]), "--derive-expiries", "--database-url",
         "postgresql://trading@127.0.0.1:5433/no_such_database_here"]
    )  # fmt: skip
    assert code == 1
    err = capsys.readouterr().err
    assert "cannot reach the database" in err
    assert "make db-start" in err


def test_the_command_reports_problems_plainly(
    db_url: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        ["--spot", str(tmp_path / "missing.csv"), "--derive-expiries", "--database-url", db_url]
    )
    assert code == 1
    assert "Not imported: File not found" in capsys.readouterr().err
