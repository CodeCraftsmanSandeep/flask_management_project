"""What the web pages ask for: filtered trades and KPIs, trade chart data, skips, data status."""

from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any

from app.backtest.present import (
    RULES,
    equity_points,
    metrics_json,
    trade_card,
    trade_row,
)
from app.backtest.query import (
    CUTOFF_OPTIONS,
    START_OPTIONS,
    TIMEFRAMES,
    Filters,
    select,
)
from app.backtest.runs import Population, RunCache, default_params
from app.backtest.sources import describe_source
from app.db.connection import session
from app.errors import NoDataError, ResultsChangedError
from app.history.backfill import SPOT_SYMBOL, UNDERLYING
from app.history.store import Store
from app.history.synthetic import DATA_SOURCE_KEY, SYNTHETIC_SOURCE
from app.strategy.costs import DEFAULT_SCHEDULE, CostSchedule
from app.strategy.engine import ENGINE_VERSION, StrategyParams
from app.strategy.metrics import compute_metrics
from app.strategy.models import MODE_LABELS, Mode

NO_DATA_MESSAGE = (
    "No NIFTY data has been downloaded yet. Open the Data page to download it "
    "(or run `make backfill`)."
)


class BacktestService:
    def __init__(
        self,
        database_url: str,
        schedule: CostSchedule = DEFAULT_SCHEDULE,
        params_for: Callable[[int], StrategyParams] = default_params,
    ) -> None:
        self._url = database_url
        self._schedule = schedule
        self._params_for = params_for
        self.runs = RunCache(database_url, schedule, params_for)

    # ------------------------------------------------------------------- helpers

    def _population(self, timeframe: int) -> Population:
        population = self.runs.population(timeframe)
        if population is None:
            raise NoDataError(NO_DATA_MESSAGE)
        return population

    # --------------------------------------------------------------------- meta

    def meta(self) -> dict[str, Any]:
        """Static options for the filter bar plus what data the results are based on."""
        with session(self._url) as conn:
            store = Store(conn)
            span = store.spot_range(SPOT_SYMBOL)
            source = store.get_meta(DATA_SOURCE_KEY)
            labels = store.import_labels()
        return {
            "has_data": span is not None,
            "synthetic": source == SYNTHETIC_SOURCE,
            "data_source": describe_source(source, labels) if span is not None else None,
            "data_range": None if span is None else [span[0].isoformat(), span[1].isoformat()],
            "timeframes": list(TIMEFRAMES),
            "modes": [{"value": m.value, "label": MODE_LABELS[m]} for m in Mode],
            "starts": [t.strftime("%H:%M") for t in START_OPTIONS],
            "cutoffs": [t.strftime("%H:%M") for t in CUTOFF_OPTIONS],
            "outcomes": ["all", "win", "loss", "tie"],
            "engine_version": ENGINE_VERSION,
            "strategy": self._strategy_summary(),
            "cost_schedule": [
                {
                    "effective_from": r.effective_from.isoformat(),
                    "stt_sell_pct": float(r.stt_sell_pct),
                    "exchange_pct": float(r.exchange_pct),
                    "stamp_buy_pct": float(r.stamp_buy_pct),
                    "verified": r.verified,
                    "source": r.source,
                }
                for r in self._schedule.rows
            ],
            "rules": RULES,
        }

    def _strategy_summary(self) -> dict[str, Any]:
        p = self._params_for(TIMEFRAMES[0])
        return {
            "fast": p.fast,
            "slow": p.slow,
            "lot_size": p.lot_size,
            "lots": p.lots,
            "square_off": p.square_off.strftime("%H:%M"),
            "slippage_pct": float(p.slippage_pct * 100),
            "slippage_points": float(p.slippage_points),
        }

    # ------------------------------------------------------------------- trades

    def trades(self, filters: Filters) -> dict[str, Any]:
        population = self._population(filters.timeframe)
        chosen = select(population.trades, filters)
        every_mode = select(population.trades, filters, ignore_mode=True)
        chosen_trades = [s.trade for s in chosen]
        compare = {
            mode.value: metrics_json(
                compute_metrics([s.trade for s in every_mode if s.trade.mode is mode])
            )
            for mode in Mode
        }
        return {
            "run": population.cache_key,
            "timeframe": population.timeframe,
            "range": {
                "first": population.first_day.isoformat() if population.first_day else None,
                "last": population.last_day.isoformat() if population.last_day else None,
            },
            "computed": {
                "from_cache": population.from_cache,
                "created_at": population.created_at.isoformat() if population.created_at else None,
            },
            "kpis": metrics_json(compute_metrics(chosen_trades)),
            "compare": compare,
            "equity": equity_points(chosen_trades),
            "trades": [trade_row(s) for s in chosen],
        }

    def trade_detail(
        self, timeframe: int, index: int, run: str | None, filters: Filters
    ) -> dict[str, Any]:
        """One trade: metadata, cost card, signal bars with EMAs, and the option's own candles."""
        population = self._population(timeframe)
        if run is not None and run != population.cache_key:
            msg = "The results were recomputed since this list was loaded. Reload the page."
            raise ResultsChangedError(msg)
        if not 0 <= index < len(population.trades):
            msg = "That trade is not in the current results. Reload the page."
            raise ResultsChangedError(msg)
        trade = population.trades[index]
        entry_no = next(
            (
                s.entry_no
                for s in select(population.trades, filters, ignore_mode=True)
                if s.index == index
            ),
            0,
        )
        with session(self._url) as conn:
            book = Store(conn).load_option_book({trade.trade_date: {trade.symbol}})
        candles = book.candles(trade.symbol, trade.trade_date)
        return {
            "run": population.cache_key,
            "id": index,
            "trade": trade_card(trade, entry_no, self._schedule.rates_on(trade.trade_date)),
            "spot": {
                "timeframe": timeframe,
                "fast": population.params.fast,
                "slow": population.params.slow,
                "bars": population.bars.get(trade.trade_date, []),
            },
            "option": {
                "symbol": trade.symbol,
                "candles": [
                    [
                        c.start.isoformat(),
                        float(c.open),
                        float(c.high),
                        float(c.low),
                        float(c.close),
                        c.volume,
                    ]
                    for c in sorted(candles.values(), key=lambda c: c.start)
                ],
            },
        }

    # --------------------------------------------------------------------- skips

    def skips(self, timeframe: int, date_from: date | None, date_to: date | None) -> dict[str, Any]:
        """Candidate trades that were not simulated, and days that could not be traded."""
        population = self._population(timeframe)

        def inside(day: date) -> bool:
            return (date_from is None or day >= date_from) and (date_to is None or day <= date_to)

        skips = [s for s in population.skips if inside(s.trade_date)]
        days = [d for d in population.days if inside(d.day)]
        return {
            "run": population.cache_key,
            "timeframe": timeframe,
            "skipped_trades": [
                {
                    "date": s.trade_date.isoformat(),
                    "mode": s.mode.value,
                    "decision": s.decision_time.strftime("%H:%M"),
                    "reason": s.reason.value,
                    "detail": s.detail,
                }
                for s in skips
            ],
            "skip_reasons": dict(Counter(s.reason.value for s in skips)),
            "day_counts": dict(Counter(d.kind for d in days)),
            "incomplete_days": [
                {"date": d.day.isoformat(), "detail": d.detail}
                for d in days
                if d.kind == "incomplete_spot"
            ],
        }

    # ---------------------------------------------------------------------- data

    def data_status(self) -> dict[str, Any]:
        with session(self._url) as conn:
            store = Store(conn)
            span = store.spot_range(SPOT_SYMBOL)
            summary = store.fetch_summary()
            failures = store.failures(10)
            spot_rows = store.count_candles("spot_candles")
            option_rows = store.count_candles("option_candles")
            expiries = store.load_expiries(UNDERLYING)
            convention = store.get_meta("candle_timestamp_convention")
            source = store.get_meta(DATA_SOURCE_KEY)
            labels = store.import_labels()
        return {
            "has_data": span is not None,
            "synthetic": source == SYNTHETIC_SOURCE,
            "source": source,
            "data_source": describe_source(source, labels) if span is not None else None,
            "spot_range": None if span is None else [span[0].isoformat(), span[1].isoformat()],
            "spot_rows": spot_rows,
            "option_rows": option_rows,
            "expiries": len(expiries),
            "timestamp_convention": convention,
            "fetch_summary": [
                {"kind": k, "status": s, "days": d, "rows": r} for k, s, d, r in summary
            ],
            "failures": [
                {"kind": k, "key": key, "day": day.isoformat(), "detail": detail}
                for k, key, day, detail in failures
            ],
        }
