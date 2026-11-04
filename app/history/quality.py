"""Data-quality checks for downloaded candles. Pure functions, no I/O.

The checks exist because a backtest is only as honest as its data:

* **Timestamp convention.** The engine needs each candle stamped with the *start* of its minute
  (09:15 ... 15:29). If a source stamps the *close* (09:16 ... 15:30) every bar would be shifted by
  a minute and signals would be taken from the future, so the convention is detected from the data
  and never guessed. Contradictory or missing evidence stops the download.
* **Completeness.** A trading day must have all 375 regular-session minutes to be trusted with
  EMAs; the engine excludes days that do not, and this module says exactly what is missing.
* **Sanity.** Inverted highs/lows and non-positive prices are reported, not silently repaired.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

from app.strategy.bars import MINUTES_PER_SESSION, SESSION_CLOSE, SESSION_OPEN, in_session
from app.strategy.models import Candle

ONE_MINUTE = timedelta(minutes=1)
LAST_START = (datetime.combine(date.min, SESSION_CLOSE) - ONE_MINUTE).time()  # 15:29
FIRST_CLOSE = (datetime.combine(date.min, SESSION_OPEN) + ONE_MINUTE).time()  # 09:16


class Convention(StrEnum):
    START = "start"  # stamp = start of the minute (what the engine expects)
    CLOSE = "close"  # stamp = end of the minute
    UNKNOWN = "unknown"


def _day_vote(first: time, last: time) -> Convention | None:
    """What one day's first/last stamps say about the convention (None = no evidence).

    * 09:15 can only be a *start* stamp (nothing closes at the open), so it settles the day even
      if an extra 15:30 candle (a closing-auction print) follows.
    * 09:16 suggests *close* stamps, unless the day also ends at 15:29, which only start-stamped
      data does: then the 09:15 candle is merely missing and the evidence conflicts (abstain).
    * Otherwise the last stamp decides only when it is 15:29 (start). A last stamp of 15:30 proves
      nothing: close-stamped data ends there, but so does start-stamped data that carries a
      closing-auction print, and a partial day (a Muhurat session, a truncated feed) is common.
    """
    if first == SESSION_OPEN:
        return Convention.START
    if first == FIRST_CLOSE:
        return None if last == LAST_START else Convention.CLOSE
    if last == LAST_START:
        return Convention.START
    return None


def detect_convention(candles: Sequence[Candle]) -> Convention:
    """Decide from each day's first/last stamps. Conflicting or missing evidence is UNKNOWN."""
    by_day: dict[date, list[time]] = defaultdict(list)
    for candle in candles:
        stamp = candle.start.time()
        if SESSION_OPEN <= stamp <= SESSION_CLOSE:  # a pre-open or post-close print proves nothing
            by_day[candle.start.date()].append(stamp)
    votes = {_day_vote(min(stamps), max(stamps)) for stamps in by_day.values()} - {None}
    if votes == {Convention.START}:
        return Convention.START
    if votes == {Convention.CLOSE}:
        return Convention.CLOSE
    return Convention.UNKNOWN


def to_start_convention(candles: Sequence[Candle], convention: Convention) -> list[Candle]:
    """Re-stamp candles so every ts is the start of its minute."""
    if convention is Convention.START:
        return list(candles)
    if convention is Convention.CLOSE:
        return [_shift(c, -ONE_MINUTE) for c in candles]
    msg = "cannot normalise candles whose timestamp convention is unknown"
    raise ValueError(msg)


def _shift(candle: Candle, delta: timedelta) -> Candle:
    return Candle(
        start=candle.start + delta,
        open=candle.open,
        high=candle.high,
        low=candle.low,
        close=candle.close,
        volume=candle.volume,
        oi=candle.oi,
    )


def regular_session_only(candles: Sequence[Candle]) -> list[Candle]:
    """Drop anything stamped outside 09:15-15:29 (pre-open, closing-auction extras, ...)."""
    return [c for c in candles if in_session(c.start)]


@dataclass(frozen=True, slots=True)
class DayQuality:
    day: date
    present: int  # regular-session minutes present
    missing: tuple[time, ...]
    out_of_session: int
    bad_ohlc: int  # high < low, or open/close outside [low, high]
    non_positive: int  # any price <= 0

    @property
    def complete(self) -> bool:
        return self.present == MINUTES_PER_SESSION

    @property
    def clean(self) -> bool:
        return self.complete and not self.bad_ohlc and not self.non_positive

    def describe(self) -> str:
        """One line for the data page / fetch log; empty when there is nothing to say."""
        notes: list[str] = []
        if not self.complete:
            notes.append(f"{self.present}/{MINUTES_PER_SESSION} minutes")
            if self.missing:
                shown = ", ".join(t.strftime("%H:%M") for t in self.missing[:5])
                more = f" +{len(self.missing) - 5} more" if len(self.missing) > 5 else ""
                notes.append(f"missing {shown}{more}")
        if self.bad_ohlc:
            notes.append(f"{self.bad_ohlc} candles with inconsistent OHLC")
        if self.non_positive:
            notes.append(f"{self.non_positive} candles with a non-positive price")
        if self.out_of_session:
            notes.append(f"{self.out_of_session} outside session hours (ignored)")
        return "; ".join(notes)


def assess_day(day: date, candles: Sequence[Candle]) -> DayQuality:
    """Quality of one day's candles (all of them must belong to *day*)."""
    inside = [c for c in candles if in_session(c.start)]
    seen = {c.start for c in inside}
    first = datetime.combine(day, SESSION_OPEN)
    missing = tuple(
        (first + timedelta(minutes=i)).time()
        for i in range(MINUTES_PER_SESSION)
        if first + timedelta(minutes=i) not in seen
    )
    bad = sum(
        1
        for c in inside
        if c.high < c.low or not (c.low <= c.open <= c.high) or not (c.low <= c.close <= c.high)
    )
    non_positive = sum(1 for c in inside if min(c.open, c.high, c.low, c.close) <= 0)
    return DayQuality(
        day=day,
        present=len(seen),
        missing=missing,
        out_of_session=len(candles) - len(inside),
        bad_ohlc=bad,
        non_positive=non_positive,
    )


def assess(candles: Sequence[Candle]) -> list[DayQuality]:
    """Per-day quality for candles spanning several days."""
    by_day: dict[date, list[Candle]] = defaultdict(list)
    for candle in candles:
        by_day[candle.start.date()].append(candle)
    return [assess_day(day, by_day[day]) for day in sorted(by_day)]
