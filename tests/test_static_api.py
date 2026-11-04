"""The static website must answer exactly like the live service.

The same trades go through the Python service (query.py, metrics.py, present.py) and through
``app/static/js/static-api.js`` under Node, for hundreds of random filter combinations, and the JSON
has to match to the last digit. This is what lets the website show real backtest numbers without a
server.
"""

import json
import random
import shutil
import subprocess
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest

from app.backtest.export import decimals_needed, population_bundle
from app.backtest.query import ALL_DAYS, DAY_NAMES, BadFilterError, parse_filters, select
from app.backtest.runs import Population, default_params
from app.backtest.service import BacktestService
from app.errors import BadRequestError, ResultsChangedError, TradingError
from app.strategy.costs import Charges
from app.strategy.engine import DayOutcome
from app.strategy.models import Mode, Skip, SkipReason, Trade
from tests.builders import make_trade

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="needs Node to run the website code"
)

ROOT = Path(__file__).resolve().parent.parent
HARNESS = ROOT / "tests" / "support" / "static_api_harness.mjs"
NODE = shutil.which("node") or "node"
FIRST_DAY = date(2026, 1, 1)


# ---------------------------------------------------------------------- the trades


def money(rng: random.Random, places: int, limit: int) -> Decimal:
    return Decimal(rng.randint(-limit, limit)).scaleb(-places)


