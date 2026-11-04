"""Golden-scenario tests for the simulation engine.

The scenario (5-minute bars, EMA 1/3 so every crossover can be checked by hand):

    bar  0-2   24000            fast == slow: ties, no sign yet
    bar  3-4   24200            first non-zero sign (+) is NOT a cross
    bar  5-6   23800  -> BEARISH cross, bar 5 closes 09:45   (slow = 23975 > 23800)
    bar  7-39  24400  -> BULLISH cross, bar 7 closes 09:55   (slow = 24143.75 < 24400)
    bar 40-74  24000  -> BEARISH cross, bar 40 closes 12:40  (slow = 24200 > 24000)

so three trades per mode: 09:45-09:55 (whipsaw), 09:55-12:40, 12:40-15:00 (square-off).
"""

import itertools
from datetime import date, time
from decimal import ROUND_HALF_UP, Decimal

import pytest

from app.strategy.costs import Charges
from app.strategy.engine import (
    BacktestResult,
    StrategyParams,
    analyse_spot,
    is_entry_candidate,
    required_contracts,
    run_backtest,
)
from app.strategy.models import (
    Candle,
    Cross,
    Direction,
    ExitReason,
    Mode,
    SkipReason,
    Trade,
)
from tests.builders import DictBook, FullBook, Price, at, dec, piecewise, spot_day

D = Decimal
DAY = date(2026, 5, 12)  # a Tuesday, and an expiry day
EXPIRIES = [date(2026, 5, 12), date(2026, 5, 19)]
PARAMS = StrategyParams(timeframe=5, fast=1, slow=3)
GOLDEN_CLOSES = piecewise({0: 24000, 3: 24200, 5: 23800, 7: 24400, 40: 24000})

S = "NSE-NIFTY-12May26"
GOLDEN_PRICES: dict[str, dict[str, float]] = {
    # T1: bearish 09:45 -> 09:55
    f"{S}-24050-PE": {"09:45": 210.00, "09:55": 190.00},  # ITM5 buy (ATM 23800 + 250)
    f"{S}-23800-PE": {"09:45": 150.00, "09:55": 140.00},  # ATM buy
    f"{S}-23800-CE": {"09:45": 120.00, "09:55": 100.00},  # ATM sell
    # T2: bullish 09:55 -> 12:40
    f"{S}-24150-CE": {"09:55": 300.00, "12:40": 380.00},  # ITM5 buy (ATM 24400 - 250)
    f"{S}-24400-CE": {"09:55": 120.00, "12:40": 160.00},  # ATM buy
    f"{S}-24400-PE": {"09:55": 110.00, "12:40": 70.00},  # ATM sell
    # T3: bearish 12:40 -> 15:00 square-off
    f"{S}-24250-PE": {"12:40": 260.00, "15:00": 240.00},  # ITM5 buy (ATM 24000 + 250)
    f"{S}-24000-PE": {"12:40": 90.00, "15:00": 85.00},  # ATM buy
    f"{S}-24000-CE": {"12:40": 100.00, "15:00": 120.00},  # ATM sell
}


def golden() -> BacktestResult:
    spot = spot_day(DAY, GOLDEN_CLOSES)
    return run_backtest(spot, EXPIRIES, DictBook(DAY, GOLDEN_PRICES), PARAMS)


def trade(result: BacktestResult, mode: Mode, entry: str) -> Trade:
    (found,) = [t for t in result.trades if t.mode is mode and t.entry_decision == at(DAY, entry)]
    return found


def ref_leg_total(price: str, *, sell: bool, stt_pct: str = "0.15", qty: int = 65) -> Decimal:
    """Independent re-statement of the cost rules, written out longhand for the tests."""

    def q(x: Decimal) -> Decimal:
        return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)

    turnover = D(price) * qty
    brokerage = D(20)
    exchange = q(turnover * D("0.03553") / 100)
    sebi = q(turnover * 10 / D(10_000_000))
    stt = q(turnover * D(stt_pct) / 100) if sell else D(0)
    stamp = D(0) if sell else q(turnover * D("0.003") / 100)
    gst = q((brokerage + exchange + sebi) * 18 / 100)
    return brokerage + stt + exchange + sebi + stamp + gst


