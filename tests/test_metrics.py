from datetime import datetime, timedelta
from decimal import Decimal
from random import Random

import pytest

from app.strategy.costs import Charges
from app.strategy.metrics import compute_metrics, equity_curve
from app.strategy.models import Trade
from tests.builders import make_trade

D = Decimal
T0 = datetime(2026, 5, 12, 10, 0)


def series(nets: list[float]) -> list[Trade]:
    """Trades whose net P&L is *nets*, exiting one minute apart in the given order."""
    return [
        make_trade(n, entry_at=T0 + timedelta(minutes=i), exit_at=T0 + timedelta(minutes=i + 1))
        for i, n in enumerate(nets)
    ]


def test_no_trades_gives_empty_but_safe_metrics() -> None:
    m = compute_metrics([])
    assert (m.trades, m.wins, m.losses, m.ties) == (0, 0, 0, 0)
    assert m.win_rate is None
    assert m.profit_factor is None
    assert not m.profit_factor_infinite
    assert m.payoff_ratio is None
    assert m.expectancy is None
    assert m.best_trade is None
    assert m.worst_trade is None
    assert m.max_drawdown == 0
    assert m.max_drawdown_pct is None
    assert m.net_pnl == 0
    assert m.max_consecutive_losses == 0


def test_mixed_series_every_metric_by_hand() -> None:
    m = compute_metrics(series([300, -100, 200, -400, 100]))
    # equity: 300, 200, 400, 0, 100 ; peak 300, 300, 400, 400, 400 ; falls 0, 100, 0, 400, 300
    assert (m.trades, m.wins, m.losses, m.ties) == (5, 3, 2, 0)
    assert m.win_rate == pytest.approx(0.6)
    assert m.net_pnl == D(100)
    assert m.profit_factor == pytest.approx(600 / 500)
    assert not m.profit_factor_infinite
    assert m.avg_win == D(200)
    assert m.avg_loss == D(-250)
    assert m.payoff_ratio == pytest.approx(0.8)  # 200 / 250
    assert m.expectancy == D(20)
    assert m.max_drawdown == D(400)
    assert m.max_drawdown_pct == pytest.approx(400 / (100_000 + 400) * 100)
    assert (m.best_trade, m.worst_trade) == (D(300), D(-400))
    assert m.max_consecutive_losses == 1


def test_all_winners_has_an_infinite_profit_factor_flag() -> None:
    m = compute_metrics(series([10, 20]))
    assert m.profit_factor is None
    assert m.profit_factor_infinite
    assert m.payoff_ratio is None
    assert m.avg_loss is None
    assert m.win_rate == 1.0
    assert m.max_drawdown == 0


def test_all_losers_has_a_zero_profit_factor() -> None:
    m = compute_metrics(series([-10, -20]))
    assert m.profit_factor == 0.0
    assert not m.profit_factor_infinite
    assert m.avg_win is None
    assert m.win_rate == 0.0
    assert m.max_drawdown == D(30)


def test_ties_count_as_trades_but_neither_win_nor_loss() -> None:
    m = compute_metrics(series([0, 0, 5]))
    assert (m.trades, m.wins, m.losses, m.ties) == (3, 1, 0, 2)
    assert m.win_rate == pytest.approx(1 / 3)
    assert m.profit_factor_infinite  # a win and no loss
    assert m.max_consecutive_losses == 0


def test_a_tie_does_not_break_a_losing_streak_count_but_a_win_does() -> None:
    assert compute_metrics(series([-1, -2, -3, 5, -1])).max_consecutive_losses == 3
    assert compute_metrics(series([-1, 0, -1])).max_consecutive_losses == 1  # tie resets
    assert compute_metrics(series([-1, -1, -1, -1])).max_consecutive_losses == 4


def test_drawdown_is_measured_in_exit_order_not_input_order() -> None:
    trades = series([300, -100, 200, -400, 100])
    shuffled = trades[:]
    Random(7).shuffle(shuffled)
    assert compute_metrics(shuffled) == compute_metrics(trades)


def test_equity_curve_is_cumulative_in_exit_order() -> None:
    curve = equity_curve(series([300, -100, 200]))
    assert [equity for _, equity in curve] == [D(300), D(200), D(400)]


def test_overlapping_trades_are_ordered_by_exit_time() -> None:
    long_one = make_trade(-500, entry_at=T0, exit_at=T0 + timedelta(minutes=60))
    short_one = make_trade(
        +200, entry_at=T0 + timedelta(minutes=5), exit_at=T0 + timedelta(minutes=10)
    )
    assert [t.net_pnl for t, _ in equity_curve([long_one, short_one])] == [D(200), D(-500)]


def test_drawdown_percent_is_relative_to_capital_plus_peak_profit() -> None:
    trades = series([1000, -500])
    assert compute_metrics(trades, capital=D(10_000)).max_drawdown_pct == pytest.approx(
        500 / 11_000 * 100
    )
    assert compute_metrics(trades, capital=D(1_000)).max_drawdown_pct == pytest.approx(25.0)


def test_drawdown_starts_from_zero_when_the_first_trades_lose() -> None:
    m = compute_metrics(series([-200, -100]))
    assert m.max_drawdown == D(300)
    assert m.max_drawdown_pct == pytest.approx(300 / 100_000 * 100)


def test_drawdown_percent_is_undefined_without_positive_capital() -> None:
    assert compute_metrics(series([-5]), capital=D(0)).max_drawdown_pct is None
    assert compute_metrics(series([-5]), capital=D(0)).max_drawdown == D(5)


def test_money_columns_are_summed_separately() -> None:
    fees = Charges(brokerage=D(40), stt=D(10), exchange=D(5), sebi=D(1), stamp=D(2), gst=D(8))
    a = make_trade(net=134, gross=300, slippage=100, charges=fees)  # 300 - 100 - 66
    b = make_trade(net=-30, gross=-10, slippage=20, charges=Charges())  # -10 - 20 - 0
    m = compute_metrics([a, b])
    assert m.gross_pnl == D(290)
    assert m.slippage == D(120)
    assert m.charges == fees.total == D(66)
    assert m.net_pnl == D(104)
    assert m.gross_pnl - m.slippage - m.charges == m.net_pnl


def test_metrics_are_exact_decimals_not_floats() -> None:
    m = compute_metrics(series([0.1, 0.2, -0.3]))
    assert m.net_pnl == D(0)  # float arithmetic would give 5.5e-17
    assert m.ties == 0
    assert m.wins == 2
