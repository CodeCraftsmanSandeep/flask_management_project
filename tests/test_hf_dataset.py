"""The Hugging Face downloader/converter, checked on tiny Parquet files like the real ones."""

import csv
import gzip
import io
import shutil
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from app.db.connection import connect
from app.history.csv_import import CsvImporter
from app.history.store import Store
from scripts import hf_dataset as hf

IST = timezone(timedelta(hours=5, minutes=30))
TS = pa.timestamp("ns", tz="+05:30")
EXPIRIES = [date(2026, 3, 3), date(2026, 3, 10), date(2026, 3, 17), date(2026, 3, 24)]


def stamps(day: date, *, pre_open: bool, closing: bool, post: bool) -> list[datetime]:
    out: list[datetime] = []
    if pre_open:
        out.append(datetime.combine(day, time(9, 7), IST))
    first = datetime.combine(day, time(9, 15), IST)
    out += [first + timedelta(minutes=i) for i in range(375)]  # 09:15 .. 15:29
    if closing:
        out.append(datetime.combine(day, time(15, 30), IST))
    if post:
        out.append(datetime.combine(day, time(15, 45), IST))
    return out


def weekdays(first: date, last: date) -> list[date]:
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if d.weekday() < 5]


def write_index(path: Path, days: list[date]) -> None:
    times = [s for d in days for s in stamps(d, pre_open=True, closing=True, post=True)]
    price = [23000.0 + (t.minute % 7) for t in times]
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(times, type=TS),
                "open": price,
                "high": [p + 1 for p in price],
                "low": [p - 1 for p in price],
                "close": price,
                "volume": pa.array([0] * len(times), type=pa.int64()),
                "trading_day": [t.date().isoformat() for t in times],
                "symbol": ["NIFTY"] * len(times),
            }
        ),
        path,
    )


def write_options(
    path: Path, expiry: date, days: list[date], strikes: tuple[int, ...] = (23000,)
) -> None:
    rows: dict[str, list[Any]] = {k: [] for k in ("ts", "strike", "kind")}
    for day in days:
        for strike in strikes:
            for kind in ("CE", "PE"):
                for t in stamps(day, pre_open=False, closing=True, post=False):
                    rows["ts"].append(t)
                    rows["strike"].append(strike)
                    rows["kind"].append(kind)
    n = len(rows["ts"])
    pq.write_table(
        pa.table(
            {
                "timestamp": pa.array(rows["ts"], type=TS),
                "open": [100.0] * n,
                "high": [101.0] * n,
                "low": [99.0] * n,
                "close": [100.5] * n,
                "volume": pa.array([10] * n, type=pa.int64()),
                "open_interest": pa.array([500] * n, type=pa.int64()),
                "trading_day": [t.date().isoformat() for t in rows["ts"]],
                "symbol": ["NIFTY"] * n,
                "strike": pa.array(rows["strike"], type=pa.int32()),
                "option_type": rows["kind"],
                "expiry": [expiry.isoformat()] * n,
            }
        ),
        path,
    )


def read_csv(path: Path) -> list[dict[str, str]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", newline="") as handle:
        return list(csv.DictReader(handle))


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Any]:
    """Index 2 Mar..13 Mar and two weekly option files, like the real dataset."""
    index = tmp_path / "NIFTY_index.parquet"
    write_index(index, weekdays(date(2026, 3, 2), date(2026, 3, 13)))
    opt10 = tmp_path / "2026-03-10.parquet"
    write_options(opt10, date(2026, 3, 10), weekdays(date(2026, 3, 4), date(2026, 3, 10)))
    opt17 = tmp_path / "2026-03-17.parquet"
    write_options(opt17, date(2026, 3, 17), weekdays(date(2026, 3, 11), date(2026, 3, 17)))
    return {"index": index, "options": [opt10, opt17], "dir": tmp_path}


# ----------------------------------------------------------------------------- selection


def test_the_needed_expiries_run_from_the_first_to_the_nearest_after_the_last_day() -> None:
    # trades on 4 Mar..11 Mar use the 10 Mar expiry (to 10 Mar) and the 17 Mar one (after)
    assert hf.select_expiries(EXPIRIES, date(2026, 3, 4), date(2026, 3, 11)) == [
        date(2026, 3, 10),
        date(2026, 3, 17),
    ]


def test_an_expiry_day_is_its_own_nearest_expiry() -> None:
    assert hf.select_expiries(EXPIRIES, date(2026, 3, 10), date(2026, 3, 10)) == [date(2026, 3, 10)]


def test_selection_fails_clearly_when_the_dataset_ends_before_the_window() -> None:
    with pytest.raises(hf.DatasetError, match="no expiry on or after 2026-03-25"):
        hf.select_expiries(EXPIRIES, date(2026, 3, 1), date(2026, 3, 25))