# --------------------------------------------------------------------------- signals


def test_the_scenario_produces_exactly_the_three_designed_crosses() -> None:
    crosses = analyse_spot(spot_day(DAY, GOLDEN_CLOSES), PARAMS).crosses
    assert [(c.decision_time, c.direction, c.close) for c in crosses] == [
        (at(DAY, "09:45"), Direction.BEARISH, dec(23800)),
        (at(DAY, "09:55"), Direction.BULLISH, dec(24400)),
        (at(DAY, "12:40"), Direction.BEARISH, dec(24000)),
    ]
    assert [c.day_no for c in crosses] == [1, 2, 3]


def test_each_mode_trades_every_cross_once() -> None:
    result = golden()
    assert len(result.trades) == 9
    assert result.skips == []
    for mode in Mode:
        assert [t.entry_decision for t in result.trades if t.mode is mode] == [
            at(DAY, "09:45"),
            at(DAY, "09:55"),
            at(DAY, "12:40"),
        ]


# --------------------------------------------------------------------------- contracts


def test_contracts_for_every_mode_and_cross() -> None:
    result = golden()
    got = {
        (t.mode, t.entry_decision.strftime("%H:%M")): (t.symbol, t.side.value)
        for t in result.trades
    }
    assert got == {
        (Mode.ITM5_BUY, "09:45"): (f"{S}-24050-PE", "BUY"),
        (Mode.ATM_BUY, "09:45"): (f"{S}-23800-PE", "BUY"),
        (Mode.ATM_SELL, "09:45"): (f"{S}-23800-CE", "SELL"),
        (Mode.ITM5_BUY, "09:55"): (f"{S}-24150-CE", "BUY"),
        (Mode.ATM_BUY, "09:55"): (f"{S}-24400-CE", "BUY"),
        (Mode.ATM_SELL, "09:55"): (f"{S}-24400-PE", "SELL"),
        (Mode.ITM5_BUY, "12:40"): (f"{S}-24250-PE", "BUY"),
        (Mode.ATM_BUY, "12:40"): (f"{S}-24000-PE", "BUY"),
        (Mode.ATM_SELL, "12:40"): (f"{S}-24000-CE", "SELL"),
    }


def test_expiry_day_is_used_by_default_with_zero_dte() -> None:
    assert {(t.expiry, t.dte) for t in golden().trades} == {(date(2026, 5, 12), 0)}


def test_skip_expiry_day_uses_the_following_expiry() -> None:
    params = StrategyParams(timeframe=5, fast=1, slow=3, skip_expiry_day=True)
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, FullBook(), params)
    assert len(result.trades) == 9
    assert {(t.expiry, t.dte) for t in result.trades} == {(date(2026, 5, 19), 7)}
    assert all("19May26" in t.symbol for t in result.trades)


def test_strike_is_based_on_the_signal_bar_close_not_the_fill_minute() -> None:
    # Bar 8 (09:55-10:00, where the bullish fill happens) trades at 24430, but the cross bar
    # (bar 7) closed at 24400, so the ATM strike must stay 24400.
    closes = piecewise({0: 24000, 3: 24200, 5: 23800, 7: 24400, 8: 24430, 9: 24400, 40: 24000})
    result = run_backtest(spot_day(DAY, closes), EXPIRIES, FullBook(), PARAMS)
    bullish = [
        t for t in result.trades if t.direction is Direction.BULLISH and t.mode is Mode.ATM_BUY
    ]
    (t,) = bullish
    assert t.strike == 24400
    assert t.spot_entry == dec(24430)


