from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from app.strategy.costs import DEFAULT_SCHEDULE, Charges, CostSchedule
from app.strategy.fills import TICK, buy_fill, sell_fill, to_tick
from app.strategy.models import Direction, Mode, OptionType, Side
from app.strategy.options import (
    atm_strike,
    expiry_from_symbol,
    groww_symbol,
    leg_for,
    pick_expiry,
)
from tests.builders import dec

D = Decimal

# --------------------------------------------------------------------------- strikes


@pytest.mark.parametrize(
    ("spot", "expected"),
    [
        ("24500", 24500),
        ("24524.99", 24500),
        ("24525", 24550),  # exactly halfway rounds UP
        ("24525.01", 24550),
        ("24549.99", 24550),
        ("24574.99", 24550),
        ("24575", 24600),  # halfway again
        ("24600.00", 24600),
        ("100", 100),
        ("24.99", 0),
    ],
)
def test_atm_rounds_to_nearest_fifty_with_ties_up(spot: str, expected: int) -> None:
    assert atm_strike(D(spot)) == expected


@pytest.mark.parametrize(
    ("mode", "direction", "side", "option_type", "strike"),
    [
        (Mode.ITM5_BUY, Direction.BULLISH, Side.BUY, OptionType.CE, 24150),
        (Mode.ITM5_BUY, Direction.BEARISH, Side.BUY, OptionType.PE, 24650),
        (Mode.ATM_BUY, Direction.BULLISH, Side.BUY, OptionType.CE, 24400),
        (Mode.ATM_BUY, Direction.BEARISH, Side.BUY, OptionType.PE, 24400),
        (Mode.ATM_SELL, Direction.BULLISH, Side.SELL, OptionType.PE, 24400),
        (Mode.ATM_SELL, Direction.BEARISH, Side.SELL, OptionType.CE, 24400),
    ],
)
def test_every_mode_and_direction_maps_to_the_documented_leg(
    mode: Mode, direction: Direction, side: Side, option_type: OptionType, strike: int
) -> None:
    leg = leg_for(mode, direction, 24400)
    assert (leg.side, leg.option_type, leg.strike) == (side, option_type, strike)


def test_itm5_is_exactly_five_strikes_in_the_money() -> None:
    call = leg_for(Mode.ITM5_BUY, Direction.BULLISH, 24400)
    put = leg_for(Mode.ITM5_BUY, Direction.BEARISH, 24400)
    assert 24400 - call.strike == 5 * 50  # a call is in the money below spot
    assert put.strike - 24400 == 5 * 50  # a put is in the money above spot


def test_symbol_format_matches_groww() -> None:
    assert groww_symbol(date(2025, 1, 2), 28500, OptionType.PE) == "NSE-NIFTY-02Jan25-28500-PE"
    assert groww_symbol(date(2025, 9, 30), 24650, OptionType.CE) == "NSE-NIFTY-30Sep25-24650-CE"


def test_symbol_months_are_fixed_english_names() -> None:
    names = [
        groww_symbol(date(2026, m, 5), 24000, OptionType.CE).split("-")[2] for m in range(1, 13)
    ]
    assert names == [
        f"05{m}26"
        for m in (
            "Jan",
            "Feb",
            "Mar",
            "Apr",
            "May",
            "Jun",
            "Jul",
            "Aug",
            "Sep",
            "Oct",
            "Nov",
            "Dec",
        )
    ]


# --------------------------------------------------------------------------- expiry

EXPIRIES = [date(2026, 5, 5), date(2026, 5, 12), date(2026, 5, 19)]


def test_nearest_expiry_on_or_after_the_trade_date() -> None:
    assert pick_expiry(date(2026, 5, 4), EXPIRIES) == date(2026, 5, 5)
    assert pick_expiry(date(2026, 5, 6), EXPIRIES) == date(2026, 5, 12)


def test_expiry_day_itself_is_allowed_by_default() -> None:
    assert pick_expiry(date(2026, 5, 12), EXPIRIES) == date(2026, 5, 12)


def test_skip_expiry_day_rolls_to_the_next_expiry() -> None:
    assert pick_expiry(date(2026, 5, 12), EXPIRIES, skip_expiry_day=True) == date(2026, 5, 19)
    # ...but only on expiry day itself
    assert pick_expiry(date(2026, 5, 11), EXPIRIES, skip_expiry_day=True) == date(2026, 5, 12)


