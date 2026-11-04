"""Fetch free NIFTY 1-minute data from Hugging Face and turn it into files `make import-csv` reads.

    make hf-data                       # the latest 12 months the dataset has
    make hf-data ARGS="--months 6"
    python -m scripts.hf_dataset --help

Source: https://huggingface.co/datasets/thetrademarkk/india-index-options-1m (a community
dataset, "educational use only, verify against official exchange data"). It holds NIFTY 1-minute
index candles and, for every weekly expiry, 1-minute candles of each option contract for roughly
the last 7 trading days before that expiry. Far or illiquid strikes are often sparse.

Only the files a window needs are downloaded (the index file plus the weekly expiry files from the
window's first day up to the first expiry on or after its last day). Nothing is sent to Hugging
Face except ordinary file downloads; no account or key is involved.

Output, in the data folder (default ``data/huggingface``, ignored by git):
    nifty_spot.csv, nifty_options.csv.gz, expiries.csv
and the exact ``make import-csv`` command to load them is printed at the end.
"""

import argparse
import json
import re
import sys
import time
import urllib.request
from collections.abc import Callable, Sequence
from datetime import date, timedelta
from itertools import pairwise
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.csv as pacsv
import pyarrow.parquet as pq

from app.history.jobs import months_back
from app.strategy.options import pick_expiry

REPO = "thetrademarkk/india-index-options-1m"
_API = f"https://huggingface.co/api/datasets/{REPO}/tree/main"
_RAW = f"https://huggingface.co/datasets/{REPO}/resolve/main"
WARMUP_DAYS = 14  # calendar days of index data before the window, for the EMAs to settle
_OPEN_MINUTE = 9 * 60 + 15
_CLOSE_MINUTE = 15 * 60 + 30  # the dataset has a closing print stamped 15:30; the importer drops it
_EXPIRY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.parquet$")
_TIMEOUT_SECONDS = 60
_RETRY_DELAYS = (2.0, 5.0, 15.0)
_SPOT_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")
_OPTION_COLUMNS = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "open_interest",
    "strike",
    "option_type",
    "expiry",
)
_PRICES = ("open", "high", "low", "close")
_MAX_WEEKLY_GAP_DAYS = 8  # Tuesday to Tuesday is 7; a holiday moving one expiry earlier makes 8
# Expiries that really existed but have no file in the dataset. Without them the engine would pick
# the next listed expiry (a different contract, wrongly) instead of skipping the day for lack of
# data. Source: the independent Kaggle list "NIFTY OPTION CHAIN 3-Oct-24 to 24-Mar-26", which has
# 2025-10-20 (the weekly expiry moved to Monday around Diwali); this dataset jumps 14-Oct -> 28-Oct.
KNOWN_EXTRA_EXPIRIES = (date(2025, 10, 20),)


class DatasetError(Exception):
    """The dataset is not what this script expects (the message says what differs)."""


# ----------------------------------------------------------------------------- selection


def select_expiries(expiries: Sequence[date], first: date, last: date) -> list[date]:
    """The weekly expiries whose option files cover trades on first..last.

    A trade on a day uses the nearest expiry on or after that day, so the files needed are those
    from the first expiry on or after *first* up to the first expiry on or after *last*.
    """
    ordered = sorted(expiries)
    upper = pick_expiry(last, ordered)
    if upper is None:
        msg = f"the dataset has no expiry on or after {last}"
        raise DatasetError(msg)
    return [e for e in ordered if first <= e <= upper]


# ------------------------------------------------------------------------------ conversion


def _local(table: pa.Table) -> pa.Array:
    """The timestamp column as IST wall-clock, whole seconds (so text carries no fraction)."""
    column = table["timestamp"]
    if not pa.types.is_timestamp(column.type) or column.type.tz is None:
        msg = "the 'timestamp' column is not a time zone aware timestamp"
        raise DatasetError(msg)
    return pc.cast(column, pa.timestamp("s", tz=column.type.tz)).combine_chunks()


def _require(table: pa.Table, columns: Sequence[str], name: str) -> None:
    missing = [c for c in columns if c not in table.column_names]
    if missing:
        msg = f"{name} lacks column(s) {', '.join(missing)} (has: {', '.join(table.column_names)})"
        raise DatasetError(msg)


def _in_session(local: pa.Array) -> pa.Array:
    minute = pc.add(pc.multiply(pc.hour(local), 60), pc.minute(local))
    return pc.and_(pc.greater_equal(minute, _OPEN_MINUTE), pc.less_equal(minute, _CLOSE_MINUTE))


def _day(local: pa.Array) -> pa.Array:
    return pc.strftime(local, format="%Y-%m-%d")


def _stamp(local: pa.Array) -> pa.Array:
    return pc.strftime(local, format="%Y-%m-%d %H:%M:%S")


