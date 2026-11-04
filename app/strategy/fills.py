"""Turning a candle price into an executed price.

* Raw price = the **open of the 1-minute option candle** that starts at the decision time.
* A buy pays ``raw * (1 + pct) + points``, rounded **up** to the 0.05 tick.
* A sell receives ``raw * (1 - pct) - points``, rounded **down** to the tick and never below 0.05.

Rounding always goes against the trader, so tick rounding can only add to slippage, never hide it.
"""

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

TICK = Decimal("0.05")


def to_tick(price: Decimal, *, up: bool) -> Decimal:
    ticks = (price / TICK).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR)
    return ticks * TICK


def buy_fill(raw: Decimal, pct: Decimal, points: Decimal) -> Decimal:
    return to_tick(raw * (1 + pct) + points, up=True)


def sell_fill(raw: Decimal, pct: Decimal, points: Decimal) -> Decimal:
    return max(to_tick(raw * (1 - pct) - points, up=False), TICK)