def test_no_expiry_left_returns_none() -> None:
    assert pick_expiry(date(2026, 5, 20), EXPIRIES) is None
    assert pick_expiry(date(2026, 5, 19), EXPIRIES, skip_expiry_day=True) is None
    assert pick_expiry(date(2026, 5, 1), []) is None


# --------------------------------------------------------------------------- fills


def test_to_tick_rounds_in_the_requested_direction() -> None:
    assert to_tick(D("100.07"), up=True) == D("100.10")
    assert to_tick(D("100.07"), up=False) == D("100.05")
    assert to_tick(D("100.10"), up=True) == D("100.10")  # already on a tick: unchanged
    assert to_tick(D("100.10"), up=False) == D("100.10")


def test_buy_pays_half_a_percent_plus_points() -> None:
    assert buy_fill(D("100"), D("0.005"), D("0.5")) == D("101.00")  # 100.5 + 0.5
    assert buy_fill(D("210"), D("0.005"), D("0.5")) == D("211.55")  # 211.05 + 0.5


def test_sell_receives_half_a_percent_minus_points() -> None:
    assert sell_fill(D("100"), D("0.005"), D("0.5")) == D("99.00")  # 99.5 - 0.5
    assert sell_fill(D("190"), D("0.005"), D("0.5")) == D("188.55")  # 189.05 - 0.5


def test_buys_round_up_and_sells_round_down_to_the_tick() -> None:
    assert buy_fill(D("100.10"), D("0.005"), D("0.5")) == D("101.15")  # 101.1005 -> 101.15
    assert sell_fill(D("100.10"), D("0.005"), D("0.5")) == D("99.05")  # 99.0995 -> 99.05


def test_sell_price_is_floored_at_the_tick() -> None:
    assert sell_fill(D("0.30"), D("0.005"), D("0.5")) == TICK  # would be negative
    assert sell_fill(D("0.05"), D("0.005"), D("0.5")) == TICK
    assert sell_fill(D("0.55"), D("0.005"), D("0.5")) == TICK  # 0.047 -> floored to 0.05


def test_zero_slippage_only_snaps_to_the_tick() -> None:
    assert buy_fill(D("100.07"), D(0), D(0)) == D("100.10")
    assert sell_fill(D("100.07"), D(0), D(0)) == D("100.05")
    assert buy_fill(D("100.05"), D(0), D(0)) == D("100.05")


def test_slippage_never_helps_the_trader() -> None:
    for raw in ("0.05", "1.00", "37.35", "100.07", "250.50", "1999.95"):
        price = D(raw)
        assert buy_fill(price, D("0.005"), D("0.5")) > price
        assert sell_fill(price, D("0.005"), D("0.5")) <= max(price, TICK)


# --------------------------------------------------------------------------- costs

MAY_2026 = date(2026, 5, 12)
DEC_2025 = date(2025, 12, 1)


def test_buy_leg_pays_stamp_duty_but_no_stt() -> None:
    c = DEFAULT_SCHEDULE.leg_charges(MAY_2026, sell=False, price=D("100.00"), quantity=65)
    # turnover 6500: exchange 0.03553% = 2.30945 -> 2.31 ; sebi 0.0065 -> 0.01 ;
    # stamp 0.003% = 0.195 -> 0.20 (half up) ; gst 18% of (20 + 2.31 + 0.01) = 4.0176 -> 4.02
    assert c == Charges(
        brokerage=D("20"),
        stt=D("0"),
        exchange=D("2.31"),
        sebi=D("0.01"),
        stamp=D("0.20"),
        gst=D("4.02"),
    )
    assert c.total == D("26.54")


def test_sell_leg_pays_stt_but_no_stamp_duty() -> None:
    c = DEFAULT_SCHEDULE.leg_charges(MAY_2026, sell=True, price=D("150.00"), quantity=65)
    # turnover 9750: stt 0.15% = 14.625 -> 14.63 ; exchange 3.464175 -> 3.46 ; sebi 0.00975 -> 0.01
    # gst 18% of (20 + 3.46 + 0.01) = 4.2246 -> 4.22
    assert c == Charges(
        brokerage=D("20"),
        stt=D("14.63"),
        exchange=D("3.46"),
        sebi=D("0.01"),
        stamp=D("0"),
        gst=D("4.22"),
    )
    assert c.total == D("42.32")


