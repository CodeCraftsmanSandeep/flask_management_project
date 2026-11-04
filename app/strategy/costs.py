"""Indian index-option trading costs, with the rates that were in force on each date.

Charges are computed per *executed order* (a leg), on the traded value ``price x quantity``:

=================  ==========================================================================
Brokerage          flat rupees per order (Groww: Rs 20 per executed F&O order)
STT                % of premium, **sell side only**
Exchange charge    % of premium, both sides (NSE transaction charge incl. the IPFT part)
SEBI fee           Rs per crore of turnover, both sides
Stamp duty         % of premium, **buy side only**
GST                % of (brokerage + exchange charge + SEBI fee)
=================  ==========================================================================

Every component is rounded to paise (half up) per leg. Real contract notes round STT to the
whole rupee, so the model can differ from a broker statement by a few rupees per day.

Rates changed over time, so :class:`CostSchedule` picks the row effective on the trade date. Rows
whose ``verified`` flag is false are best-effort estimates; trades priced from them are flagged in
the UI instead of pretending to be exact.
"""

import hashlib
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

_PAISE = Decimal("0.01")
_HUNDRED = Decimal(100)
_CRORE = Decimal(10_000_000)


def paise(value: Decimal) -> Decimal:
    return value.quantize(_PAISE, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class Charges:
    brokerage: Decimal = Decimal(0)
    stt: Decimal = Decimal(0)
    exchange: Decimal = Decimal(0)
    sebi: Decimal = Decimal(0)
    stamp: Decimal = Decimal(0)
    gst: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst

    def __add__(self, other: "Charges") -> "Charges":
        return Charges(
            self.brokerage + other.brokerage,
            self.stt + other.stt,
            self.exchange + other.exchange,
            self.sebi + other.sebi,
            self.stamp + other.stamp,
            self.gst + other.gst,
        )


@dataclass(frozen=True, slots=True)
class CostRates:
    effective_from: date
    brokerage_per_order: Decimal  # rupees
    stt_sell_pct: Decimal  # percent: 0.15 means 0.15 %
    exchange_pct: Decimal
    sebi_per_crore: Decimal  # rupees per crore of turnover
    stamp_buy_pct: Decimal
    gst_pct: Decimal
    verified: bool
    source: str


class CostSchedule:
    """A dated list of :class:`CostRates`; the latest row not after the trade date applies."""

    def __init__(self, rows: Sequence[CostRates]) -> None:
        if not rows:
            msg = "A cost schedule needs at least one row."
            raise ValueError(msg)
        ordered = sorted(rows, key=lambda r: r.effective_from)
        if len({r.effective_from for r in ordered}) != len(ordered):
            msg = "Two cost rows share the same effective date."
            raise ValueError(msg)
        self._rows = ordered
        self._dates = [r.effective_from for r in ordered]

    @property
    def rows(self) -> list[CostRates]:
        return list(self._rows)

    def rates_on(self, day: date) -> CostRates:
        index = bisect_right(self._dates, day) - 1
        if index < 0:
            msg = f"No cost rates are defined for {day} (earliest is {self._dates[0]})."
            raise ValueError(msg)
        return self._rows[index]

    def leg_charges(self, day: date, *, sell: bool, price: Decimal, quantity: int) -> Charges:
        """Charges for one executed order of *quantity* units at *price*."""
        rates = self.rates_on(day)
        turnover = price * quantity
        brokerage = paise(rates.brokerage_per_order)
        exchange = paise(turnover * rates.exchange_pct / _HUNDRED)
        sebi = paise(turnover * rates.sebi_per_crore / _CRORE)
        return Charges(
            brokerage=brokerage,
            stt=paise(turnover * rates.stt_sell_pct / _HUNDRED) if sell else Decimal(0),
            exchange=exchange,
            sebi=sebi,
            stamp=Decimal(0) if sell else paise(turnover * rates.stamp_buy_pct / _HUNDRED),
            gst=paise((brokerage + exchange + sebi) * rates.gst_pct / _HUNDRED),
        )

    def fingerprint(self) -> str:
        """Changes whenever any rate changes, so cached results never outlive their costs."""
        text = "|".join(repr(r) for r in self._rows)
        return hashlib.sha256(text.encode()).hexdigest()[:12]


def _row(
    effective_from: date,
    stt: str,
    exchange: str,
    *,
    verified: bool,
    source: str,
    stamp: str = "0.003",
) -> CostRates:
    return CostRates(
        effective_from=effective_from,
        brokerage_per_order=Decimal(20),
        stt_sell_pct=Decimal(stt),
        exchange_pct=Decimal(exchange),
        sebi_per_crore=Decimal(10),
        stamp_buy_pct=Decimal(stamp),
        gst_pct=Decimal(18),
        verified=verified,
        source=source,
    )


DEFAULT_SCHEDULE = CostSchedule(
    [
        _row(
            date(2020, 1, 1),
            "0.05",
            "0.053",
            stamp="0.002",
            verified=False,
            source="Estimate. STT 0.05%, NSE option charge ~0.053%; check against contract notes.",
        ),
        _row(
            date(2020, 7, 1),
            "0.05",
            "0.053",
            verified=False,
            source="Estimate. Stamp duty 0.003% from 1 Jul 2020; exchange charge approximate.",
        ),
        _row(
            date(2023, 4, 1),
            "0.0625",
            "0.0495",
            verified=False,
            source="STT 0.0625% (Finance Act 2023); NSE charge (NSE/FA/56129) approximate.",
        ),
        _row(
            date(2024, 10, 1),
            "0.1",
            "0.03553",
            verified=True,
            source="STT 0.1% (Finance No.2 Act 2024); NSE 0.03503% + 0.0005% IPFT (NSE/FA/64232).",
        ),
        _row(
            date(2026, 4, 1),
            "0.15",
            "0.03553",
            verified=True,
            source="STT 0.15% (Finance Act 2026, NSE/FATAX/73524); NSE total same (NSE/FA/73061).",
        ),
    ]
)
