"""Which option to trade: expiry, strike, Groww symbol.

* **ATM** is the signal-bar close rounded to the nearest multiple of 50; a price exactly halfway
  (25 points from both) rounds *up*.
* **ITM 5** is five strikes in the money: 250 points *below* ATM for a call, *above* for a put.
* **Directional mapping** (every mode expresses the same market view):

  ============  ==============================  ==============================
  Mode          Bullish cross                   Bearish cross
  ============  ==============================  ==============================
  ITM 5 buy     BUY CE at ATM - 250             BUY PE at ATM + 250
  ATM buy       BUY CE at ATM                   BUY PE at ATM
  ATM sell      SELL PE at ATM                  SELL CE at ATM
  ============  ==============================  ==============================

* **Expiry** is the nearest listed expiry on or after the trade date (so expiry day itself is
  allowed, DTE 0), or the one after it when ``skip_expiry_day`` is set.
"""

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .models import Direction, Mode, OptionType, Side

STRIKE_STEP = 50
ITM_STEPS = 5
UNDERLYING = "NIFTY"
EXCHANGE_PREFIX = "NSE"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


@dataclass(frozen=True, slots=True)
class OptionLeg:
    side: Side
    option_type: OptionType
    strike: int


def atm_strike(spot: Decimal) -> int:
    """Nearest strike to *spot*; exact ties go to the higher strike."""
    half = Decimal(STRIKE_STEP) / 2
    return int((spot + half) // STRIKE_STEP) * STRIKE_STEP


def leg_for(mode: Mode, direction: Direction, atm: int) -> OptionLeg:
    bullish = direction is Direction.BULLISH
    if mode is Mode.ATM_SELL:
        return OptionLeg(Side.SELL, OptionType.PE if bullish else OptionType.CE, atm)
    option_type = OptionType.CE if bullish else OptionType.PE
    if mode is Mode.ATM_BUY:
        return OptionLeg(Side.BUY, option_type, atm)
    depth = ITM_STEPS * STRIKE_STEP
    return OptionLeg(Side.BUY, option_type, atm - depth if bullish else atm + depth)


def groww_symbol(expiry: date, strike: int, option_type: OptionType) -> str:
    """``NSE-NIFTY-02Jan25-24650-CE``; month names are fixed English, not locale-dependent."""
    stamp = f"{expiry.day:02d}{_MONTHS[expiry.month - 1]}{expiry.year % 100:02d}"
    return f"{EXCHANGE_PREFIX}-{UNDERLYING}-{stamp}-{strike}-{option_type.value}"


def expiry_from_symbol(symbol: str) -> date:
    """Inverse of :func:`groww_symbol` for the expiry: ``NSE-NIFTY-02Jan25-24650-CE`` -> 2025-01-02.

    Raises ``ValueError`` for anything that is not a symbol of that shape.
    """
    parts = symbol.split("-")
    if len(parts) != 5 or len(parts[2]) != 7:
        msg = f"not an option symbol: {symbol!r}"
        raise ValueError(msg)
    stamp = parts[2]
    try:
        month = _MONTHS.index(stamp[2:5]) + 1
        return date(2000 + int(stamp[5:]), month, int(stamp[:2]))
    except ValueError as exc:
        msg = f"not an option symbol: {symbol!r}"
        raise ValueError(msg) from exc


def pick_expiry(
    trade_date: date, expiries: Sequence[date], *, skip_expiry_day: bool = False
) -> date | None:
    """The expiry to trade on *trade_date* from a sorted list, or None if none is listed."""
    index = bisect_left(expiries, trade_date)
    if skip_expiry_day and index < len(expiries) and expiries[index] == trade_date:
        index += 1
    return expiries[index] if index < len(expiries) else None