def index_days(index_path: Path) -> list[date]:
    """Every day that has index candles, oldest first."""
    table = pq.read_table(index_path, columns=["timestamp"])
    days = pc.unique(_day(_local(table))).to_pylist()
    return sorted(date.fromisoformat(d) for d in days)


def convert_spot(index_path: Path, out_path: Path, first: date, last: date) -> int:
    """Write the regular-session index candles of first..last as CSV; returns the row count."""
    table = pq.read_table(index_path)
    _require(table, _SPOT_COLUMNS, index_path.name)
    local = _local(table)
    day = _day(local)
    keep = pc.and_(
        pc.and_(
            pc.greater_equal(day, first.isoformat()),
            pc.less_equal(day, last.isoformat()),
        ),
        _in_session(local),
    )
    out = pa.table(
        {
            "timestamp": _stamp(local),
            **{name: table[name].cast(pa.float64()) for name in _PRICES},
            "volume": table["volume"].cast(pa.int64()),
        }
    ).filter(keep)
    pacsv.write_csv(out, out_path)
    return int(out.num_rows)


def convert_options(option_paths: Sequence[Path], out_path: Path, first: date) -> int:
    """Write the option candles from *first* on (all given files) as one gzipped CSV."""
    rows = 0
    writer: pacsv.CSVWriter | None = None
    with pa.CompressedOutputStream(str(out_path), "gzip") as sink:
        for path in option_paths:
            table = pq.read_table(path)
            _require(table, _OPTION_COLUMNS, path.name)
            local = _local(table)
            keep = pc.and_(pc.greater_equal(_day(local), first.isoformat()), _in_session(local))
            out = pa.table(
                {
                    "expiry": table["expiry"].cast(pa.string()),
                    "strike": table["strike"].cast(pa.int64()),
                    "option_type": table["option_type"].cast(pa.string()),
                    "timestamp": _stamp(local),
                    **{name: table[name].cast(pa.float64()) for name in _PRICES},
                    "volume": table["volume"].cast(pa.int64()),
                    "oi": table["open_interest"].cast(pa.int64()),
                }
            ).filter(keep)
            if writer is None:
                writer = pacsv.CSVWriter(sink, out.schema)
            writer.write_table(out)
            rows += int(out.num_rows)
        if writer is not None:
            writer.close()
    return rows


def option_data_end(option_paths: Sequence[Path]) -> date:
    """The last day on which any of these option files has a candle."""
    latest: date | None = None
    for path in option_paths:
        table = pq.read_table(path, columns=["timestamp"])
        if table.num_rows == 0:
            continue
        newest = pc.max(_local(table)).as_py()
        day = newest.date() if newest is not None else None
        if day is not None and (latest is None or day > latest):
            latest = day
    if latest is None:
        msg = "the option files hold no candles"
        raise DatasetError(msg)
    return latest


def expiry_gaps(expiries: Sequence[date]) -> list[tuple[date, date]]:
    """Consecutive expiries further apart than a week plus one holiday shift: files are missing."""
    ordered = sorted(expiries)
    return [(a, b) for a, b in pairwise(ordered) if (b - a).days > _MAX_WEEKLY_GAP_DAYS]


def write_expiries(expiries: Sequence[date], out_path: Path) -> None:
    out_path.write_text("expiry\n" + "".join(f"{e.isoformat()}\n" for e in expiries))


# -------------------------------------------------------------------------------- network


def _open(url: str) -> object:
    if not url.startswith("https://huggingface.co/"):
        msg = f"refusing to open {url}"
        raise DatasetError(msg)
    return urllib.request.urlopen(url, timeout=_TIMEOUT_SECONDS)  # noqa: S310 - https only, above


def _get_json(url: str) -> list[dict[str, object]]:
    with _open(url) as response:  # type: ignore[attr-defined]
        data = json.load(response)
    if not isinstance(data, list):
        msg = "unexpected answer from the Hugging Face file listing"
        raise DatasetError(msg)
    return data


def list_expiry_files() -> dict[date, int]:
    """``{expiry: size in bytes}`` of every weekly NIFTY option file in the dataset."""
    found: dict[date, int] = {}
    for item in _get_json(f"{_API}/options/NIFTY"):
        match = _EXPIRY_FILE.match(Path(str(item.get("path", ""))).name)
        if item.get("type") == "file" and match:
            found[date.fromisoformat(match.group(1))] = int(item.get("size", 0))  # type: ignore[call-overload]
    if not found:
        msg = "the dataset lists no NIFTY option files (its layout may have changed)"
        raise DatasetError(msg)
    return found