def random_trades(rng: random.Random, days: int, *, places: int = 4) -> list[Trade]:
    """Trades in entry order, on every weekday including Saturdays and Sundays, with ties."""
    trades = []
    for offset in range(days):
        day = FIRST_DAY + timedelta(days=offset)
        for mode in Mode:
            # entries anywhere from 09:30 to 14:55, so every start and cutoff boundary is hit
            for minute in sorted(
                rng.sample(range(9 * 60 + 30, 14 * 60 + 56, 5), rng.randint(0, 4))
            ):
                entry = datetime.combine(day, time(minute // 60, minute % 60))
                exit_minute = min(minute + rng.choice((5, 15, 45, 120)), 15 * 60)
                exit_ = datetime.combine(day, time(exit_minute // 60, exit_minute % 60))
                net = Decimal(0) if rng.random() < 0.06 else money(rng, places, 900_000)
                trades.append(
                    make_trade(
                        net,
                        entry_at=entry,
                        exit_at=exit_,
                        mode=mode,
                        day=day,
                        slippage=abs(money(rng, places, 40_000)),
                        charges=Charges(
                            brokerage=Decimal(20),
                            stt=abs(money(rng, 2, 9_000)),
                            exchange=abs(money(rng, 2, 4_000)),
                            gst=abs(money(rng, 2, 900)),
                        ),
                    )
                )
    trades.sort(key=lambda t: (t.trade_date, t.entry_decision))
    return trades


def population(timeframe: int, trades: list[Trade], rng: random.Random) -> Population:
    skips = [
        Skip(
            trade_date=FIRST_DAY + timedelta(days=rng.randint(0, 60)),
            mode=rng.choice(list(Mode)),
            decision_time=datetime(2026, 1, 1, 10, rng.choice((0, 15, 30))),
            reason=rng.choice(list(SkipReason)),
            detail=f"no candle {rng.randint(1, 99)}",
        )
        for _ in range(rng.randint(0, 9))
    ]
    kinds = ("ok", "ok", "no_signal", "incomplete_spot")
    days = [
        DayOutcome(
            FIRST_DAY + timedelta(days=i), k := rng.choice(kinds), f"{k} {i}" if k != "ok" else ""
        )
        for i in range(60)
    ]
    return Population(
        cache_key=f"key{timeframe}",
        timeframe=timeframe,
        params=default_params(timeframe),
        trades=trades,
        skips=skips,
        days=days,
        first_day=trades[0].trade_date if trades else None,
        last_day=trades[-1].trade_date if trades else None,
        created_at=datetime(2026, 5, 1, 12, 0),
        from_cache=True,  # a website only ever shows saved results
    )


class FakeRuns:
    def __init__(self, populations: dict[int, Population]) -> None:
        self.populations = populations

    def population(self, timeframe: int) -> Population:
        return self.populations[timeframe]


# ---------------------------------------------------------------------- the queries


def filter_params(rng: random.Random, timeframe: int) -> dict[str, str]:
    params = {"timeframe": str(timeframe)}
    if rng.random() < 0.5:
        params["mode"] = rng.choice(["all", *(m.value for m in Mode)])
    if rng.random() < 0.5:
        params["outcome"] = rng.choice(["all", "win", "loss", "tie"])
    if rng.random() < 0.5:
        params["start"] = rng.choice(["09:30", "10:00", "10:30"])
    if rng.random() < 0.5:
        params["cutoff"] = rng.choice(
            ["11:30", "12:00", "12:30", "13:00", "13:30", "14:00", "14:30"]
        )
    if rng.random() < 0.4:
        params["cross"] = rng.choice(["all", "1", "2", "3", "5", "20"])
    if rng.random() < 0.6:
        chosen = rng.sample(DAY_NAMES, rng.randint(1, 7))
        params["days"] = ",".join(chosen)
    if rng.random() < 0.4:
        params["from"] = (FIRST_DAY + timedelta(days=rng.randint(0, 40))).isoformat()
    if rng.random() < 0.4:
        params["to"] = (FIRST_DAY + timedelta(days=rng.randint(20, 90))).isoformat()
    if "from" in params and "to" in params and params["from"] > params["to"]:
        params["from"], params["to"] = params["to"], params["from"]
    return params


BAD_PARAMS = [
    {"timeframe": "7"},
    {"timeframe": "fast"},
    {"mode": "straddle"},
    {"outcome": "huge"},
    {"start": "9am"},
    {"start": "09:45"},
    {"cutoff": "15:00"},
    {"cutoff": "late"},
    {"cross": "0"},
    {"cross": "21"},
    {"cross": "first"},
    {"days": "mon,funday"},
    {"days": "mon,,tue"},
    {"from": "yesterday"},
    {"to": "2026-13-45"},
    {"from": "2026-05-12", "to": "2026-05-01"},
]


def queries(rng: random.Random, timeframes: list[int], count: int) -> list[str]:
    urls = []
    for _ in range(count):
        tf = rng.choice(timeframes)
        urls.append("/api/backtest/trades?" + urlencode(filter_params(rng, tf)))
    for bad in BAD_PARAMS:
        urls.append("/api/backtest/trades?" + urlencode(bad))
    urls.append("/api/backtest/trades")  # every default
    urls.append("/api/backtest/trades?mode=&outcome=&days=&cross=")  # empty means default
    for tf in timeframes:
        urls.append(f"/api/backtest/skips?timeframe={tf}")
        urls.append(f"/api/backtest/skips?timeframe={tf}&from=2026-01-20&to=2026-02-10")
        urls.append(f"/api/backtest/skips?timeframe={tf}&from=2026-02-01")
    urls += [
        "/api/backtest/skips",
        "/api/backtest/skips?timeframe=9",
        "/api/backtest/skips?from=soon",
        "/api/backtest/skips?to=2026-02-30",
    ]
    return urls


# --------------------------------------------------------------- what Python says


def python_answer(service: BacktestService, url: str) -> dict[str, Any]:
    path, _, query = url.partition("?")
    args = dict(pair.split("=", 1) for pair in query.split("&") if pair)
    args = {k: v.replace("%3A", ":").replace("%2C", ",") for k, v in args.items()}
    try:
        if path == "/api/backtest/trades":
            return {"ok": service.trades(parse_filters(args))}
        if path == "/api/backtest/skips":
            filters = parse_filters({"timeframe": args.get("timeframe", "")})

            def day(name: str) -> date | None:
                raw = args.get(name, "").strip()
                if not raw:
                    return None
                try:
                    return date.fromisoformat(raw)
                except ValueError:
                    msg = f"{name} must look like 2026-05-12"
                    raise BadRequestError(msg) from None

            return {"ok": service.skips(filters.timeframe, day("from"), day("to"))}
    except TradingError as error:
        return {
            "error": {"message": error.message, "code": error.code, "status": error.http_status}
        }
    raise AssertionError(url)


def normal(value: Any) -> Any:
    """JSON round trip, so ``-0.0 == 0`` and tuples are lists exactly as they are on the wire."""
    return json.loads(json.dumps(value))


def write_site(folder: Path, populations: dict[int, Population]) -> None:
    (folder / "data").mkdir(parents=True)
    for timeframe, pop in populations.items():
        (folder / "data" / f"tf-{timeframe}.json").write_text(json.dumps(population_bundle(pop)))


def run_node(folder: Path, urls: list[str]) -> list[dict[str, Any]]:
    queries_file = folder / "queries.json"
    queries_file.write_text(json.dumps(urls))
    done = subprocess.run(  # noqa: S603  (fixed program and arguments, no user input)
        [NODE, str(HARNESS), str(folder), str(queries_file)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)  # type: ignore[no-any-return]


def compare(service: BacktestService, folder: Path, urls: list[str]) -> None:
    got = run_node(folder, urls)
    assert len(got) == len(urls)
    for url, answer in zip(urls, got, strict=True):
        assert normal(answer) == normal(python_answer(service, url)), url


def service_for(populations: dict[int, Population]) -> BacktestService:
    service = BacktestService("postgresql://unused@127.0.0.1:1/none")
    service.runs = FakeRuns(populations)  # type: ignore[assignment]
    return service


# ------------------------------------------------------------------------- tests


def test_random_trades_and_filters_match_the_live_service(tmp_path: Path) -> None:
    rng = random.Random(20260604)
    populations = {tf: population(tf, random_trades(rng, 70), rng) for tf in (5, 15, 30)}
    assert all(len(p.trades) > 150 for p in populations.values())
    write_site(tmp_path, populations)
    compare(service_for(populations), tmp_path, queries(rng, [5, 15, 30], 500))


@pytest.mark.parametrize("places", [0, 2, 4])
def test_any_number_of_decimals_matches(tmp_path: Path, places: int) -> None:
    rng = random.Random(places)
    pop = population(15, random_trades(rng, 40, places=places), rng)
    # charges have 2 places, so a "0 places" run still needs 2 unless they are whole rupees
    assert decimals_needed(pop.trades) >= places
    write_site(tmp_path, {15: pop})
    compare(service_for({15: pop}), tmp_path, queries(rng, [15], 150))


def test_the_odd_populations_match(tmp_path: Path) -> None:
    rng = random.Random(7)
    day = date(2026, 3, 2)
    wins = [
        make_trade("100.5", day=day, entry_at=datetime(2026, 3, 2, 10, i * 5)) for i in range(6)
    ]
    losses = [
        make_trade("-100.5", day=day, entry_at=datetime(2026, 3, 2, 10, i * 5)) for i in range(6)
    ]
    ties = [make_trade("0", day=day, entry_at=datetime(2026, 3, 2, 11, i * 5)) for i in range(3)]
    # 1 win in 32 trades: win rate 1/32 = 0.03125, an exact tie at 4 places (round half to even)
    thirty_two = [make_trade("5", day=day, entry_at=datetime(2026, 3, 2, 10, 0))] + [
        make_trade("-5", day=day, entry_at=datetime(2026, 3, 2, 10, 0)) for _ in range(31)
    ]
    cases = {
        5: population(5, [], rng),
        15: population(15, wins, rng),
        30: population(30, losses + ties, rng),
    }
    cases[5].first_day = cases[5].last_day = None
    write_site(tmp_path / "a", cases)
    compare(service_for(cases), tmp_path / "a", queries(rng, [5, 15, 30], 80))

    more = {5: population(5, thirty_two, rng), 15: population(15, wins + losses + ties, rng)}
    write_site(tmp_path / "b", more)
    urls = ["/api/backtest/trades?timeframe=5", *queries(rng, [5, 15], 60)]
    compare(service_for(more), tmp_path / "b", urls)
    # Python rounds the exact tie 0.03125 to the even 0.0312; a naive JavaScript round gives 0.0313
    assert run_node(tmp_path / "b", urls[:1])[0]["ok"]["kpis"]["win_rate"] == 0.0312


def test_the_weekday_filter_agrees_with_python_weekdays(tmp_path: Path) -> None:
    """Every date of a year, so no weekday or month boundary can be off by one."""
    trades = [make_trade("10", day=FIRST_DAY + timedelta(days=i)) for i in range(400)]
    rng = random.Random(1)
    pop = population(15, trades, rng)
    write_site(tmp_path, {15: pop})
    urls = [f"/api/backtest/trades?timeframe=15&days={name}" for name in DAY_NAMES] + [
        "/api/backtest/trades?timeframe=15&days=mon,tue,wed,thu,fri"
    ]
    compare(service_for({15: pop}), tmp_path, urls)
    for name, answer in zip(DAY_NAMES, run_node(tmp_path, urls)[:7], strict=True):
        index = DAY_NAMES.index(name)
        expected = sum(1 for t in trades if t.trade_date.weekday() == index)
        assert answer["ok"]["kpis"]["trades"] == expected


def test_trade_detail_gets_its_entry_number_from_the_filters(tmp_path: Path) -> None:
    rng = random.Random(3)
    pop = population(15, random_trades(rng, 30), rng)
    write_site(tmp_path, {15: pop})
    detail_dir = tmp_path / "data" / "detail" / "15"
    detail_dir.mkdir(parents=True)
    for index in range(len(pop.trades)):
        (detail_dir / f"{index}.json").write_text(
            json.dumps({"id": index, "trade": {"entry_no": 0}})
        )

    filters_text = "days=mon,tue,wed&start=10:00&cutoff=13:00"
    ids = [0, 5, 17, len(pop.trades) - 1]
    urls = [f"/api/backtest/trade/15/{i}?{filters_text}&run=whatever" for i in ids]
    urls += [f"/api/backtest/trade/15/{len(pop.trades)}", "/api/backtest/trade/15/0?start=bad"]
    answers = run_node(tmp_path, urls)

    filters = parse_filters(
        {"timeframe": "15", "days": "mon,tue,wed", "start": "10:00", "cutoff": "13:00"}
    )
    seen = {s.index: s.entry_no for s in select(pop.trades, filters, ignore_mode=True)}
    for index, answer in zip(ids, answers, strict=False):
        assert answer["ok"] == {"id": index, "trade": {"entry_no": seen.get(index, 0)}}
    assert answers[-2]["error"]["code"] == ResultsChangedError.code
    assert answers[-2]["error"]["status"] == ResultsChangedError.http_status
    assert answers[-1]["error"]["code"] == BadFilterError.code


def test_unknown_requests_are_refused_not_guessed(tmp_path: Path) -> None:
    rng = random.Random(4)
    pop = population(15, random_trades(rng, 5), rng)
    write_site(tmp_path, {15: pop})
    answers = run_node(tmp_path, ["/api/data/status", "/api/accounts", "/api/backtest/trade/15/x"])
    assert [a["error"]["status"] for a in answers] == [404, 404, 404]


def test_a_missing_data_file_is_a_clear_error(tmp_path: Path) -> None:
    (tmp_path / "data").mkdir()
    answers = run_node(tmp_path, ["/api/backtest/trades?timeframe=15", "/api/backtest/meta"])
    assert all(a["error"]["code"] == "not_found" for a in answers)


def test_the_bundle_orders_trades_exactly_as_the_run_does() -> None:
    rng = random.Random(9)
    pop = population(15, random_trades(rng, 30), rng)
    bundle = population_bundle(pop)
    assert [t["entry_decision"] for t in bundle["trades"]] == [
        t.entry_decision.isoformat() for t in pop.trades
    ]
    stamps = [t["entry_decision"] for t in bundle["trades"]]
    assert all(a[:10] <= b[:10] for a, b in pairwise(stamps))
    assert set(range(7)) == ALL_DAYS
