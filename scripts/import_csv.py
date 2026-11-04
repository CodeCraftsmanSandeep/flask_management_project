"""Load NIFTY 1-minute candles from CSV files (any data provider) into the backtest database.

    make import-csv ARGS="--spot nifty.csv --options options.csv.gz --expiries expiries.csv"
    make import-csv ARGS="--spot nifty.csv --options opts.csv --derive-expiries --dry-run"
    python -m scripts.import_csv --help

Always try --dry-run first: it reads and checks every file, prints what it found and what looks
wrong, and writes nothing. A real run validates everything before it writes anything, and is safe
to repeat (rows are upserted). Offline: no network, no Groww login.

The file layout, accepted column names and time formats are described in
``app/history/csv_import.py`` and in the readme.
"""

import argparse
import logging
import sys
from pathlib import Path

import psycopg

from app.config import Config
from app.db.connection import connect
from app.db.migrate import migrate
from app.errors import TradingError
from app.history.csv_import import CsvImporter, ImportReport
from app.history.store import Store

_DELIMITERS = {"comma": ",", "tab": "\t", "semicolon": ";", "pipe": "|"}
_MAX_SHOWN = 15


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--spot", type=Path, nargs="*", default=[], help="NIFTY 50 1-minute file(s)"
    )
    parser.add_argument(
        "--options",
        type=Path,
        nargs="*",
        default=[],
        help="option 1-minute file(s) (.csv or .csv.gz)",
    )
    parser.add_argument(
        "--expiries", type=Path, default=None, help="file with one expiry date per line"
    )
    parser.add_argument(
        "--derive-expiries",
        action="store_true",
        help=(
            "take the expiry dates from the option files\n"
            "(only right if they hold every weekly expiry)"
        ),
    )
    parser.add_argument(
        "--timestamps",
        choices=("auto", "start", "close"),
        default="auto",
        help=(
            "does a stamp mark the start or the end of its minute?\n"
            "(default: detect from the index)"
        ),
    )
    parser.add_argument(
        "--label", default="csv", help="a name for this data, kept in the download log"
    )
    parser.add_argument("--delimiter", choices=sorted(_DELIMITERS), default="comma")
    parser.add_argument(
        "--skip-bad-rows",
        action="store_true",
        help="skip unreadable rows (and report them) instead of stopping at the first one",
    )
    parser.add_argument("--dry-run", action="store_true", help="check the files, write nothing")
    parser.add_argument(
        "--database-url", default=None, help="default: DATABASE_URL or the local trading DB"
    )
    return parser.parse_args(argv)


def _say(text: str) -> None:
    sys.stdout.write(f"  {text}\n")


def _show(report: ImportReport) -> None:
    out = sys.stdout.write
    out("\nDry run: nothing was written.\n" if report.dry_run else "\nImported.\n")
    out(f"  timestamps mark the {report.convention} of each minute\n")
    if report.spot_rows:
        out(
            f"  index    {report.spot_rows} candles, {report.spot_days} days "
            f"({report.spot_first} to {report.spot_last}), "
            f"{report.incomplete_spot_days} incomplete\n"
        )
    if report.option_rows:
        pairs = f", {report.option_day_pairs} contract-days" if report.option_day_pairs else ""
        out(
            f"  options  {report.option_rows} candles, {report.option_contracts} contracts{pairs}\n"
        )
    if report.expiries:
        out(f"  expiries {report.expiries} dates\n")
    for title, items in (("note", report.notes), ("WARNING", report.warnings)):
        for item in items[:_MAX_SHOWN]:
            out(f"  {title}: {item}\n")
        if len(items) > _MAX_SHOWN:
            out(f"  ... and {len(items) - _MAX_SHOWN} more {title.lower()}s\n")
    if report.dry_run:
        out("Looks right? Run the same command without --dry-run.\n")
    else:
        out("Open the Data page or `make run` to use it. Include ~2 weeks before the first day\n")
        out("you want to test: the EMAs need them to settle.\n")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    url = args.database_url or Config.from_env().database_url
    try:
        migrate(url)
        conn = connect(url)
    except psycopg.OperationalError as exc:
        reason = str(exc).strip().splitlines()[-1] if str(exc).strip() else "connection failed"
        sys.stderr.write(
            f"\nNot imported: cannot reach the database ({reason}).\n"
            "Start it with `make db-start` (a non-default database must be created first).\n"
        )
        return 1
    try:
        importer = CsvImporter(
            Store(conn),
            label=args.label,
            delimiter=_DELIMITERS[args.delimiter],
            lenient=args.skip_bad_rows,
            progress=_say,
        )
        report = importer.run(
            spot_files=args.spot,
            option_files=args.options,
            expiries_file=args.expiries,
            derive_expiries=args.derive_expiries,
            timestamps=args.timestamps,
            dry_run=args.dry_run,
        )
    except TradingError as exc:
        sys.stderr.write(f"\nNot imported: {exc.message}\n")
        return 1
    finally:
        conn.close()
    _show(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
