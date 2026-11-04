"""The filter bar: which trades of a cached run are shown, and how they are numbered.

The cached run is simulated once for the *widest* entry window (09:30-14:30). Narrower windows,
outcome, cross number, weekdays and date range are applied here, afterwards, which is what makes
the page instant. ``tests/test_engine_window.py`` proves a filtered widest run is identical to
simulating the narrower window directly.

**Weekdays only hide trades.** Every day is still simulated, because the EMAs run continuously
across days: leaving Mondays out shows what the strategy did on the other days, it does not
re-run it as if Mondays never happened.

**Entry numbers.** A trade's *entry #* is its rank among that mode's entries *on that day inside
the selected window*, so "1st cross" means "the first trade this window would have taken". It is
numbered before the outcome/cross filters so hiding a loser never renumbers the winners.
"""

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time
from typing import Literal

from app.errors import BadRequestError
from app.strategy.models import Mode, Trade

TIMEFRAMES = (5, 15, 30)
START_OPTIONS = (time(9, 30), time(10, 0), time(10, 30))
CUTOFF_OPTIONS = (
    time(11, 30),
    time(12, 0),
    time(12, 30),
    time(13, 0),
    time(13, 30),
    time(14, 0),
    time(14, 30),
)
MAX_CROSS_NO = 20
DAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # index = date.weekday()
ALL_DAYS = frozenset(range(7))
Outcome = Literal["all", "win", "loss", "tie"]
OUTCOMES: tuple[Outcome, ...] = ("all", "win", "loss", "tie")


class BadFilterError(BadRequestError):
    """The query string asked for something the app does not offer."""

    code = "bad_filter"


@dataclass(frozen=True, slots=True)
class Filters:
    timeframe: int = 15
    mode: Mode | None = None  # None = every mode
    outcome: Outcome = "all"
    start: time = START_OPTIONS[0]
    cutoff: time = CUTOFF_OPTIONS[-1]
    cross: int | None = None  # None = any entry number
    date_from: date | None = None
    date_to: date | None = None
    days: frozenset[int] = ALL_DAYS  # weekdays that may be traded, 0 = Monday

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            msg = f"timeframe must be one of {', '.join(map(str, TIMEFRAMES))}"
            raise BadFilterError(msg)
        if self.start not in START_OPTIONS:
            msg = "start must be one of " + ", ".join(t.strftime("%H:%M") for t in START_OPTIONS)
            raise BadFilterError(msg)
        if self.cutoff not in CUTOFF_OPTIONS:
            msg = "cutoff must be one of " + ", ".join(t.strftime("%H:%M") for t in CUTOFF_OPTIONS)
            raise BadFilterError(msg)
        if self.outcome not in OUTCOMES:
            msg = f"outcome must be one of {', '.join(OUTCOMES)}"
            raise BadFilterError(msg)
        if self.cross is not None and not 1 <= self.cross <= MAX_CROSS_NO:
            msg = f"cross must be between 1 and {MAX_CROSS_NO}"
            raise BadFilterError(msg)
        if not self.days or not self.days <= ALL_DAYS:
            msg = "days must name at least one of " + ", ".join(DAY_NAMES)
            raise BadFilterError(msg)
        if self.date_from and self.date_to and self.date_from > self.date_to:
            msg = "'from' must not be after 'to'"
            raise BadFilterError(msg)


def _time(value: str, name: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError:
        msg = f"{name} must look like 09:30"
        raise BadFilterError(msg) from None


def _day(value: str, name: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        msg = f"{name} must look like 2026-05-12"
        raise BadFilterError(msg) from None


def _days(value: str) -> frozenset[int]:
    """``mon,tue,thu`` -> {0, 1, 3}: exactly the weekdays named (special Saturday and Sunday
    sessions, such as budget day or Muhurat, are days of their own and are never implied)."""
    chosen: set[int] = set()
    for part in value.lower().split(","):
        name = part.strip()
        if name not in DAY_NAMES:
            msg = "days must be a comma-separated list of " + ", ".join(DAY_NAMES)
            raise BadFilterError(msg)
        chosen.add(DAY_NAMES.index(name))
    return frozenset(chosen)


def _int(value: str, name: str) -> int:
    try:
        return int(value)
    except ValueError:
        msg = f"{name} must be a whole number"
        raise BadFilterError(msg) from None


def parse_filters(args: Mapping[str, str]) -> Filters:
    """Build :class:`Filters` from a query string; empty values mean "use the default"."""
    get = {k: v.strip() for k, v in args.items() if v and v.strip()}
    defaults = Filters()
    mode = None
    if "mode" in get and get["mode"] != "all":
        try:
            mode = Mode(get["mode"])
        except ValueError:
            msg = "mode must be one of all, " + ", ".join(m.value for m in Mode)
            raise BadFilterError(msg) from None
    return Filters(
        timeframe=_int(get["timeframe"], "timeframe") if "timeframe" in get else defaults.timeframe,
        mode=mode,
        outcome=get.get("outcome", defaults.outcome),  # type: ignore[arg-type]
        start=_time(get["start"], "start") if "start" in get else defaults.start,
        cutoff=_time(get["cutoff"], "cutoff") if "cutoff" in get else defaults.cutoff,
        cross=_int(get["cross"], "cross") if "cross" in get and get["cross"] != "all" else None,
        days=_days(get["days"]) if "days" in get else defaults.days,
        date_from=_day(get["from"], "from") if "from" in get else None,
        date_to=_day(get["to"], "to") if "to" in get else None,
    )


@dataclass(frozen=True, slots=True)
class Selected:
    """A trade that passed the filters, with its place in the cached run and its entry number."""

    index: int  # position in the cached run: the stable id used by the detail endpoint
    trade: Trade
    entry_no: int


def outcome_of(trade: Trade) -> Outcome:
    if trade.net_pnl > 0:
        return "win"
    if trade.net_pnl < 0:
        return "loss"
    return "tie"


def select(
    trades: Sequence[Trade], filters: Filters, *, ignore_mode: bool = False
) -> list[Selected]:
    """Apply the filters. With *ignore_mode* the mode filter is skipped (for the compare table)."""
    in_window: list[tuple[int, Trade]] = []
    for index, trade in enumerate(trades):
        entry = trade.entry_decision.time()
        if not filters.start <= entry <= filters.cutoff:
            continue
        if trade.trade_date.weekday() not in filters.days:
            continue
        if filters.date_from and trade.trade_date < filters.date_from:
            continue
        if filters.date_to and trade.trade_date > filters.date_to:
            continue
        in_window.append((index, trade))

    counters: dict[tuple[Mode, date], int] = defaultdict(int)
    selected: list[Selected] = []
    for index, trade in in_window:  # the run is already in entry order
        key = (trade.mode, trade.trade_date)
        counters[key] += 1
        if filters.cross is not None and counters[key] != filters.cross:
            continue
        if filters.outcome != "all" and outcome_of(trade) != filters.outcome:
            continue
        if not ignore_mode and filters.mode is not None and trade.mode is not filters.mode:
            continue
        selected.append(Selected(index, trade, counters[key]))
    return selected
