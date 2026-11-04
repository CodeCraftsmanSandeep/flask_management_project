"""Plain data types for the backtest engine.

All timestamps are *naive datetimes in IST* (India has no daylight saving, so there is nothing to
get wrong, and the exchange clock is the only clock that matters). All money is :class:`Decimal`.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .costs import Charges


class Mode(StrEnum):
    """How the option leg is chosen and traded."""

    ITM5_BUY = "itm5_buy"
    ATM_BUY = "atm_buy"
    ATM_SELL = "atm_sell"


MODE_LABELS: dict[Mode, str] = {
    Mode.ITM5_BUY: "Deep ITM buy (ITM 5)",
    Mode.ATM_BUY: "ATM buy",
    Mode.ATM_SELL: "ATM sell",
}


class Direction(StrEnum):
    """The market view a crossover expresses (not the option side)."""

    BULLISH = "bullish"  # EMA9 crossed above EMA21
    BEARISH = "bearish"  # EMA9 crossed below EMA21


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OptionType(StrEnum):
    CE = "CE"
    PE = "PE"


class ExitReason(StrEnum):
    OPPOSITE_CROSS = "opposite_cross"
    SQUARE_OFF = "square_off"


class SkipReason(StrEnum):
    """Why a signal that should have become a trade did not. Nothing is dropped silently."""

    NO_EXPIRY = "no_expiry"  # no listed expiry on/after the trade date
    NO_OPTION_DATA = "no_option_data"  # no candle for the contract near the fill time
    BAD_PRICE = "bad_price"  # price below the 0.05 tick: corrupt data, not a tradable quote
    NO_SPOT_DATA = "no_spot_data"  # no NIFTY candle near the fill time


@dataclass(frozen=True, slots=True)
class Candle:
    """One 1-minute candle. ``start`` is the minute it opens (09:15 .. 15:29)."""

    start: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int = 0
    oi: int | None = None


@dataclass(frozen=True, slots=True)
class Bar:
    """A candle resampled to the signal timeframe, anchored at the 09:15 open."""

    start: datetime
    end: datetime  # the moment the bar is closed, i.e. when its signal can be acted on
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    minutes: int  # how many 1-minute candles actually went into the bar


@dataclass(frozen=True, slots=True)
class SignalBar(Bar):
    ema_fast: float
    ema_slow: float


@dataclass(frozen=True, slots=True)
class Cross:
    """EMA9 crossing EMA21 on a closed bar."""

    bar_start: datetime
    decision_time: datetime  # the bar's close: the earliest moment this signal is known
    direction: Direction
    close: Decimal  # signal-bar close: the spot price the ATM strike is based on
    ema_fast: float
    ema_slow: float
    day_no: int  # 1 = first cross of that trading day (counting every cross since 09:15)


@dataclass(frozen=True, slots=True)
class Skip:
    """A candidate trade that could not be simulated, with the reason."""

    trade_date: date
    mode: Mode
    decision_time: datetime
    reason: SkipReason
    detail: str


@dataclass(frozen=True, slots=True)
class Trade:
    mode: Mode
    timeframe: int
    trade_date: date
    cross_day_no: int
    direction: Direction
    side: Side
    symbol: str
    option_type: OptionType
    strike: int
    expiry: date
    dte: int
    quantity: int
    entry_decision: datetime
    entry_fill: datetime
    exit_decision: datetime
    exit_fill: datetime
    exit_reason: ExitReason
    spot_entry: Decimal
    spot_exit: Decimal
    option_entry_raw: Decimal
    option_exit_raw: Decimal
    option_entry_fill: Decimal
    option_exit_fill: Decimal
    gross_pnl: Decimal
    slippage: Decimal
    charges: Charges
    net_pnl: Decimal
    whipsaw: bool
    stale_exit: bool
    cost_verified: bool