def test_required_contracts_lists_exactly_the_symbols_the_trades_use() -> None:
    analysis = analyse_spot(spot_day(DAY, GOLDEN_CLOSES), PARAMS)
    needed = required_contracts(analysis, EXPIRIES, PARAMS)
    assert needed == {DAY: set(GOLDEN_PRICES)}
    assert required_contracts(analysis, EXPIRIES, PARAMS, modes=[Mode.ATM_SELL]) == {
        DAY: {f"{S}-23800-CE", f"{S}-24400-PE", f"{S}-24000-CE"}
    }
    assert required_contracts(analysis, EXPIRIES, PARAMS, start=date(2026, 5, 13)) == {}
    assert required_contracts(analysis, [], PARAMS) == {}


# --------------------------------------------------------------------------- fills & money


def test_deep_itm_buy_golden_trade_every_number() -> None:
    t = trade(golden(), Mode.ITM5_BUY, "09:45")
    assert (t.option_entry_raw, t.option_exit_raw) == (D("210.00"), D("190.00"))
    # buy: 210 * 1.005 + 0.5 = 211.55 ; sell: 190 * 0.995 - 0.5 = 188.55
    assert (t.option_entry_fill, t.option_exit_fill) == (D("211.55"), D("188.55"))
    assert t.gross_pnl == D("-1300.00")  # (190 - 210) * 65
    assert t.slippage == D("195.00")  # ((211.55 - 210) + (190 - 188.55)) * 65
    assert t.charges == Charges(
        brokerage=D("40"),
        stt=D("18.38"),
        exchange=D("9.24"),
        sebi=D("0.02"),
        stamp=D("0.41"),
        gst=D("8.86"),
    )
    assert t.charges.total == D("76.91")
    assert t.net_pnl == D("-1571.91")
    assert t.quantity == 65


def test_atm_sell_golden_trade_every_number() -> None:
    t = trade(golden(), Mode.ATM_SELL, "09:45")
    assert (t.option_entry_raw, t.option_exit_raw) == (D("120.00"), D("100.00"))
    # sell first: 120 * 0.995 - 0.5 = 118.90 ; buy back: 100 * 1.005 + 0.5 = 101.00
    assert (t.option_entry_fill, t.option_exit_fill) == (D("118.90"), D("101.00"))
    assert t.gross_pnl == D("1300.00")  # (120 - 100) * 65: a short gains when the price falls
    assert t.slippage == D("136.50")  # ((120 - 118.90) + (101 - 100)) * 65
    assert t.charges == Charges(
        brokerage=D("40"),
        stt=D("11.59"),  # STT is on the SELL leg, which is the entry here
        exchange=D("5.08"),
        sebi=D("0.02"),
        stamp=D("0.20"),  # stamp duty is on the BUY leg, which is the exit here
        gst=D("8.12"),
    )
    assert t.net_pnl == D("1098.49")


def test_every_trade_balances_and_matches_the_longhand_cost_rules() -> None:
    for t in golden().trades:
        assert t.net_pnl == t.gross_pnl - t.slippage - t.charges.total
        buying = t.side.value == "BUY"
        expected = ref_leg_total(str(t.option_entry_fill), sell=not buying) + ref_leg_total(
            str(t.option_exit_fill), sell=buying
        )
        assert t.charges.total == expected
        # fills never beat the raw candle price, on either leg
        if buying:
            assert t.option_entry_fill >= t.option_entry_raw
            assert t.option_exit_fill <= t.option_exit_raw
        else:
            assert t.option_entry_fill <= t.option_entry_raw
            assert t.option_exit_fill >= t.option_exit_raw
        assert t.slippage > 0


def test_gross_follows_the_option_side() -> None:
    result = golden()
    long_t = trade(result, Mode.ATM_BUY, "09:55")  # 120 -> 160 on a bought call
    short_t = trade(result, Mode.ATM_SELL, "12:40")  # sold CE 100 -> 120: price rose, short loses
    assert long_t.gross_pnl == D("2600.00")  # +40 * 65
    assert short_t.gross_pnl == D("-1300.00")  # -20 * 65


