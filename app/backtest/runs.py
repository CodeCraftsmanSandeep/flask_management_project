"""Computing and caching the *population* of simulated trades for one signal timeframe.

For each timeframe the engine is run once, for every mode, over all downloaded days with the widest
entry window. The result (trades, skipped candidates, per-day outcomes and the signal bars + EMAs
needed to draw charts) is stored in Postgres under a key derived from everything that influences it:

    cache key = hash(engine version, strategy parameters, cost schedule, downloaded data version)

Change any of those and the key changes, so a stale result can never be served; the old row is
pruned once a fresh one is stored. Within a process the latest population per timeframe is also
kept in memory, so browsing filters costs nothing.
"""

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import psycopg

from app.backtest import codec
from app.db.connection import session
from app.history.backfill import SPOT_SYMBOL, UNDERLYING, WARMUP_DAYS
from app.history.store import Store
from app.strategy.costs import DEFAULT_SCHEDULE, CostSchedule
from app.strategy.engine import (
    ENGINE_VERSION,
    DayOutcome,
    StrategyParams,
    analyse_spot,
    required_contracts,
    run_backtest,
)
from app.strategy.models import Mode, Skip, Trade

BarRow = list[Any]  # [iso start, open, high, low, close, ema_fast, ema_slow]


@dataclass(slots=True)
class Population:
    cache_key: str
    timeframe: int
    params: StrategyParams
    trades: list[Trade]
    skips: list[Skip]
    days: list[DayOutcome]
    bars: dict[date, list[BarRow]] = field(default_factory=dict)  # days that have trades
    first_day: date | None = None  # first traded day (after warm-up)
    last_day: date | None = None
    created_at: datetime | None = None
    from_cache: bool = False
    data_version: str = ""


def cache_key(params: StrategyParams, schedule: CostSchedule, data_version: str) -> str:
    text = "|".join([ENGINE_VERSION, params.fingerprint(), schedule.fingerprint(), data_version])
    return hashlib.sha256(text.encode()).hexdigest()[:24]


def default_params(timeframe: int) -> StrategyParams:
    """The strategy as specified: 9/21 EMAs, the given timeframe, everything else default."""
    return StrategyParams(timeframe=timeframe)


def trade_start(first_spot_day: date, last_spot_day: date) -> date:
    """First traded day: skip the warm-up unless the data is too short to afford one."""
    warmed = first_spot_day + timedelta(days=WARMUP_DAYS)
    return warmed if warmed < last_spot_day else first_spot_day


class RunCache:
    """Computes populations on demand, caching them in Postgres and in memory."""

    def __init__(
        self,
        database_url: str,
        schedule: CostSchedule = DEFAULT_SCHEDULE,
        params_for: Callable[[int], StrategyParams] = default_params,
    ) -> None:
        self._url = database_url
        self._schedule = schedule
        self._params_for = params_for
        self._lock = threading.Lock()
        self._memory: dict[int, Population] = {}

    @property
    def schedule(self) -> CostSchedule:
        return self._schedule

    def params(self, timeframe: int) -> StrategyParams:
        return self._params_for(timeframe)

    def population(self, timeframe: int) -> Population | None:
        """The cached population, computing it first if needed. ``None`` when no data exists."""
        with session(self._url) as conn:
            store = Store(conn)
            span = store.spot_range(SPOT_SYMBOL)
            if span is None:
                return None
            params = self._params_for(timeframe)
            data_version = store.data_version()
            key = cache_key(params, self._schedule, data_version)

            with self._lock:
                held = self._memory.get(timeframe)
                if held is not None and held.cache_key == key:
                    return held
                loaded = self._load(conn, key, params)
                if loaded is None:
                    loaded = self._compute(store, params, key, span)
                    loaded.data_version = data_version
                    self._save(conn, loaded)
                loaded.data_version = data_version
                self._memory[timeframe] = loaded
                return loaded

    # ------------------------------------------------------------------ compute

    def _compute(
        self, store: Store, params: StrategyParams, key: str, span: tuple[date, date]
    ) -> Population:
        first, last = span
        start = trade_start(first, last)
        spot = store.load_spot(SPOT_SYMBOL, first, last)
        expiries = store.load_expiries(UNDERLYING)
        analysis = analyse_spot(spot, params)
        required = required_contracts(analysis, expiries, params, tuple(Mode), start, last)
        book = store.load_option_book(required)
        result = run_backtest(
            spot,
            expiries,
            book,
            params,
            schedule=self._schedule,
            start=start,
            end=last,
            analysis=analysis,
        )
        traded_days = {t.trade_date for t in result.trades}
        bars: dict[date, list[BarRow]] = {}
        for bar in analysis.signal_bars:
            day = bar.start.date()
            if day in traded_days:
                bars.setdefault(day, []).append(
                    [
                        bar.start.isoformat(),
                        float(bar.open),
                        float(bar.high),
                        float(bar.low),
                        float(bar.close),
                        bar.ema_fast,
                        bar.ema_slow,
                    ]
                )
        return Population(
            cache_key=key,
            timeframe=params.timeframe,
            params=params,
            trades=result.trades,
            skips=result.skips,
            days=result.days,
            bars=bars,
            first_day=start,
            last_day=last,
            created_at=datetime.now(),
        )

    # -------------------------------------------------------------------- cache

    def _load(
        self, conn: psycopg.Connection, key: str, params: StrategyParams
    ) -> Population | None:
        row = conn.execute(
            "SELECT payload, created_at FROM backtest_runs WHERE cache_key = %s", (key,)
        ).fetchone()
        conn.commit()
        if row is None:
            return None
        payload, created_at = row
        return Population(
            cache_key=key,
            timeframe=params.timeframe,
            params=params,
            trades=[codec.decode_trade(t) for t in payload["trades"]],
            skips=[codec.decode_skip(s) for s in payload["skips"]],
            days=[codec.decode_day(d) for d in payload["days"]],
            bars={date.fromisoformat(d): rows for d, rows in payload["bars"].items()},
            first_day=date.fromisoformat(payload["first_day"]) if payload["first_day"] else None,
            last_day=date.fromisoformat(payload["last_day"]) if payload["last_day"] else None,
            created_at=created_at,
            from_cache=True,
        )

    def _save(self, conn: psycopg.Connection, population: Population) -> None:
        payload = {
            "trades": [codec.encode_trade(t) for t in population.trades],
            "skips": [codec.encode_skip(s) for s in population.skips],
            "days": [codec.encode_day(d) for d in population.days],
            "bars": {d.isoformat(): rows for d, rows in population.bars.items()},
            "first_day": population.first_day.isoformat() if population.first_day else None,
            "last_day": population.last_day.isoformat() if population.last_day else None,
        }
        params = population.params
        conn.execute(
            "INSERT INTO backtest_runs (cache_key, timeframe, params_fingerprint, engine_version, "
            "data_version, schedule_fingerprint, trade_count, payload) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (cache_key) DO NOTHING",
            (
                population.cache_key,
                population.timeframe,
                params.fingerprint(),
                ENGINE_VERSION,
                population.data_version,
                self._schedule.fingerprint(),
                len(population.trades),
                json.dumps(payload),
            ),
        )
        # Older results for the same parameters can never be asked for again.
        conn.execute(
            "DELETE FROM backtest_runs WHERE timeframe = %s AND params_fingerprint = %s "
            "AND cache_key <> %s",
            (population.timeframe, params.fingerprint(), population.cache_key),
        )
        conn.commit()

    def forget_memory(self) -> None:
        """Drop the in-process copies (tests use this to prove the Postgres cache works)."""
        with self._lock:
            self._memory.clear()
