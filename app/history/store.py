"""Reading and writing market data in Postgres.

The :class:`Store` is a thin, SQL-only layer: no business rules live here. Writes are idempotent
upserts (downloading the same day twice never duplicates or corrupts anything) and commit
immediately, so an interrupted download keeps everything fetched so far.
"""

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Literal

import psycopg

from app.strategy.models import Candle

FetchKind = Literal["spot", "option", "expiries", "contracts"]
FetchStatus = Literal["ok", "empty", "error"]

_CANDLE_COLUMNS = "ts, open, high, low, close, volume"


def _day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start 00:00, day-after-end 00:00) so both dates are fully included."""
    return datetime.combine(start, time.min), datetime.combine(end + timedelta(days=1), time.min)


def _candle(row: tuple[object, ...], *, with_oi: bool = False) -> Candle:
    ts, o, h, low, c, volume = row[:6]
    return Candle(
        start=ts,  # type: ignore[arg-type]
        open=o,  # type: ignore[arg-type]
        high=h,  # type: ignore[arg-type]
        low=low,  # type: ignore[arg-type]
        close=c,  # type: ignore[arg-type]
        volume=int(volume),  # type: ignore[call-overload]
        oi=row[6] if with_oi else None,  # type: ignore[arg-type]
    )


@dataclass(frozen=True, slots=True)
class StoredBook:
    """Option candles preloaded for the (symbol, day) pairs a backtest needs.

    Implements the engine's ``OptionBook`` protocol. A pair that was never loaded simply has no
    candles, which the engine reports as a skipped trade rather than guessing a price.
    """

    data: Mapping[tuple[str, date], Mapping[datetime, Candle]]

    def candles(self, symbol: str, day: date) -> Mapping[datetime, Candle]:
        return self.data.get((symbol, day), {})


class Store:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # ------------------------------------------------------------------- candles

    def save_spot(self, symbol: str, candles: Iterable[Candle]) -> int:
        return self._save("spot_candles", symbol, candles, with_oi=False)

    def save_option(self, symbol: str, candles: Iterable[Candle]) -> int:
        return self._save("option_candles", symbol, candles, with_oi=True)

    def save_option_many(self, rows: Iterable[tuple[str, Candle]]) -> int:
        """Upsert option candles of many contracts in one transaction (used by bulk imports)."""
        data = [(s, c.start, c.open, c.high, c.low, c.close, c.volume, c.oi) for s, c in rows]
        if data:
            with self._conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO option_candles (symbol, ts, open, high, low, close, volume, oi) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (symbol, ts) DO UPDATE SET open = EXCLUDED.open, "
                    "high = EXCLUDED.high, low = EXCLUDED.low, close = EXCLUDED.close, "
                    "volume = EXCLUDED.volume, oi = EXCLUDED.oi",
                    data,
                )
        self._conn.commit()
        return len(data)

    def _save(self, table: str, symbol: str, candles: Iterable[Candle], *, with_oi: bool) -> int:
        rows = [
            (symbol, c.start, c.open, c.high, c.low, c.close, c.volume)
            + ((c.oi,) if with_oi else ())
            for c in candles
        ]
        if rows:
            columns = "symbol, ts, open, high, low, close, volume" + (", oi" if with_oi else "")
            marks = ", ".join(["%s"] * (8 if with_oi else 7))
            updates = "open, high, low, close, volume" + (", oi" if with_oi else "")
            assignments = ", ".join(f"{c} = EXCLUDED.{c}" for c in updates.split(", "))
            sql = (
                f"INSERT INTO {table} ({columns}) VALUES ({marks}) "  # noqa: S608 - fixed names
                f"ON CONFLICT (symbol, ts) DO UPDATE SET {assignments}"
            )
            with self._conn.cursor() as cur:
                cur.executemany(sql, rows)
        self._conn.commit()
        return len(rows)

    def load_spot(self, symbol: str, start: date, end: date) -> list[Candle]:
        """All stored 1-minute candles of *symbol* on dates start..end inclusive, oldest first."""
        lo, hi = _day_bounds(start, end)
        rows = self._conn.execute(
            f"SELECT {_CANDLE_COLUMNS} FROM spot_candles "  # noqa: S608
            "WHERE symbol = %s AND ts >= %s AND ts < %s ORDER BY ts",
            (symbol, lo, hi),
        ).fetchall()
        self._conn.commit()
        return [_candle(row) for row in rows]

    def load_option_book(self, required: Mapping[date, Collection[str]]) -> StoredBook:
        """Load exactly the (symbol, day) pairs in *required* (one query per day)."""
        data: dict[tuple[str, date], dict[datetime, Candle]] = {}
        for day, symbols in required.items():
            if not symbols:
                continue
            lo, hi = _day_bounds(day, day)
            rows = self._conn.execute(
                f"SELECT symbol, {_CANDLE_COLUMNS}, oi FROM option_candles "  # noqa: S608
                "WHERE symbol = ANY(%s) AND ts >= %s AND ts < %s ORDER BY ts",
                (sorted(symbols), lo, hi),
            ).fetchall()
            for symbol, *rest in rows:
                data.setdefault((symbol, day), {})[rest[0]] = _candle(tuple(rest), with_oi=True)
        self._conn.commit()
        return StoredBook(data)

    def spot_range(self, symbol: str) -> tuple[date, date] | None:
        """First and last day with stored spot candles, or None when there are none."""
        row = self._conn.execute(
            "SELECT min(ts)::date, max(ts)::date FROM spot_candles WHERE symbol = %s", (symbol,)
        ).fetchone()
        self._conn.commit()
        return (row[0], row[1]) if row and row[0] is not None else None

    def data_version(self) -> str:
        """Changes whenever anything is downloaded or imported (every write logs a fetch)."""
        row = self._conn.execute(
            "SELECT count(*), coalesce(max(fetched_at)::text, '') FROM fetch_log"
        ).fetchone()
        self._conn.commit()
        return f"{row[0]}:{row[1]}" if row else "0:"

    def count_candles(self, table: Literal["spot_candles", "option_candles"]) -> int:
        row = self._conn.execute(f"SELECT count(*) FROM {table}").fetchone()  # noqa: S608
        self._conn.commit()
        return int(row[0]) if row else 0

    # ------------------------------------------------------- expiries and contracts

    def save_expiries(self, underlying: str, expiries: Iterable[date]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO expiries (underlying, expiry) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                [(underlying, e) for e in expiries],
            )
        self._conn.commit()

    def load_expiries(self, underlying: str) -> list[date]:
        rows = self._conn.execute(
            "SELECT expiry FROM expiries WHERE underlying = %s ORDER BY expiry", (underlying,)
        ).fetchall()
        self._conn.commit()
        return [r[0] for r in rows]

    def save_contracts(self, underlying: str, expiry: date, symbols: Iterable[str]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO contracts (symbol, underlying, expiry) VALUES (%s, %s, %s) "
                "ON CONFLICT (symbol) DO NOTHING",
                [(s, underlying, expiry) for s in symbols],
            )
        self._conn.commit()

    def load_contracts(self, underlying: str, expiry: date) -> set[str]:
        rows = self._conn.execute(
            "SELECT symbol FROM contracts WHERE underlying = %s AND expiry = %s",
            (underlying, expiry),
        ).fetchall()
        self._conn.commit()
        return {r[0] for r in rows}

    # ---------------------------------------------------------------------- meta

    def get_meta(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
        self._conn.commit()
        return None if row is None else str(row[0])

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (key, value),
        )
        self._conn.commit()

    # ----------------------------------------------------------------- fetch log

    def log_fetch(
        self,
        kind: FetchKind,
        key: str,
        day: date,
        status: FetchStatus,
        *,
        rows: int = 0,
        detail: str | None = None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO fetch_log (kind, key, day, status, rows, detail) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (kind, key, day) DO UPDATE SET status = EXCLUDED.status, "
            "rows = EXCLUDED.rows, detail = EXCLUDED.detail, fetched_at = now()",
            (kind, key, day, status, rows, detail),
        )
        self._conn.commit()

    def log_fetch_many(
        self, entries: Iterable[tuple[FetchKind, str, date, FetchStatus, int, str | None]]
    ) -> int:
        """:meth:`log_fetch` for many ``(kind, key, day, status, rows, detail)`` at once."""
        data = list(entries)
        if data:
            with self._conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO fetch_log (kind, key, day, status, rows, detail) "
                    "VALUES (%s, %s, %s, %s, %s, %s) "
                    "ON CONFLICT (kind, key, day) DO UPDATE SET status = EXCLUDED.status, "
                    "rows = EXCLUDED.rows, detail = EXCLUDED.detail, fetched_at = now()",
                    data,
                )
        self._conn.commit()
        return len(data)

    def fetch_status(self, kind: FetchKind, key: str, start: date, end: date) -> dict[date, str]:
        """``{day: status}`` for the days in start..end that have been attempted."""
        rows = self._conn.execute(
            "SELECT day, status FROM fetch_log WHERE kind = %s AND key = %s "
            "AND day BETWEEN %s AND %s",
            (kind, key, start, end),
        ).fetchall()
        self._conn.commit()
        return {r[0]: r[1] for r in rows}

    def fetch_summary(self) -> list[tuple[str, str, int, int]]:
        """``(kind, status, days, rows)`` totals for the data page."""
        rows = self._conn.execute(
            "SELECT kind, status, count(*), coalesce(sum(rows), 0) FROM fetch_log "
            "GROUP BY kind, status ORDER BY kind, status"
        ).fetchall()
        self._conn.commit()
        return [(r[0], r[1], int(r[2]), int(r[3])) for r in rows]

    def import_labels(self) -> list[str]:
        """The ``--label`` of every CSV import that wrote to the fetch log, sorted."""
        rows = self._conn.execute(
            "SELECT DISTINCT substr(detail, 6) FROM fetch_log WHERE detail LIKE 'csv: %' ORDER BY 1"
        ).fetchall()
        self._conn.commit()
        return [str(r[0]) for r in rows]

    def failures(self, limit: int = 50) -> list[tuple[str, str, date, str]]:
        """Most recent failed fetches: ``(kind, key, day, detail)``."""
        rows = self._conn.execute(
            "SELECT kind, key, day, coalesce(detail, '') FROM fetch_log "
            "WHERE status = 'error' ORDER BY fetched_at DESC LIMIT %s",
            (limit,),
        ).fetchall()
        self._conn.commit()
        return [(r[0], r[1], r[2], r[3]) for r in rows]
