"""Shape engine objects for the browser: plain JSON values, rupees as numbers, times as text.

Everything numeric is rounded for display only (the exact ``Decimal`` values stay in the cache).
"""

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from app.backtest.query import Selected, outcome_of
from app.strategy.costs import CostRates
from app.strategy.metrics import Metrics, equity_curve
from app.strategy.models import MODE_LABELS, Trade


def rupees(value: Decimal | None) -> float | None:
    return None if value is None else float(round(value, 2))


def metrics_json(m: Metrics) -> dict[str, Any]:
    return {
        "trades": m.trades,
        "wins": m.wins,
        "losses": m.losses,
        "ties": m.ties,
        "win_rate": None if m.win_rate is None else round(m.win_rate, 4),
        "gross_pnl": rupees(m.gross_pnl),
        "slippage": rupees(m.slippage),
        "charges": rupees(m.charges),
        "net_pnl": rupees(m.net_pnl),
        "profit_factor": None if m.profit_factor is None else round(m.profit_factor, 3),
        "profit_factor_infinite": m.profit_factor_infinite,
        "payoff_ratio": None if m.payoff_ratio is None else round(m.payoff_ratio, 3),
        "avg_win": rupees(m.avg_win),
        "avg_loss": rupees(m.avg_loss),
        "expectancy": rupees(m.expectancy),
        "max_drawdown": rupees(m.max_drawdown),
        "max_drawdown_pct": None if m.max_drawdown_pct is None else round(m.max_drawdown_pct, 2),
        "best_trade": rupees(m.best_trade),
        "worst_trade": rupees(m.worst_trade),
        "max_consecutive_losses": m.max_consecutive_losses,
    }


def trade_row(selected: Selected) -> dict[str, Any]:
    """The compact row of the trade list."""
    t = selected.trade
    return {
        "id": selected.index,
        "date": t.trade_date.isoformat(),
        "entry": t.entry_fill.strftime("%H:%M"),
        "exit": t.exit_fill.strftime("%H:%M"),
        "mode": t.mode.value,
        "direction": t.direction.value,
        "side": t.side.value,
        "option": f"{t.strike} {t.option_type.value}",
        "net": rupees(t.net_pnl),
        "outcome": outcome_of(t),
        "exit_reason": t.exit_reason.value,
        "whipsaw": t.whipsaw,
        "entry_no": selected.entry_no,
    }


def trade_card(trade: Trade, entry_no: int, rates: CostRates) -> dict[str, Any]:
    """Everything the metadata bar and the cost card show for one trade."""
    c = trade.charges
    return {
        "mode": trade.mode.value,
        "mode_label": MODE_LABELS[trade.mode],
        "timeframe": trade.timeframe,
        "date": trade.trade_date.isoformat(),
        "entry_no": entry_no,
        "signal_no": trade.cross_day_no,
        "symbol": trade.symbol,
        "direction": trade.direction.value,
        "side": trade.side.value,
        "option_type": trade.option_type.value,
        "strike": trade.strike,
        "expiry": trade.expiry.isoformat(),
        "dte": trade.dte,
        "quantity": trade.quantity,
        "entry_decision": trade.entry_decision.isoformat(),
        "entry_fill": trade.entry_fill.isoformat(),
        "exit_decision": trade.exit_decision.isoformat(),
        "exit_fill": trade.exit_fill.isoformat(),
        "exit_reason": trade.exit_reason.value,
        "spot_entry": rupees(trade.spot_entry),
        "spot_exit": rupees(trade.spot_exit),
        "option_entry_raw": rupees(trade.option_entry_raw),
        "option_exit_raw": rupees(trade.option_exit_raw),
        "option_entry_fill": rupees(trade.option_entry_fill),
        "option_exit_fill": rupees(trade.option_exit_fill),
        "whipsaw": trade.whipsaw,
        "stale_exit": trade.stale_exit,
        "cost_verified": trade.cost_verified,
        "outcome": outcome_of(trade),
        "costs": {
            "gross": rupees(trade.gross_pnl),
            "slippage": rupees(trade.slippage),
            "brokerage": rupees(c.brokerage),
            "stt": rupees(c.stt),
            "exchange": rupees(c.exchange),
            "sebi": rupees(c.sebi),
            "stamp": rupees(c.stamp),
            "gst": rupees(c.gst),
            "charges_total": rupees(c.total),
            "net": rupees(trade.net_pnl),
        },
        "rates": {
            "effective_from": rates.effective_from.isoformat(),
            "verified": rates.verified,
            "source": rates.source,
            "stt_sell_pct": float(rates.stt_sell_pct),
            "exchange_pct": float(rates.exchange_pct),
            "stamp_buy_pct": float(rates.stamp_buy_pct),
            "sebi_per_crore": float(rates.sebi_per_crore),
            "gst_pct": float(rates.gst_pct),
            "brokerage_per_order": float(rates.brokerage_per_order),
        },
    }