def test_spot_entry_and_exit_are_the_open_of_the_fill_minute() -> None:
    result = golden()
    t1, t2, t3 = (trade(result, Mode.ATM_BUY, x) for x in ("09:45", "09:55", "12:40"))
    # the 09:45 candle belongs to bar 6 (23800); 09:55 to bar 8 (24400); 12:40 to bar 41 (24000)
    assert (t1.spot_entry, t1.spot_exit) == (dec(23800), dec(24400))
    assert (t2.spot_entry, t2.spot_exit) == (dec(24400), dec(24000))
    assert (t3.spot_entry, t3.spot_exit) == (dec(24000), dec(24000))


# --------------------------------------------------------------------------- exits & reversal


def test_opposite_cross_reverses_in_the_same_minute() -> None:
    result = golden()
    for mode in Mode:
        first, second, third = (trade(result, mode, x) for x in ("09:45", "09:55", "12:40"))
        assert first.exit_reason is ExitReason.OPPOSITE_CROSS
        assert first.exit_decision == second.entry_decision == at(DAY, "09:55")
        assert first.exit_fill == second.entry_fill == at(DAY, "09:55")
        assert second.exit_fill == third.entry_fill == at(DAY, "12:40")
        assert first.direction is not second.direction


def test_the_last_trade_is_squared_off_at_1500() -> None:
    last = trade(golden(), Mode.ATM_BUY, "12:40")
    assert last.exit_reason is ExitReason.SQUARE_OFF
    assert last.exit_decision == last.exit_fill == at(DAY, "15:00")
    assert not last.stale_exit
    assert last.option_exit_raw == D("85.00")  # the OPEN of the 15:00 candle


def test_whipsaw_badge_marks_quick_reversals_only() -> None:
    result = golden()
    assert trade(result, Mode.ATM_BUY, "09:45").whipsaw  # 10 minutes = 2 bars
    assert not trade(result, Mode.ATM_BUY, "09:55").whipsaw
    assert not trade(result, Mode.ATM_BUY, "12:40").whipsaw  # square-off is never a whipsaw


def test_one_position_at_a_time_per_mode() -> None:
    result = golden()
    for mode in Mode:
        mine = sorted((t for t in result.trades if t.mode is mode), key=lambda t: t.entry_fill)
        for a, b in itertools.pairwise(mine):
            assert b.entry_fill >= a.exit_fill


def test_trades_are_sorted_by_entry_time() -> None:
    entries = [t.entry_fill for t in golden().trades]
    assert entries == sorted(entries)


# --------------------------------------------------------------------------- 15:00 and window edges


def test_a_cross_at_exactly_1500_is_a_square_off_not_a_reversal() -> None:
    # bearish cross at 11:00 (bar 20), bullish cross whose bar closes exactly at 15:00 (bar 68)
    closes = piecewise({0: 24000, 3: 24200, 20: 23800, 68: 24400})
    result = run_backtest(spot_day(DAY, closes), EXPIRIES, FullBook(), PARAMS)
    crosses = analyse_spot(spot_day(DAY, closes), PARAMS).crosses
    assert [c.decision_time for c in crosses] == [at(DAY, "11:00"), at(DAY, "15:00")]
    assert len(result.trades) == 3  # one per mode, no reverse trade
    assert {t.exit_reason for t in result.trades} == {ExitReason.SQUARE_OFF}
    assert {t.exit_decision for t in result.trades} == {at(DAY, "15:00")}


def test_a_cross_at_1455_closes_the_trade_but_opens_nothing() -> None:
    # same, one bar earlier: the 14:55 cross is a real opposite cross, but outside the window
    closes = piecewise({0: 24000, 3: 24200, 20: 23800, 67: 24400})
    result = run_backtest(spot_day(DAY, closes), EXPIRIES, FullBook(), PARAMS)
    assert len(result.trades) == 3
    assert {t.exit_reason for t in result.trades} == {ExitReason.OPPOSITE_CROSS}
    assert {t.exit_decision for t in result.trades} == {at(DAY, "14:55")}