def test_stt_was_a_tenth_of_a_percent_before_april_2026() -> None:
    c = DEFAULT_SCHEDULE.leg_charges(DEC_2025, sell=True, price=D("150.00"), quantity=65)
    assert c.stt == D("9.75")  # 0.10% of 9750
    assert c.total == D("37.44")


def test_rate_changes_take_effect_on_the_exact_date() -> None:
    before = DEFAULT_SCHEDULE.rates_on(date(2026, 3, 31))
    after = DEFAULT_SCHEDULE.rates_on(date(2026, 4, 1))
    assert (before.stt_sell_pct, after.stt_sell_pct) == (D("0.1"), D("0.15"))
    assert DEFAULT_SCHEDULE.rates_on(date(2024, 9, 30)).stt_sell_pct == D("0.0625")
    assert DEFAULT_SCHEDULE.rates_on(date(2024, 10, 1)).stt_sell_pct == D("0.1")


def test_only_rows_backed_by_a_source_are_marked_verified() -> None:
    assert DEFAULT_SCHEDULE.rates_on(date(2024, 10, 1)).verified
    assert DEFAULT_SCHEDULE.rates_on(MAY_2026).verified
    assert not DEFAULT_SCHEDULE.rates_on(date(2022, 1, 3)).verified
    assert not DEFAULT_SCHEDULE.rates_on(date(2023, 6, 1)).verified


def test_dates_before_the_first_row_are_an_error_not_a_guess() -> None:
    with pytest.raises(ValueError, match="No cost rates"):
        DEFAULT_SCHEDULE.rates_on(date(2019, 12, 31))


def test_schedule_rejects_empty_and_duplicate_dates() -> None:
    with pytest.raises(ValueError, match="at least one"):
        CostSchedule([])
    row = DEFAULT_SCHEDULE.rows[0]
    with pytest.raises(ValueError, match="same effective date"):
        CostSchedule([row, row])


def test_fingerprint_changes_when_any_rate_changes() -> None:
    rows = DEFAULT_SCHEDULE.rows
    tweaked = [*rows[:-1], replace(rows[-1], brokerage_per_order=D(21))]
    assert CostSchedule(rows).fingerprint() == DEFAULT_SCHEDULE.fingerprint()
    assert CostSchedule(tweaked).fingerprint() != DEFAULT_SCHEDULE.fingerprint()


def test_charges_add_component_wise() -> None:
    a = Charges(D(1), D(2), D(3), D(4), D(5), D(6))
    b = Charges(D(10), D(20), D(30), D(40), D(50), D(60))
    assert (a + b) == Charges(D(11), D(22), D(33), D(44), D(55), D(66))
    assert (a + b).total == D(231)


def test_gst_applies_to_brokerage_exchange_and_sebi_only() -> None:
    c = DEFAULT_SCHEDULE.leg_charges(MAY_2026, sell=True, price=D("1000.00"), quantity=65)
    taxable = c.brokerage + c.exchange + c.sebi
    assert c.gst == (taxable * D("0.18")).quantize(D("0.01"))
    assert c.gst != ((taxable + c.stt) * D("0.18")).quantize(D("0.01"))


def test_dec_helper_keeps_decimal_exact() -> None:
    assert dec(0.1) == D("0.1")


@pytest.mark.parametrize(
    "day", [date(2025, 1, 2), date(2026, 12, 31), date(2026, 2, 3), date(2030, 9, 30)]
)
@pytest.mark.parametrize("option_type", list(OptionType))
def test_the_expiry_in_a_symbol_round_trips(day: date, option_type: OptionType) -> None:
    assert expiry_from_symbol(groww_symbol(day, 24000, option_type)) == day


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "NSE-NIFTY",
        "NSE-NIFTY-24000-CE",
        "NSE-NIFTY-12Foo26-24000-CE",
        "NSE-NIFTY-32May26-24000-CE",
        "NSE-NIFTY-12May2-24000-CE",
        "NSE-NIFTY-xxMay26-24000-CE",
        "NSE-NIFTY-12May26-24000-CE-extra",
    ],
)
def test_malformed_symbols_are_rejected(bad: str) -> None:
    with pytest.raises(ValueError, match="not an option symbol"):
        expiry_from_symbol(bad)
