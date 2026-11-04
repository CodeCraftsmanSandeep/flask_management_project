"""Performance metrics over a list of trades.

Definitions (all on **net** P&L unless the name says gross):

* **win / loss / tie**: net > 0 / net < 0 / net == 0. A tie counts as a trade but is neither a win
  nor a loss.
* **win rate** = wins / trades.
* **profit factor** = sum of winning nets / |sum of losing nets|. ``None`` when there are no losing
  trades (it would be infinite) or no trades at all; ``profit_factor_infinite`` tells the two apart.
* **payoff ratio** = average win / |average loss|; ``None`` unless there is at least one of each.
* **expectancy** = net P&L per trade.
* **max drawdown** is measured on the *realised* cumulative net P&L, trades ordered by exit time
  (then entry time). The curve starts at 0 before the first trade, i.e. at starting capital. In
  rupees it is the largest peak-to-trough fall. As a percentage it is that fall relative to
  ``starting capital + peak P&L`` at the time, taking the largest such ratio. It does not look
  inside trades (no mark-to-market).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from .models import Trade

ZERO = Decimal(0)
DEFAULT_CAPITAL = Decimal(100_000)


@dataclass(frozen=True, slots=True)
class Metrics:
    trades: int
    wins: int
    losses: int
    ties: int
    win_rate: float | None
    gross_pnl: Decimal
    slippage: Decimal
    charges: Decimal
    net_pnl: Decimal
    profit_factor: float | None
    profit_factor_infinite: bool
    payoff_ratio: float | None
    avg_win: Decimal | None
    avg_loss: Decimal | None
    expectancy: Decimal | None
    max_drawdown: Decimal
    max_drawdown_pct: float | None
    best_trade: Decimal | None
    worst_trade: Decimal | None
    max_consecutive_losses: int


def equity_curve(trades: Sequence[Trade]) -> list[tuple[Trade, Decimal]]:
    """Trades in exit order with the cumulative net P&L after each."""
    ordered = sorted(trades, key=lambda t: (t.exit_fill, t.entry_fill, t.mode.value))
    total = ZERO
    curve = []
    for trade in ordered:
        total += trade.net_pnl
        curve.append((trade, total))
    return curve


def compute_metrics(trades: Sequence[Trade], capital: Decimal = DEFAULT_CAPITAL) -> Metrics:
    nets = [t.net_pnl for t in trades]
    wins = [n for n in nets if n > 0]
    losses = [n for n in nets if n < 0]
    ties = len(nets) - len(wins) - len(losses)

    gross_win, gross_loss = sum(wins, ZERO), -sum(losses, ZERO)
    profit_factor = float(gross_win / gross_loss) if gross_loss > 0 else None
    avg_win = gross_win / len(wins) if wins else None
    avg_loss = -gross_loss / len(losses) if losses else None
    payoff = float(avg_win / -avg_loss) if avg_win is not None and avg_loss is not None else None

    peak, drawdown, drawdown_pct = ZERO, ZERO, 0.0
    equity = ZERO
    streak = longest = 0
    for trade, equity in equity_curve(trades):
        peak = max(peak, equity)
        fall = peak - equity
        drawdown = max(drawdown, fall)
        base = capital + peak
        if base > 0:  # a non-positive base makes a percentage meaningless
            drawdown_pct = max(drawdown_pct, float(fall / base * 100))
        streak = streak + 1 if trade.net_pnl < 0 else 0
        longest = max(longest, streak)

    count = len(nets)
    return Metrics(
        trades=count,
        wins=len(wins),
        losses=len(losses),
        ties=ties,
        win_rate=len(wins) / count if count else None,
        gross_pnl=sum((t.gross_pnl for t in trades), ZERO),
        slippage=sum((t.slippage for t in trades), ZERO),
        charges=sum((t.charges.total for t in trades), ZERO),
        net_pnl=sum(nets, ZERO),
        profit_factor=profit_factor,
        profit_factor_infinite=bool(wins) and not losses,
        payoff_ratio=payoff,
        avg_win=avg_win,
        avg_loss=avg_loss,
        expectancy=sum(nets, ZERO) / count if count else None,
        max_drawdown=drawdown,
        max_drawdown_pct=drawdown_pct if count and capital > 0 else None,
        best_trade=max(nets) if nets else None,
        worst_trade=min(nets) if nets else None,
        max_consecutive_losses=longest,
    )