# ----------------------------------------------------------------------------- conversion


def test_the_index_keeps_the_regular_session_and_the_window_only(
    files: dict[str, Any],
) -> None:
    out = files["dir"] / "spot.csv"
    rows = hf.convert_spot(files["index"], out, date(2026, 3, 9), date(2026, 3, 10))
    data = read_csv(out)

    assert rows == len(data) == 2 * 376  # 09:15..15:29 plus the 15:30 closing print, 2 days
    assert data[0]["timestamp"] == "2026-03-09 09:15:00"  # the 09:07 pre-open candle is gone
    assert data[-1]["timestamp"] == "2026-03-10 15:30:00"  # the 15:45 post-close one too
    assert {r["timestamp"][:10] for r in data} == {"2026-03-09", "2026-03-10"}
    assert list(data[0]) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_option_files_are_cut_at_the_first_day_and_joined(files: dict[str, Any]) -> None:
    out = files["dir"] / "options.csv.gz"
    rows = hf.convert_options(files["options"], out, date(2026, 3, 9))
    data = read_csv(out)

    assert rows == len(data)
    assert min(r["timestamp"] for r in data) == "2026-03-09 09:15:00"
    assert {r["expiry"] for r in data} == {"2026-03-10", "2026-03-17"}
    assert {r["option_type"] for r in data} == {"CE", "PE"}
    assert set(data[0]) >= {"expiry", "strike", "option_type", "timestamp", "open", "oi"}
    # 09 and 10 Mar from the first file, 11-17 Mar (5 weekdays) from the second: 7 days
    assert rows == 7 * 2 * 376


def test_a_file_without_the_expected_columns_is_named(files: dict[str, Any]) -> None:
    broken = files["dir"] / "2026-03-24.parquet"
    pq.write_table(pa.table({"timestamp": pa.array([], type=TS), "open": [1.0][:0]}), broken)
    with pytest.raises(hf.DatasetError, match=r"2026-03-24\.parquet lacks column\(s\) high"):
        hf.convert_options([broken], files["dir"] / "x.csv.gz", date(2026, 3, 9))


def test_timestamps_without_a_time_zone_are_refused(tmp_path: Path) -> None:
    path = tmp_path / "NIFTY_index.parquet"
    naive = pa.array([datetime(2026, 3, 9, 9, 15)], type=pa.timestamp("ns"))
    pq.write_table(
        pa.table(
            {
                "timestamp": naive,
                "open": [1.0],
                "high": [1.0],
                "low": [1.0],
                "close": [1.0],
                "volume": pa.array([0], type=pa.int64()),
            }
        ),
        path,
    )
    with pytest.raises(hf.DatasetError, match="not a time zone aware timestamp"):
        hf.convert_spot(path, tmp_path / "o.csv", date(2026, 3, 9), date(2026, 3, 9))


def test_index_days_lists_the_days_with_candles(files: dict[str, Any]) -> None:
    days = hf.index_days(files["index"])
    assert days[0] == date(2026, 3, 2)
    assert days[-1] == date(2026, 3, 13)
    assert len(days) == 10


def test_expiries_are_written_with_a_header(tmp_path: Path) -> None:
    path = tmp_path / "e.csv"
    hf.write_expiries(EXPIRIES[:2], path)
    assert path.read_text() == "expiry\n2026-03-03\n2026-03-10\n"


# -------------------------------------------------------------------- it really imports


def test_the_converted_files_import_cleanly_and_are_read_as_start_stamped(
    files: dict[str, Any], db_url: str
) -> None:
    folder: Path = files["dir"]
    hf.convert_spot(files["index"], folder / "spot.csv", date(2026, 3, 2), date(2026, 3, 13))
    hf.convert_options(files["options"], folder / "options.csv.gz", date(2026, 3, 4))
    hf.write_expiries([date(2026, 3, 10), date(2026, 3, 17)], folder / "expiries.csv")

    conn = connect(db_url)
    try:
        report = CsvImporter(Store(conn), label="huggingface").run(
            spot_files=[folder / "spot.csv"],
            option_files=[folder / "options.csv.gz"],
            expiries_file=folder / "expiries.csv",
        )
    finally:
        conn.close()

    assert report.convention == "start"
    assert report.bad_rows == 0
    assert report.spot_days == 10
    assert report.incomplete_spot_days == 0
    assert report.option_contracts == 4  # 23000 CE and PE, two expiries
    assert report.dropped_out_of_session > 0  # the 15:30 closing prints


# ---------------------------------------------------------------------------- downloading


class _Response(io.BytesIO):
    def __enter__(self) -> "_Response":
        return self


