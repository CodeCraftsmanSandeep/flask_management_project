"""A cached run as plain JSON, so a static website can browse it without this server.

``scripts/build_site.py`` writes one *bundle* per signal timeframe plus one file per trade (the
exact answer of ``BacktestService.trade_detail``). ``app/static/js/static-api.js`` then does in the
browser what ``query.py``, ``metrics.py`` and ``present.py`` do here: filter the trades, number the
entries, compute the KPIs. ``tests/test_static_api.py`` runs both on the same trades and demands
identical JSON.

Money is written as exact decimal text (never a float) so the browser can add it up as integers and
round it exactly the way ``Decimal`` does.
"""

from decimal import Decimal
from typing import Any

from app.backtest.runs import Population
from app.strategy.models import Trade


def decimal_text(value: Decimal) -> str:
    """Plain digits, never ``1E+1``."""
    return format(value, "f")


def places(value: Decimal) -> int:
    exponent = value.as_tuple().exponent
    return -exponent if isinstance(exponent, int) and exponent < 0 else 0


def decimals_needed(trades: list[Trade]) -> int:
    """The widest fraction of any money value, so the browser can scale them all to integers."""
    return max(
        (
            max(places(t.gross_pnl), places(t.slippage), places(t.charges.total), places(t.net_pnl))
            for t in trades
        ),
        default=0,
    )


def trade_record(trade: Trade) -> dict[str, Any]:
    """What the browser needs to filter, number, sort and total a trade (not to draw it)."""
    return {
        "date": trade.trade_date.isoformat(),
        "entry_decision": trade.entry_decision.isoformat(),
        "entry_fill": trade.entry_fill.isoformat(),
        "exit_fill": trade.exit_fill.isoformat(),
        "mode": trade.mode.value,
        "direction": trade.direction.value,
        "side": trade.side.value,
        "option": f"{trade.strike} {trade.option_type.value}",
        "exit_reason": trade.exit_reason.value,
        "whipsaw": trade.whipsaw,
        "gross": decimal_text(trade.gross_pnl),
        "slippage": decimal_text(trade.slippage),
        "charges": decimal_text(trade.charges.total),
        "net": decimal_text(trade.net_pnl),
    }


def population_bundle(population: Population) -> dict[str, Any]:
    """Everything ``/api/backtest/trades`` and ``/api/backtest/skips`` are computed from.

    A trade's position in ``trades`` is its id, exactly as in the live app.
    """
    return {
        "run": population.cache_key,
        "timeframe": population.timeframe,
        "range": {
            "first": population.first_day.isoformat() if population.first_day else None,
            "last": population.last_day.isoformat() if population.last_day else None,
        },
        "computed": {
            "from_cache": True,
            "created_at": population.created_at.isoformat() if population.created_at else None,
        },
        "decimals": decimals_needed(population.trades),
        "trades": [trade_record(t) for t in population.trades],
        "skips": [
            {
                "date": s.trade_date.isoformat(),
                "mode": s.mode.value,
                "decision": s.decision_time.strftime("%H:%M"),
                "reason": s.reason.value,
                "detail": s.detail,
            }
            for s in population.skips
        ],
        "days": [
            {"date": d.day.isoformat(), "kind": d.kind, "detail": d.detail} for d in population.days
        ],
    }
