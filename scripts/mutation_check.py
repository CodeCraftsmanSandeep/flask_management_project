"""Poor man's mutation testing: break the code on purpose and confirm the tests notice.

Each mutation rewrites one line of the engine, runs the strategy tests, and restores the file.
A mutation that is *not* caught (tests still pass) means a test is missing.

    python -m scripts.mutation_check
"""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = [
    "tests/test_bars.py",
    "tests/test_options_fills_costs.py",
    "tests/test_engine.py",
    "tests/test_metrics.py",
    "tests/test_engine_window.py",
]


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    old: str
    new: str


MUTATIONS = [
    Mutation(
        "cutoff excludes its own minute",
        "app/strategy/engine.py",
        "<= params.latest_entry",
        "< params.latest_entry",
    ),
    Mutation(
        "window start becomes exclusive",
        "app/strategy/engine.py",
        "params.earliest_entry <= cross",
        "params.earliest_entry < cross",
    ),
    Mutation(
        "a 15:00 cross is treated as live",
        "app/strategy/engine.py",
        "c.decision_time < square_off",
        "c.decision_time <= square_off",
    ),
    Mutation(
        "buy slippage is subtracted",
        "app/strategy/fills.py",
        "to_tick(raw * (1 + pct) + points",
        "to_tick(raw * (1 + pct) - points",
    ),
    Mutation(
        "sell fill rounds up",
        "app/strategy/fills.py",
        "to_tick(raw * (1 - pct) - points, up=False)",
        "to_tick(raw * (1 - pct) - points, up=True)",
    ),
    Mutation(
        "sell floor removed",
        "app/strategy/fills.py",
        "max(to_tick(raw * (1 - pct) - points, up=False), TICK)",
        "to_tick(raw * (1 - pct) - points, up=False)",
    ),
    Mutation("ITM depth is 4 strikes", "app/strategy/options.py", "ITM_STEPS = 5", "ITM_STEPS = 4"),
    Mutation(
        "ATM ties round down",
        "app/strategy/options.py",
        "int((spot + half) // STRIKE_STEP)",
        "int((spot + half - Decimal('0.0001')) // STRIKE_STEP)",
    ),
    Mutation(
        "ITM goes the wrong way for puts",
        "app/strategy/options.py",
        "atm - depth if bullish else atm + depth",
        "atm + depth if bullish else atm - depth",
    ),
    Mutation(
        "sell gross sign flipped",
        "app/strategy/engine.py",
        "gross = (entry_raw - exit_raw) * qty",
        "gross = (exit_raw - entry_raw) * qty",
    ),
    Mutation(
        "whipsaw window off by one bar",
        "app/strategy/engine.py",
        "held <= timedelta(",
        "held < timedelta(",
    ),
    Mutation(
        "stale exit uses the open",
        "app/strategy/engine.py",
        "exit_candle.close if stale",
        "exit_candle.open if stale",
    ),
    Mutation(
        "tie handling flips sign on zero",
        "app/strategy/bars.py",
        "(diff > TIE_EPSILON) - (diff < -TIE_EPSILON)",
        "(diff >= 0) - (diff < 0)",
    ),
    Mutation(
        "EMA alpha off by one",
        "app/strategy/bars.py",
        "\n    alpha = 2 / (period + 1)",
        "\n    alpha = 2 / period",
    ),
]


def clear_bytecode() -> None:
    """Same-size edits within one second would otherwise reuse a stale .pyc (mtime+size match)."""
    for cache in (ROOT / "app").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)


def run_tests() -> bool:
    clear_bytecode()
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "-m", "pytest", *TESTS, "-q", "-x", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    return proc.returncode == 0


def main() -> int:
    survivors: list[str] = []
    for m in MUTATIONS:
        path = ROOT / m.file
        original = path.read_text()
        if m.old not in original:
            print(f"SKIP     {m.name}: pattern not found in {m.file}")
            survivors.append(f"{m.name} (pattern missing)")
            continue
        path.write_text(original.replace(m.old, m.new, 1))
        try:
            passed = run_tests()
        finally:
            path.write_text(original)
        print(f"{'SURVIVED' if passed else 'killed  '} {m.name}")
        if passed:
            survivors.append(m.name)
    clear_bytecode()
    print(f"\n{len(MUTATIONS) - len(survivors)}/{len(MUTATIONS)} mutations caught")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
