"""Simulated NIFTY and option prices, for trying the app without market-data access.

**These are not real prices.** A random walk stands in for the index and a Black-Scholes model
stands in for the options. The data goes through the very same downloader as real Groww data
(``scripts/demo_data.py``), into a *separate* database that is stamped as synthetic, so the pages
can show a banner and nobody mistakes the results for evidence about the strategy.
"""

import math
import random
import re
from datetime import date, datetime, time, timedelta
from typing import Any

SYNTHETIC_SOURCE = "synthetic_demo"
DATA_SOURCE_KEY = "data_source"

_MINUTES = 375  # 09:15 .. 15:29 inclusive
_OPEN = time(9, 15)
_SYMBOL = re.compile(r"^NSE-NIFTY-(\d{2})([A-Za-z]{3})(\d{2})-(\d+)-(CE|PE)$")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_EXPIRY_WEEKDAY = 1  # Tuesday
_TICK = 0.05
_MIN_PRICE = 0.05
_YEAR_MINUTES = 365 * 24 * 60


def _tick(value: float) -> float:
    return round(round(value / _TICK) * _TICK, 2)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def black_scholes(spot: float, strike: float, years: float, vol: float, *, call: bool) -> float:
    """Zero-rate Black-Scholes price; at or past expiry it is the intrinsic value."""
    intrinsic = max(spot - strike, 0.0) if call else max(strike - spot, 0.0)
    if years <= 0 or vol <= 0:
        return intrinsic
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + 0.5 * vol * vol * years) / root
    d2 = d1 - root
    if call:
        return spot * _norm_cdf(d1) - strike * _norm_cdf(d2)
    return strike * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def trading_days(first: date, last: date) -> list[date]:
    """Weekdays only (no exchange holidays are modelled)."""
    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    return [d for d in days if d.weekday() < 5]


class SyntheticSource:
    """Implements the downloader's ``MarketData`` interface with generated prices."""

    def __init__(
        self,
        first_day: date,
        last_day: date,
        *,
        seed: int = 7,
        start_level: float = 24000.0,
        volatility: float = 0.14,
    ) -> None:
        self._vol = volatility
        self._seed = seed
        self._days = trading_days(first_day, last_day)
        self._spot: dict[date, list[tuple[datetime, float, float, float, float]]] = {}
        self._build_spot(random.Random(seed), start_level)  # noqa: S311 - a simulation, not security
        closes = [bar[4] for bars in self._spot.values() for bar in bars]
        self._low = min(closes) if closes else start_level
        self._high = max(closes) if closes else start_level

    # ------------------------------------------------------------------------- spot

    def _build_spot(self, rng: random.Random, level: float) -> None:
        previous_return = 0.0
        for day in self._days:
            # Some days drift (so the EMAs produce clean crossovers), others chop sideways.
            drift = rng.choice((-1, 0, 0, 1)) * rng.uniform(0.00001, 0.00004)
            bars: list[tuple[datetime, float, float, float, float]] = []
            for minute in range(_MINUTES):
                start = datetime.combine(day, _OPEN) + timedelta(minutes=minute)
                shock = rng.gauss(0.0, 0.00034)
                ret = 0.18 * previous_return + shock + drift
                previous_return = ret
                open_ = level
                close = level * (1.0 + ret)
                wick = abs(rng.gauss(0.0, 0.00012))
                high = max(open_, close) * (1.0 + wick)
                low = min(open_, close) * (1.0 - abs(rng.gauss(0.0, 0.00012)))
                bars.append((start, _tick(open_), _tick(high), _tick(low), _tick(close)))
                level = close
            self._spot[day] = bars

    # ---------------------------------------------------------------- MarketData API

    def get_candles(
        self, *, exchange: str, segment: str, symbol: str, start: datetime, end: datetime
    ) -> list[list[Any]]:
        if exchange != "NSE":
            return []
        if segment == "CASH" and symbol == "NSE-NIFTY":
            rows = [
                self._row(bar[0], bar[1:], volume=0, oi=None)
                for day in self._days
                if start.date() <= day <= end.date()
                for bar in self._spot[day]
            ]
        elif segment == "FNO":
            rows = self._option_rows(symbol, start.date(), end.date())
        else:
            return []
        return [row for row in rows if start <= datetime.fromisoformat(row[0]) <= end]

    def get_expiries(
        self, *, exchange: str, underlying: str, year: int, month: int | None = None
    ) -> list[str]:
        if exchange != "NSE" or underlying != "NIFTY" or month is None:
            return []
        first = date(year, month, 1)
        expiries = []
        day = first
        while day.month == month:
            if day.weekday() == _EXPIRY_WEEKDAY:
                expiries.append(day.isoformat())
            day += timedelta(days=1)
        return expiries

    def get_contracts(self, *, exchange: str, underlying: str, expiry: date) -> list[str]:
        if exchange != "NSE" or underlying != "NIFTY":
            return []
        low = int(self._low // 50) * 50 - 1500
        high = int(self._high // 50) * 50 + 1550
        stamp = f"{expiry.day:02d}{_MONTHS[expiry.month - 1]}{expiry.year % 100:02d}"
        return [
            f"NSE-NIFTY-{stamp}-{k}-{kind}" for k in range(low, high, 50) for kind in ("CE", "PE")
        ]

    # ---------------------------------------------------------------------- options

    def _option_rows(self, symbol: str, first: date, last: date) -> list[list[Any]]:
        match = _SYMBOL.match(symbol)
        if not match:
            return []
        day_s, month_s, year_s, strike_s, kind = match.groups()
        if month_s not in _MONTHS:
            return []
        expiry = date(2000 + int(year_s), _MONTHS.index(month_s) + 1, int(day_s))
        strike = float(strike_s)
        rows: list[list[Any]] = []
        for day in self._days:
            if first <= day <= last and day <= expiry:
                rows.extend(self._option_day(symbol, day, expiry, strike, call=kind == "CE"))
        return rows

    def _option_day(
        self, symbol: str, day: date, expiry: date, strike: float, *, call: bool
    ) -> list[list[Any]]:
        rng = random.Random(f"{self._seed}|{symbol}|{day}")  # noqa: S311 - a simulation
        close_of_expiry = datetime.combine(expiry, time(15, 30))
        rows: list[list[Any]] = []
        for bar in self._spot[day]:
            start, s_open, _s_high, _s_low, s_close = bar

            def price(spot: float, at: datetime) -> float:
                years = (close_of_expiry - at).total_seconds() / 60 / _YEAR_MINUTES
                return black_scholes(spot, strike, years, self._vol, call=call)

            noise = 1.0 + rng.gauss(0.0, 0.002)  # quote jitter
            open_ = max(_MIN_PRICE, _tick(price(s_open, start) * noise))
            close = max(_MIN_PRICE, _tick(price(s_close, start + timedelta(minutes=1)) * noise))
            high = max(open_, close, _tick(max(open_, close) * (1.0 + abs(rng.gauss(0, 0.002)))))
            low = max(
                _MIN_PRICE,
                min(open_, close, _tick(min(open_, close) * (1 - abs(rng.gauss(0, 0.002))))),
            )
            volume = int(rng.uniform(200, 4000))
            rows.append(self._row(start, (open_, high, low, close), volume=volume, oi=50_000))
        return rows

    @staticmethod
    def _row(
        start: datetime, ohlc: tuple[float, float, float, float], *, volume: int, oi: int | None
    ) -> list[Any]:
        o, h, low, c = ohlc
        return [start.isoformat(), o, h, low, c, volume, oi]