def download(
    url: str,
    target: Path,
    expected: int | None = None,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Download *url* to *target* unless a complete copy is there. Returns True if it fetched."""
    if target.exists() and (expected is None or target.stat().st_size == expected):
        return False
    part = target.with_suffix(target.suffix + ".part")
    for attempt in range(len(_RETRY_DELAYS) + 1):
        try:
            with _open(url) as response, part.open("wb") as handle:  # type: ignore[attr-defined]
                while chunk := response.read(1 << 20):
                    handle.write(chunk)
            if expected is not None and part.stat().st_size != expected:
                msg = f"{target.name}: got {part.stat().st_size} bytes, expected {expected}"
                raise DatasetError(msg)
            part.replace(target)
            return True
        except (OSError, DatasetError) as exc:
            part.unlink(missing_ok=True)
            if attempt == len(_RETRY_DELAYS):
                msg = f"could not download {target.name}: {exc}"
                raise DatasetError(msg) from exc
            sleep(_RETRY_DELAYS[attempt])
    raise AssertionError  # pragma: no cover - the loop always returns or raises


# ------------------------------------------------------------------------------------ main


def fetch_options(
    folder: Path,
    expiries: Sequence[date],
    listing: dict[date, int],
    say: Callable[[str], object],
) -> list[Path]:
    """Download (when missing) the option file of each expiry; returns their paths in order."""
    folder.mkdir(exist_ok=True)
    paths: list[Path] = []
    for number, expiry in enumerate(expiries, start=1):
        target = folder / f"{expiry.isoformat()}.parquet"
        url = f"{_RAW}/options/NIFTY/{expiry.isoformat()}.parquet"
        fetched = download(url, target, listing[expiry])
        paths.append(target)
        if fetched or number % 10 == 0 or number == len(expiries):
            say(f"  option files {number}/{len(expiries)} (latest: {expiry})\n")
    return paths


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--months", type=int, default=12, help="how many months (default 12)")
    parser.add_argument(
        "--end",
        type=date.fromisoformat,
        default=None,
        help="last day, YYYY-MM-DD (default: the last day the index file has)",
    )
    parser.add_argument("--dir", type=Path, default=Path("data/huggingface"), help="data folder")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 1 <= args.months <= 60:
        sys.stderr.write("--months must be between 1 and 60\n")
        return 2
    folder: Path = args.dir
    folder.mkdir(parents=True, exist_ok=True)
    say = sys.stdout.write
    try:
        say("Finding the files...\n")
        listing = list_expiry_files()
        index_path = folder / "NIFTY_index.parquet"
        download(f"{_RAW}/index/NIFTY.parquet", index_path)
        days = index_days(index_path)
        last = min(args.end, days[-1]) if args.end else days[-1]
        first = months_back(last, args.months)
        say(f"Index data: {days[0]} to {days[-1]}. Window: {first} to {last}.\n")

        while True:
            expiries = select_expiries(list(listing), first, last)
            paths = fetch_options(folder / "options", expiries, listing, say)
            listed = sorted(
                {*expiries, *(e for e in KNOWN_EXTRA_EXPIRIES if first <= e <= expiries[-1])}
            )
            gaps = expiry_gaps(listed)
            end = option_data_end(paths[-2:])
            if end < last:
                say(f"The option files only run to {end}: ending the window there.\n")
                new_last = end
            elif gaps and gaps[-1][1] == listed[-1]:
                # No file for the weeks before the newest one: its contracts would be the wrong
                # (later) expiry for those days, so stop at the last day the older file covers.
                before, after = gaps[-1]
                say(f"No expiry files between {before} and {after}: ending the window before it.\n")
                covering = dict(zip(expiries, paths, strict=True)).get(before)
                new_last = option_data_end([covering]) if covering else before
            else:
                break
            if new_last >= last:
                break
            last = new_last
            first = months_back(last, args.months)
            say(f"Window: {first} to {last}.\n")
        for before, after in gaps:
            say(
                f"WARNING: the dataset has no expiry file between {before} and {after}. "
                "Signals on those days will be skipped and listed.\n"
            )

        say("Converting...\n")
        spot_rows = convert_spot(
            index_path, folder / "nifty_spot.csv", first - timedelta(days=WARMUP_DAYS), last
        )
        option_rows = convert_options(paths, folder / "nifty_options.csv.gz", first)
        write_expiries(listed, folder / "expiries.csv")
    except (DatasetError, OSError, pa.ArrowException) as exc:
        sys.stderr.write(f"\nStopped: {exc}\n")
        return 1

    say(f"\nWrote {spot_rows} index rows, {option_rows} option rows, {len(listed)} expiries.\n")
    say("Check, then import (the first run is a dry run that writes nothing):\n")
    files = f"--spot {folder}/nifty_spot.csv --options {folder}/nifty_options.csv.gz"
    files += f" --expiries {folder}/expiries.csv --label huggingface"
    say(f'  make import-csv ARGS="{files} --dry-run"\n')
    say(f'  make import-csv ARGS="{files}"\n')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