def equity_points(trades: Sequence[Trade]) -> list[list[Any]]:
    """``[exit time, cumulative net P&L]`` in exit order, for the small equity chart."""
    return [[t.exit_fill.isoformat(), rupees(total)] for t, total in equity_curve(trades)]


RULES: list[dict[str, str]] = [
    {
        "title": "Signal",
        "text": "9-EMA crossing the 21-EMA on a closed spot bar (NIFTY index). Bars are the "
        "selected timeframe, built from 1-minute candles anchored at 09:15. The EMAs run "
        "continuously across days (they are not reset each morning) and start from the first "
        "close. A tie (EMAs equal) keeps the previous direction; the very first direction "
        "is not a cross.",
    },
    {
        "title": "Which bar triggers the fill",
        "text": "The decision is made when the signal bar closes. The order fills at the open "
        "of the option's 1-minute candle that starts at that moment. If that candle is "
        "missing, the next candle within 5 minutes is used; otherwise the trade is skipped "
        "and listed (never invented).",
    },
    {
        "title": "Entry window",
        "text": "Signals before the start time or after the cutoff are ignored (inclusive at "
        "both ends). A cross before the start time does not carry into the window. The cutoff "
        "only blocks new entries; open trades still exit normally.",
    },
    {
        "title": "Exit and reversal",
        "text": "A trade exits on the opposite crossover, or at the 15:00 square-off. An opposite "
        "cross inside the window closes the trade and opens the reverse one immediately. A "
        "cross that completes at 15:00 or later is not acted on: square-off wins.",
    },
    {
        "title": "Contracts",
        "text": "Nearest weekly expiry on or after the trade date (expiry day itself is 0 DTE and "
        "is traded). ATM = signal-bar close rounded to the nearest 50 (ties go up). ITM 5 = "
        "five strikes (250 points) in the money. Buy modes: bullish buys a call, bearish "
        "buys a put. ATM sell: bullish sells a put, bearish sells a call.",
    },
    {
        "title": "Sizing and friction",
        "text": "1 lot = 65 units. Each leg pays 0.5% of the premium plus 0.5 points slippage "
        "(buys round up, sells round down to the 0.05 tick, never below 0.05), then the full "
        "cost stack: brokerage, STT, exchange charges, SEBI fee, stamp duty and GST at the "
        "rates in force on the trade date.",
    },
    {
        "title": "Gross vs net",
        "text": "Gross P&L uses the raw candle prices. Net P&L = gross - slippage - regulatory "
        "charges. Win rate, profit factor and the other KPIs are on net P&L.",
    },
    {
        "title": "No trade, no data",
        "text": "Days with fewer than 375 spot minutes are excluded (and listed). A signal with "
        "no usable option price, no listed expiry or a corrupt price is skipped and listed "
        "with the reason. Nothing is silently dropped.",
    },
    {
        "title": "What is not modelled",
        "text": "No stop-loss or target, no bid/ask spread beyond the slippage setting, no "
        "partial fills, no margin or liquidity limits. Max drawdown is on realised P&L only.",
    },
]
