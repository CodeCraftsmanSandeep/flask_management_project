"""Differential tests: the entry window is only a filter, and that filter is *exactly* right.

Three independent routes to the same answer must agree on random price paths:

1. the **population** (widest window) simulated once, then filtered by entry time -- what the app
   caches and serves;
2. a **windowed simulation**, where the engine itself is run with that window; and
3. a small **reference state machine** written out here, from the rules in plain English.
"""

import itertools
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timedelta
from random import Random

import pytest

from app.strategy.engine import StrategyParams, analyse_spot, run_backtest
from app.strategy.models import Candle, Cross, Mode, Trade
from tests.builders import FullBook, spot_day

STARTS = [time(9, 30), time(10, 0), time(10, 30)]
CUTOFFS = [
    time(11, 30),
    time(12, 0),
    time(12, 30),
    time(13, 0),
    time(13, 30),
    time(14, 0),
    time(14, 30),
]
SQUARE_OFF = time(15, 0)
DAYS = [date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13), date(2026, 5, 14)]

Row = tuple[datetime, datetime, str, str]  # entry decision, exit decision, reason, direction


def random_market(seed: int, timeframe: int) -> list[Candle]:
    rng = Random(seed)
    bars = -(-375 // timeframe)
    price = 24000.0
    candles: list[Candle] = []
    for day in DAYS:
        closes = []
        price += rng.gauss(0, 60)  # overnight gap
        for _ in range(bars):
            price += rng.gauss(0, 18 * timeframe**0.5)
            closes.append(round(price, 2))
        candles += spot_day(day, closes, timeframe)
    return candles


def reference_rows(crosses: list[Cross], start: time, cutoff: time) -> list[Row]:
    """The rules, as a state machine over one day's crosses (no engine code involved)."""
    rows: list[Row] = []
    holding: tuple[datetime, str] | None = None
    for cross in crosses:
        if cross.decision_time.time() >= SQUARE_OFF:
            break
        if holding is not None:
            rows.append((holding[0], cross.decision_time, "opposite_cross", holding[1]))
            holding = None
        if start <= cross.decision_time.time() <= cutoff:
            holding = (cross.decision_time, cross.direction.value)
    if holding is not None:
        end = datetime.combine(holding[0].date(), SQUARE_OFF)
        rows.append((holding[0], end, "square_off", holding[1]))
    return rows


def rows_of(trades: Sequence[Trade]) -> list[Row]:
    return [
        (t.entry_decision, t.exit_decision, t.exit_reason.value, t.direction.value)
        for t in trades
        if t.mode is Mode.ATM_BUY
    ]


@pytest.mark.parametrize("timeframe", [5, 15, 30])
@pytest.mark.parametrize("seed", [1, 2, 3, 4])
@pytest.mark.parametrize(("fast", "slow"), [(9, 21), (2, 5)])
def test_filter_of_population_equals_windowed_run_equals_reference(
    seed: int, timeframe: int, fast: int, slow: int
) -> None:
    spot = random_market(seed, timeframe)
    base = StrategyParams(timeframe=timeframe, fast=fast, slow=slow)
    population = run_backtest(spot, [date(2026, 5, 21)], FullBook(), base)
    assert population.skips == []
    all_crosses = population.analysis.crosses
    population_rows = rows_of(population.trades)
    assert population_rows, "random market produced no trades; the test would prove nothing"

    for start in STARTS:
        for cutoff in CUTOFFS:
            filtered = [r for r in population_rows if start <= r[0].time() <= cutoff]

            windowed = run_backtest(
                spot,
                [date(2026, 5, 21)],
                FullBook(),
                replace(base, earliest_entry=start, latest_entry=cutoff),
            )
            assert rows_of(windowed.trades) == filtered, (start, cutoff)

            expected: list[Row] = []
            for day in DAYS:
                day_crosses = [c for c in all_crosses if c.decision_time.date() == day]
                expected += reference_rows(day_crosses, start, cutoff)
            assert sorted(filtered) == sorted(expected), (start, cutoff)


def test_the_random_markets_actually_exercise_reversals_and_square_offs() -> None:
    reasons: set[str] = set()
    multi_trade_days = 0
    for seed in (1, 2, 3, 4):
        result = run_backtest(
            random_market(seed, 5), [date(2026, 5, 21)], FullBook(), StrategyParams(fast=2, slow=5)
        )
        rows = rows_of(result.trades)
        reasons |= {r[2] for r in rows}
        per_day: dict[date, int] = {}
        for r in rows:
            per_day[r[0].date()] = per_day.get(r[0].date(), 0) + 1
        multi_trade_days += sum(1 for n in per_day.values() if n > 1)
    assert reasons == {"opposite_cross", "square_off"}
    assert multi_trade_days > 0


def test_a_trade_never_outlives_the_session_or_precedes_its_signal() -> None:
    for timeframe in (5, 15, 30):
        result = run_backtest(
            random_market(9, timeframe),
            [date(2026, 5, 21)],
            FullBook(),
            StrategyParams(timeframe=timeframe, fast=2, slow=5),
        )
        assert result.trades
        for t in result.trades:
            assert t.entry_decision < t.exit_decision <= datetime.combine(t.trade_date, SQUARE_OFF)
            assert t.entry_fill >= t.entry_decision
            assert t.entry_fill < t.exit_fill
            assert t.exit_fill - t.entry_fill < timedelta(hours=6)


def test_analysis_crosses_alternate_in_every_random_market() -> None:
    for seed in (1, 2, 3):
        crosses = analyse_spot(random_market(seed, 5), StrategyParams(fast=2, slow=5)).crosses
        directions = [c.direction for c in crosses]
        assert all(a is not b for a, b in itertools.pairwise(directions))