def test_entry_window_edges_are_inclusive() -> None:
    def cross(hhmm: str) -> Cross:
        return Cross(at(DAY, hhmm), at(DAY, hhmm), Direction.BULLISH, dec(1), 0.0, 0.0, 1)

    assert not is_entry_candidate(cross("09:25"), PARAMS)
    assert is_entry_candidate(cross("09:30"), PARAMS)
    assert is_entry_candidate(cross("14:30"), PARAMS)
    assert not is_entry_candidate(cross("14:35"), PARAMS)


def test_a_cross_at_1430_enters_and_one_at_1435_does_not() -> None:
    at_1430 = piecewise({0: 24000, 3: 24200, 62: 23800})  # bar 62 closes 14:30
    at_1435 = piecewise({0: 24000, 3: 24200, 63: 23800})  # bar 63 closes 14:35
    entered = run_backtest(spot_day(DAY, at_1430), EXPIRIES, FullBook(), PARAMS)
    assert {t.entry_decision for t in entered.trades} == {at(DAY, "14:30")}
    not_entered = run_backtest(spot_day(DAY, at_1435), EXPIRIES, FullBook(), PARAMS)
    assert not_entered.trades == []
    assert [d.kind for d in not_entered.days] == ["no_signal"]


def test_a_cross_before_the_window_does_not_carry_into_a_trade_later() -> None:
    # Only cross is bearish at 09:30 with the window opening at 10:00: no trade at all.
    closes = piecewise({0: 24000, 1: 24200, 2: 23800})
    params = StrategyParams(timeframe=5, fast=1, slow=3, earliest_entry=time(10, 0))
    result = run_backtest(spot_day(DAY, closes), EXPIRIES, FullBook(), params)
    assert result.trades == []
    assert result.days[0].kind == "no_signal"


# --------------------------------------------------------------------------- timeframes


@pytest.mark.parametrize(
    ("timeframe", "decision"),
    [(5, "09:30"), (15, "10:00"), (30, "10:45")],
)
def test_decision_time_is_the_close_of_the_anchored_bar(timeframe: int, decision: str) -> None:
    bars = -(-375 // timeframe)
    # bar 2 flips the sign relative to bar 1; bar 2 ends 09:15 + 3 * timeframe
    closes = piecewise({0: 24000, 1: 24200, 2: 23800}, bars=bars)
    params = StrategyParams(timeframe=timeframe, fast=1, slow=3, earliest_entry=time(9, 30))
    result = run_backtest(spot_day(DAY, closes, timeframe), EXPIRIES, FullBook(), params)
    assert {t.entry_decision for t in result.trades} == {at(DAY, decision)}
    assert {t.timeframe for t in result.trades} == {timeframe}


# --------------------------------------------------------------------------- missing data


def test_missing_fill_candle_uses_the_next_candle_within_the_wait() -> None:
    prices = {k: dict(v) for k, v in GOLDEN_PRICES.items()}
    del prices[f"{S}-23800-PE"]["09:45"]
    prices[f"{S}-23800-PE"]["09:47"] = 155.0
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), PARAMS)
    t = trade(result, Mode.ATM_BUY, "09:45")
    assert t.entry_decision == at(DAY, "09:45")
    assert t.entry_fill == at(DAY, "09:47")  # the actual fill time is recorded
    assert t.option_entry_raw == D("155.00")


def test_no_candle_within_the_wait_skips_that_mode_only() -> None:
    prices = {k: dict(v) for k, v in GOLDEN_PRICES.items()}
    prices[f"{S}-23800-PE"] = {"09:51": 150.0, "09:55": 140.0}  # 6 minutes late: too late
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), PARAMS)
    assert len(result.trades) == 8
    (skip,) = result.skips
    assert (skip.mode, skip.reason, skip.decision_time) == (
        Mode.ATM_BUY,
        SkipReason.NO_OPTION_DATA,
        at(DAY, "09:45"),
    )
    assert len([t for t in result.trades if t.mode is Mode.ITM5_BUY]) == 3  # others untouched


