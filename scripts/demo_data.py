"""Build a throw-away database of SIMULATED NIFTY data, to try the app without Groww data access.

    make demo-data                       # about 4 months, takes a minute
    make demo-data ARGS="--days 30"
    make demo                            # run the app on it

These are not real prices (see ``app/history/synthetic.py``). The data goes through the real
downloader into a database whose name must contain "demo" (it is dropped and recreated on every
run), and is stamped as synthetic so the pages show a banner. Your real ``trading`` database is
never touched.
"""

import argparse
import logging
import sys
import time
from datetime import date, timedelta
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

from app.config import DEFAULT_DATABASE_URL
from app.db.connection import connect
from app.db.migrate import migrate
from app.history.backfill import Backfill, Throttle
from app.history.store import Store
from app.history.synthetic import DATA_SOURCE_KEY, SYNTHETIC_SOURCE, SyntheticSource
from app.strategy.engine import StrategyParams

TIMEFRAMES = (5, 15, 30)
DEFAULT_DEMO_URL = DEFAULT_DATABASE_URL.rsplit("/", 1)[0] + "/trading_demo"
WARMUP_BUFFER_DAYS = 40  # calendar days of extra spot before the first traded day


def database_name(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def check_is_demo_database(url: str) -> None:
    """Refuse to run against anything that could be real data."""
    name = database_name(url)
    if "demo" not in name.lower():
        msg = (
            f"Refusing to use database {name!r}: its name must contain 'demo', because it is "
            "dropped and recreated. Simulated prices must never mix with real data."
        )
        raise SystemExit(msg)


def recreate_database(url: str) -> None:
    name = database_name(url)
    admin = with_database(url, "postgres")
    with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--days", type=int, default=120, help="calendar days of history (default 120)"
    )
    parser.add_argument("--seed", type=int, default=7, help="random seed: same seed, same data")
    parser.add_argument(
        "--database-url", default=DEFAULT_DEMO_URL, help="must name a *demo* database"
    )
    parser.add_argument(
        "--timeframes", type=int, nargs="+", choices=TIMEFRAMES, default=list(TIMEFRAMES)
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not 10 <= args.days <= 800:
        sys.stderr.write("--days must be between 10 and 800\n")
        return 2
    check_is_demo_database(args.database_url)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")

    end = date.today() - timedelta(days=1)
    start = end - timedelta(days=args.days)
    sys.stdout.write(
        f"Creating simulated data for {start} to {end} in {database_name(args.database_url)}\n"
    )
    recreate_database(args.database_url)
    migrate(args.database_url)

    source = SyntheticSource(start - timedelta(days=WARMUP_BUFFER_DAYS), end, seed=args.seed)
    conn = connect(args.database_url)
    began = time.monotonic()
    try:
        store = Store(conn)
        store.set_meta(DATA_SOURCE_KEY, SYNTHETIC_SOURCE)  # before any data, so it is never missing
        backfill = Backfill(
            source,
            store,
            throttle=Throttle(interval=0),
            sleep=lambda _seconds: None,
            progress=None,
        )
        report = backfill.run(start, end, [StrategyParams(timeframe=tf) for tf in args.timeframes])
    finally:
        conn.close()

    for step, phase in report.phases.items():
        sys.stdout.write(
            f"  {step:10} downloaded {phase.fetched:5}  empty {phase.empty:5}  "
            f"errors {phase.errors}\n"
        )
    sys.stdout.write(f"Done in {time.monotonic() - began:.0f}s. Run it with: make demo\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