def test_a_complete_local_copy_is_not_downloaded_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "f.parquet"
    target.write_bytes(b"12345")
    monkeypatch.setattr(hf, "_open", lambda _url: pytest.fail("must not download"))
    assert hf.download("https://huggingface.co/x", target, 5) is False


def test_a_download_is_checked_against_the_listed_size_and_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    answers = iter([b"123", b"12345"])  # first attempt is cut short
    monkeypatch.setattr(hf, "_open", lambda _url: _Response(next(answers)))
    pauses: list[float] = []
    target = tmp_path / "f.parquet"

    assert hf.download("https://huggingface.co/x", target, 5, sleep=pauses.append) is True

    assert target.read_bytes() == b"12345"
    assert pauses == [2.0]
    assert not list(tmp_path.glob("*.part"))


def test_a_download_that_keeps_failing_gives_up_with_a_clear_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_url: str) -> _Response:
        raise OSError("connection reset")

    monkeypatch.setattr(hf, "_open", broken)
    with pytest.raises(hf.DatasetError, match=r"could not download f\.parquet: connection reset"):
        hf.download("https://huggingface.co/x", tmp_path / "f.parquet", 5, sleep=lambda _s: None)
    assert not list(tmp_path.glob("*.part"))


def test_only_the_dataset_host_is_ever_opened() -> None:
    with pytest.raises(hf.DatasetError, match="refusing to open"):
        hf._open("http://example.com/x")
    with pytest.raises(hf.DatasetError, match="refusing to open"):
        hf._open("file:///etc/passwd")


# --------------------------------------------------------------------------------- command


