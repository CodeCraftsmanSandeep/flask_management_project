"""Builders for engine tests: synthetic candles, option books and trades. No I/O."""

from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from app.strategy.bars import MINUTES_PER_SESSION, SESSION_OPEN
from app.strategy.costs import Charges
from app.strategy.models import (
    Candle,
    Direction,
    ExitReason,
    Mode,
    OptionType,
    Side,
    Trade,
)


def dec(value: str | int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def at(day: date, hhmm: str) -> datetime:
    hour, minute = hhmm.split(":")
    return datetime.combine(day, time(int(hour), int(minute)))


def candle(start: datetime, price: str | float | Decimal, volume: int = 100) -> Candle:
    p = dec(price)
    return Candle(start=start, open=p, high=p, low=p, close=p, volume=volume)


def session_minutes(day: date) -> list[datetime]:
    first = datetime.combine(day, SESSION_OPEN)
    return [first + timedelta(minutes=i) for i in range(MINUTES_PER_SESSION)]


def spot_day(day: date, bar_closes: Sequence[float], timeframe: int = 5) -> list[Candle]:
    """A full 375-minute NIFTY day where every 1-minute candle of bar *i* is flat at closes[i].

    So the closed bar's close equals ``bar_closes[i]`` exactly, which makes crossovers easy to
    reason about by hand.
    """
    expected = -(-MINUTES_PER_SESSION // timeframe)  # ceil: a partial last bar counts
    if len(bar_closes) != expected:
        msg = f"need {expected} bar closes for {timeframe}-minute bars, got {len(bar_closes)}"
        raise ValueError(msg)
    return [candle(m, bar_closes[i // timeframe]) for i, m in enumerate(session_minutes(day))]


def piecewise(changes: Mapping[int, float], bars: int = 75) -> list[float]:
    """Constant price per bar that changes at the given bar indexes: ``{0: 100, 5: 110}``."""
    out: list[float] = []
    price = changes[0]
    for i in range(bars):
        price = changes.get(i, price)
        out.append(price)
    return out


Price = float | str | tuple[float | str, float | str]


class DictBook:
    """Sparse option candles: ``{symbol: {"09:45": 210.0, ...}}``, each a flat candle at that price.

    A ``(open, close)`` tuple makes a candle whose open and close differ, to prove which one the
    engine reads (high/low are set to enclose both).
    """

    def __init__(self, day: date, prices: Mapping[str, Mapping[str, Price]]) -> None:
        self._day = day
        self._data: dict[str, dict[datetime, Candle]] = {}
        for symbol, minutes in prices.items():
            self._data[symbol] = {
                at(day, hhmm): self._make(at(day, hhmm), price) for hhmm, price in minutes.items()
            }
        self.requested: list[str] = []

    @staticmethod
    def _make(start: datetime, price: Price) -> Candle:
        if not isinstance(price, tuple):
            return candle(start, price)
        open_, close = dec(price[0]), dec(price[1])
        return Candle(
            start=start,
            open=open_,
            high=max(open_, close),
            low=min(open_, close),
            close=close,
            volume=100,
        )

    def candles(self, symbol: str, day: date) -> Mapping[datetime, Candle]:
        self.requested.append(symbol)
        return self._data.get(symbol, {}) if day == self._day else {}


class FullBook:
    """Every contract has a flat candle every minute, so nothing is ever skipped."""

    def __init__(self, price: str | float = "100.00") -> None:
        self._price = dec(price)
        self._cache: dict[date, dict[datetime, Candle]] = {}

    def candles(self, symbol: str, day: date) -> Mapping[datetime, Candle]:
        if day not in self._cache:
            self._cache[day] = {m: candle(m, self._price) for m in session_minutes(day)}
        return self._cache[day]


def make_trade(
    net: str | float | Decimal = "0",
    *,
    exit_at: datetime | None = None,
    entry_at: datetime | None = None,
    mode: Mode = Mode.ATM_BUY,
    day: date = date(2026, 5, 12),
    gross: str | float | Decimal | None = None,
    slippage: str | float | Decimal = "0",
    charges: Charges | None = None,
) -> Trade:
    """A Trade whose P&L fields are consistent with each other (gross - slippage - charges)."""
    entry = entry_at or datetime.combine(day, time(10, 0))
    exit_ = exit_at or datetime.combine(day, time(10, 30))
    fees = charges or Charges()
    net_value = dec(net)
    gross_value = dec(gross) if gross is not None else net_value + dec(slippage) + fees.total
    return Trade(
        mode=mode,
        timeframe=5,
        trade_date=day,
        cross_day_no=1,
        direction=Direction.BULLISH,
        side=Side.BUY,
        symbol="NSE-NIFTY-12May26-24000-CE",
        option_type=OptionType.CE,
        strike=24000,
        expiry=day,
        dte=0,
        quantity=65,
        entry_decision=entry,
        entry_fill=entry,
        exit_decision=exit_,
        exit_fill=exit_,
        exit_reason=ExitReason.OPPOSITE_CROSS,
        spot_entry=dec(24000),
        spot_exit=dec(24010),
        option_entry_raw=dec(100),
        option_exit_raw=dec(101),
        option_entry_fill=dec(100),
        option_exit_fill=dec(101),
        gross_pnl=gross_value,
        slippage=dec(slippage),
        charges=fees,
        net_pnl=net_value,
        whipsaw=False,
        stale_exit=False,
        cost_verified=True,
    )