def test_an_entry_that_would_not_precede_its_exit_is_skipped() -> None:
    prices = {k: dict(v) for k, v in GOLDEN_PRICES.items()}
    prices[f"{S}-23800-PE"] = {"09:55": 140.0}  # first candle only at the exit minute
    patient = StrategyParams(timeframe=5, fast=1, slow=3, fill_wait_minutes=10)  # would reach it
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), patient)
    assert [(s.mode, s.reason) for s in result.skips] == [(Mode.ATM_BUY, SkipReason.NO_OPTION_DATA)]


def test_missing_exit_candle_falls_back_to_the_last_close_and_is_flagged() -> None:
    prices = {k: dict(v) for k, v in GOLDEN_PRICES.items()}
    prices[f"{S}-23800-PE"] = {"09:45": 150.0, "09:50": 145.0}  # nothing at/after 09:55
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), PARAMS)
    t = trade(result, Mode.ATM_BUY, "09:45")
    assert t.stale_exit
    assert t.option_exit_raw == D("145.00")  # close of the last candle before the exit
    assert t.exit_fill == t.exit_decision == at(DAY, "09:55")
    assert not trade(result, Mode.ITM5_BUY, "09:45").stale_exit


def test_fills_read_the_open_of_the_candle_and_a_stale_exit_reads_the_close() -> None:
    prices: dict[str, dict[str, Price]] = {k: dict(v) for k, v in GOLDEN_PRICES.items()}
    # entry candle 09:45: open 150, close 160 ; exit candle 09:55: open 140, close 130
    prices[f"{S}-23800-PE"] = {"09:45": (150, 160), "09:55": (140, 130)}
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), PARAMS)
    t = trade(result, Mode.ATM_BUY, "09:45")
    assert (t.option_entry_raw, t.option_exit_raw) == (D(150), D(140))  # opens, not closes
    assert not t.stale_exit

    # no candle at/after 09:55: fall back to the CLOSE of the last candle before the exit
    prices[f"{S}-23800-PE"] = {"09:45": (150, 160), "09:50": (146, 143)}
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), PARAMS)
    t = trade(result, Mode.ATM_BUY, "09:45")
    assert t.stale_exit
    assert (t.option_entry_raw, t.option_exit_raw) == (D(150), D(143))


def test_a_price_below_the_tick_is_skipped_as_bad_data() -> None:
    prices = {k: dict(v) for k, v in GOLDEN_PRICES.items()}
    prices[f"{S}-23800-PE"]["09:45"] = 0.04
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, prices), PARAMS)
    assert [(s.mode, s.reason) for s in result.skips] == [(Mode.ATM_BUY, SkipReason.BAD_PRICE)]


def test_no_listed_expiry_skips_every_candidate_and_says_why() -> None:
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), [], FullBook(), PARAMS)
    assert result.trades == []
    assert len(result.skips) == 9
    assert {s.reason for s in result.skips} == {SkipReason.NO_EXPIRY}


def test_every_skip_is_recorded_not_dropped() -> None:
    result = run_backtest(spot_day(DAY, GOLDEN_CLOSES), EXPIRIES, DictBook(DAY, {}), PARAMS)
    assert result.trades == []
    assert len(result.skips) == 9
    assert {(s.mode, s.decision_time.strftime("%H:%M")) for s in result.skips} == {
        (m, x) for m in Mode for x in ("09:45", "09:55", "12:40")
    }


# --------------------------------------------------------------------------- days


def test_flat_day_is_reported_as_a_no_signal_day() -> None:
    result = run_backtest(spot_day(DAY, [24000.0] * 75), EXPIRIES, FullBook(), PARAMS)
    assert result.trades == []
    assert [(d.day, d.kind) for d in result.days] == [(DAY, "no_signal")]