def test_the_command_downloads_only_what_the_window_needs_and_prints_the_next_step(
    files: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    opt03 = files["dir"] / "2026-03-03.parquet"
    write_options(opt03, date(2026, 3, 3), weekdays(date(2026, 2, 24), date(2026, 3, 3)))
    sources = {
        "index/NIFTY.parquet": files["index"],
        "options/NIFTY/2026-03-03.parquet": opt03,
        "options/NIFTY/2026-03-10.parquet": files["options"][0],
        "options/NIFTY/2026-03-17.parquet": files["options"][1],
    }
    wanted: list[str] = []

    def fake_download(url: str, target: Path, expected: int | None = None, **_: Any) -> bool:
        key = url.removeprefix(hf._RAW + "/")
        wanted.append(key)
        shutil.copy(sources[key], target)
        return True

    sizes = {
        date(2026, 3, 3): 1,
        date(2026, 3, 10): 1,
        date(2026, 3, 17): 1,
        date(2026, 3, 24): 1,
    }
    monkeypatch.setattr(hf, "list_expiry_files", lambda: sizes)
    monkeypatch.setattr(hf, "download", fake_download)
    out = tmp_path / "out"

    code = hf.main(["--months", "1", "--end", "2026-03-11", "--dir", str(out)])

    text = capsys.readouterr().out
    assert code == 0, text
    # window 11 Feb..11 Mar needs the 3 Mar, 10 Mar and 17 Mar files (the 24 Mar one is not needed)
    assert sorted(w for w in wanted if w.startswith("options")) == [
        "options/NIFTY/2026-03-03.parquet",
        "options/NIFTY/2026-03-10.parquet",
        "options/NIFTY/2026-03-17.parquet",
    ]
    assert "make import-csv" in text
    assert (out / "nifty_spot.csv").exists()
    assert (out / "nifty_options.csv.gz").exists()
    assert (out / "expiries.csv").read_text().startswith("expiry\n")


def test_the_command_reports_a_problem_instead_of_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom() -> dict[date, int]:
        msg = "the dataset lists no NIFTY option files (its layout may have changed)"
        raise hf.DatasetError(msg)

    monkeypatch.setattr(hf, "list_expiry_files", boom)
    assert hf.main(["--dir", str(tmp_path)]) == 1
    assert "Stopped: the dataset lists no NIFTY option files" in capsys.readouterr().err


def test_a_silly_month_count_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    assert hf.main(["--months", "0"]) == 2
    assert "--months" in capsys.readouterr().err


def _serve(
    monkeypatch: pytest.MonkeyPatch, sources: dict[str, Path], sizes: dict[date, int]
) -> None:
    def fake_download(url: str, target: Path, expected: int | None = None, **_: Any) -> bool:
        shutil.copy(sources[url.removeprefix(hf._RAW + "/")], target)
        return True

    monkeypatch.setattr(hf, "list_expiry_files", lambda: sizes)
    monkeypatch.setattr(hf, "download", fake_download)


def test_the_window_ends_where_the_option_data_ends_not_where_the_index_ends(
    files: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # the index runs to 13 Mar but the newest option file only has candles up to 12 Mar
    opt03 = files["dir"] / "2026-03-03.parquet"
    write_options(opt03, date(2026, 3, 3), weekdays(date(2026, 2, 24), date(2026, 3, 3)))
    short17 = files["dir"] / "short-2026-03-17.parquet"
    write_options(short17, date(2026, 3, 17), weekdays(date(2026, 3, 11), date(2026, 3, 12)))
    _serve(
        monkeypatch,
        {
            "index/NIFTY.parquet": files["index"],
            "options/NIFTY/2026-03-03.parquet": opt03,
            "options/NIFTY/2026-03-10.parquet": files["options"][0],
            "options/NIFTY/2026-03-17.parquet": short17,
        },
        {date(2026, 3, 3): 1, date(2026, 3, 10): 1, date(2026, 3, 17): 1},
    )
    out = tmp_path / "out"

    assert hf.main(["--months", "1", "--dir", str(out)]) == 0

    assert "only run to 2026-03-12" in capsys.readouterr().out
    last_spot_day = max(r["timestamp"][:10] for r in read_csv(out / "nifty_spot.csv"))
    assert last_spot_day == "2026-03-12"


def test_option_data_end_is_the_last_day_with_a_candle(files: dict[str, Any]) -> None:
    assert hf.option_data_end(files["options"]) == date(2026, 3, 17)
    assert hf.option_data_end(files["options"][:1]) == date(2026, 3, 10)


def test_missing_weeks_between_expiry_files_are_found() -> None:
    expiries = [date(2025, 10, 7), date(2025, 10, 14), date(2025, 10, 28), date(2025, 11, 4)]
    assert hf.expiry_gaps(expiries) == [(date(2025, 10, 14), date(2025, 10, 28))]


def test_a_holiday_shifted_expiry_is_not_a_gap() -> None:
    # Mar 2026: 24th -> 30th -> 7 Apr (8 days) are normal
    shifted = [date(2026, 3, 24), date(2026, 3, 30), date(2026, 4, 7)]
    assert hf.expiry_gaps(shifted) == []


def test_a_verified_missing_expiry_is_added_to_the_expiry_list(
    files: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opt03 = files["dir"] / "2026-03-03.parquet"
    write_options(opt03, date(2026, 3, 3), weekdays(date(2026, 2, 24), date(2026, 3, 3)))
    _serve(
        monkeypatch,
        {
            "index/NIFTY.parquet": files["index"],
            "options/NIFTY/2026-03-03.parquet": opt03,
            "options/NIFTY/2026-03-10.parquet": files["options"][0],
            "options/NIFTY/2026-03-17.parquet": files["options"][1],
        },
        {date(2026, 3, 3): 1, date(2026, 3, 10): 1, date(2026, 3, 17): 1},
    )
    monkeypatch.setattr(hf, "KNOWN_EXTRA_EXPIRIES", (date(2026, 3, 5), date(2030, 1, 1)))
    out = tmp_path / "out"

    assert hf.main(["--months", "1", "--end", "2026-03-11", "--dir", str(out)]) == 0

    listed = (out / "expiries.csv").read_text().split()[1:]
    assert listed == ["2026-03-03", "2026-03-05", "2026-03-10", "2026-03-17"]


def test_the_window_stops_before_a_stretch_with_no_expiry_files(
    files: dict[str, Any],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # the 24 Mar expiry has no file; the 31 Mar one covers 25-30 Mar, which are really days of the
    # missing expiry, so they must not be used
    index = tmp_path / "long_index.parquet"
    write_index(index, weekdays(date(2026, 2, 16), date(2026, 3, 30)))
    sources = {"index/NIFTY.parquet": index}
    for expiry, first_day in [
        (date(2026, 3, 3), date(2026, 2, 24)),
        (date(2026, 3, 10), date(2026, 3, 4)),
        (date(2026, 3, 17), date(2026, 3, 11)),
        (date(2026, 3, 31), date(2026, 3, 25)),
    ]:
        path = tmp_path / f"{expiry}.parquet"
        write_options(path, expiry, weekdays(first_day, min(expiry, date(2026, 3, 30))))
        sources[f"options/NIFTY/{expiry}.parquet"] = path
    sizes = {date(2026, 3, 3): 1, date(2026, 3, 10): 1, date(2026, 3, 17): 1, date(2026, 3, 31): 1}
    _serve(monkeypatch, sources, sizes)
    out = tmp_path / "out"

    assert hf.main(["--months", "1", "--dir", str(out)]) == 0

    text = capsys.readouterr().out
    assert "No expiry files between 2026-03-17 and 2026-03-31" in text
    assert max(r["timestamp"][:10] for r in read_csv(out / "nifty_spot.csv")) == "2026-03-17"
    assert (out / "expiries.csv").read_text().split()[1:][-1] == "2026-03-17"
