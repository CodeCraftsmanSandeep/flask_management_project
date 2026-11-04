"""Download the last N months of NIFTY spot + the option candles the backtest needs.

    make backfill                      # last 12 months, timeframes 5/15/30, all three modes
    make backfill ARGS="--months 1"    # a quick trial first
    python -m scripts.backfill --help

Read-only: only Groww's market-data endpoints are called (no orders, no account changes).
Safe to stop with Ctrl-C and to run again: finished work is kept and skipped next time.
"""

import argparse
import logging
import signal
import sys
from datetime import date, timedelta
from types import FrameType

from app import build_service
from app.config import Config
from app.db.connection import connect
from app.db.migrate import migrate
from app.errors import TradingError
from app.history.backfill import Backfill, Progress
from app.history.jobs import months_back
from app.history.store import Store
from app.strategy.engine import StrategyParams

TIMEFRAMES = (5, 15, 30)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--months", type=int, default=12, help="how many months back (default 12)")
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="last day, YYYY-MM-DD")
    parser.add_argument(
        "--timeframes",
        type=int,
        nargs="+",
        choices=TIMEFRAMES,
        default=list(TIMEFRAMES),
        help="signal timeframes whose option contracts to download (default: 5 15 30)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.months < 1:
        sys.stderr.write("--months must be at least 1\n")
        return 2
    end = args.end or date.today() - timedelta(days=1)
    start = months_back(end, args.months)

    config = Config.from_env()
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    migrate(config.database_url)

    service = build_service(config)
    try:
        service.connect()
    except TradingError as exc:
        sys.stderr.write(f"Could not log in to Groww: {exc.message}\n")
        return 1

    stop = {"requested": False}

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        stop["requested"] = True
        sys.stdout.write("\nStopping after the current request (finished work is kept)...\n")

    signal.signal(signal.SIGINT, request_stop)

    last = {"phase": ""}

    def show(progress: Progress) -> None:
        if progress.phase != last["phase"]:
            last["phase"] = progress.phase
            sys.stdout.write(f"\n{progress.phase}: {progress.total} item(s)\n")
        if progress.done % 25 == 0:
            sys.stdout.write(f"  {progress.done}/{progress.total}  {progress.message}\n")
            sys.stdout.flush()

    params = [StrategyParams(timeframe=tf) for tf in args.timeframes]
    conn = connect(config.database_url)
    try:
        backfill = Backfill(
            service, Store(conn), progress=show, should_stop=lambda: stop["requested"]
        )
        sys.stdout.write(f"Downloading NIFTY data for {start} to {end}\n")
        report = backfill.run(start, end, params)
    except TradingError as exc:
        sys.stderr.write(f"\nStopped: {exc.message}\n")
        return 1
    finally:
        conn.close()

    sys.stdout.write("\nDone.\n" if not report.stopped else "\nStopped early.\n")
    for name, phase in report.phases.items():
        sys.stdout.write(
            f"  {name:10} downloaded {phase.fetched:5}  empty {phase.empty:5}  "
            f"already had {phase.cached:5}  errors {phase.errors}\n"
        )
    for problem in report.problems[:20]:
        sys.stdout.write(f"  note: {problem}\n")
    if len(report.problems) > 20:
        sys.stdout.write(f"  ... and {len(report.problems) - 20} more notes\n")
    return 0 if not report.stopped else 130


if __name__ == "__main__":
    raise SystemExit(main())