def test_an_incomplete_day_is_excluded_reported_and_not_used_for_emas() -> None:
    day1, day2 = date(2026, 5, 11), DAY
    spot1 = spot_day(day1, GOLDEN_CLOSES)
    del spot1[100]  # one missing minute
    spot2 = spot_day(day2, GOLDEN_CLOSES)
    result = run_backtest(spot1 + spot2, EXPIRIES, FullBook(), PARAMS)
    assert [(d.day, d.kind) for d in result.days] == [(day1, "incomplete_spot"), (day2, "ok")]
    assert result.days[0].detail == "374/375 minutes"
    assert {t.trade_date for t in result.trades} == {day2}
    # the broken day never reaches the EMA series: day 2's EMAs start from day 2's first close
    first = result.analysis.signal_bars[0]
    assert first.start == at(day2, "09:15")
    assert first.ema_fast == first.ema_slow == 24000.0


def test_days_before_start_warm_up_the_emas_but_are_not_traded() -> None:
    day1, day2 = date(2026, 5, 11), DAY
    # day 1 ends high (sign +). Day 2 opens low: a cross on the very first bar, from yesterday.
    spot1 = spot_day(day1, piecewise({0: 24000, 3: 24200}))
    spot2 = spot_day(day2, piecewise({0: 23000}))
    book = FullBook()
    traded = run_backtest(spot1 + spot2, EXPIRIES, book, PARAMS, start=day2)
    assert [d.day for d in traded.days] == [day2]
    assert traded.trades == []  # the carried-over cross is at 09:20, before the 09:30 window
    assert traded.analysis.crosses[0].decision_time == at(day2, "09:20")
    assert any(b.start.date() == day1 for b in traded.analysis.signal_bars)


def test_end_date_excludes_later_days() -> None:
    day1, day2 = date(2026, 5, 11), DAY
    spot = spot_day(day1, GOLDEN_CLOSES) + spot_day(day2, GOLDEN_CLOSES)
    result = run_backtest(
        spot, [date(2026, 5, 12), date(2026, 5, 19)], FullBook(), PARAMS, end=day1
    )
    assert {t.trade_date for t in result.trades} == {day1}
    assert {d.day for d in result.days} == {day1}


def test_costs_use_the_rates_in_force_on_the_trade_date() -> None:
    old_day = date(2022, 3, 1)
    result = run_backtest(spot_day(old_day, GOLDEN_CLOSES), [date(2022, 3, 3)], FullBook(), PARAMS)
    assert result.trades
    assert {t.cost_verified for t in result.trades} == {False}  # 2022 rates are estimates
    assert {t.cost_verified for t in golden().trades} == {True}


# --------------------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "kwargs",
    [
        {"timeframe": 0},
        {"timeframe": 376},
        {"fast": 21, "slow": 9},
        {"fast": 9, "slow": 9},
        {"earliest_entry": time(15, 0)},
        {"latest_entry": time(15, 0)},
        {"earliest_entry": time(14, 45), "latest_entry": time(14, 30)},
        {"lot_size": 0},
        {"lots": 0},
        {"slippage_pct": D("-0.001")},
        {"slippage_points": D("-1")},
        {"fill_wait_minutes": -1},
    ],
)
def test_nonsense_parameters_are_rejected(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError, match=r"."):
        StrategyParams(**kwargs)  # type: ignore[arg-type]


def test_params_fingerprint_tracks_every_setting() -> None:
    base = StrategyParams()
    assert base.fingerprint() == StrategyParams().fingerprint()
    assert base.fingerprint() != StrategyParams(slippage_points=D("1")).fingerprint()
    assert base.fingerprint() != StrategyParams(timeframe=15).fingerprint()
    assert base.quantity == 65
    assert StrategyParams(lots=3).quantity == 195


def test_unsorted_spot_input_is_rejected() -> None:
    spot = spot_day(DAY, GOLDEN_CLOSES)
    swapped: list[Candle] = [spot[1], spot[0], *spot[2:]]
    with pytest.raises(ValueError, match="strictly increasing"):
        run_backtest(swapped, EXPIRIES, FullBook(), PARAMS)
