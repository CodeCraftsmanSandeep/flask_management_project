"""Build the static website (GitHub Pages) from the backtest stored in the local database.

    python -m scripts.build_site [--out docs]

The output is plain files and needs no server: the landing page, a read-only backtest page that
filters real trades in the browser (``app/static/js/static-api.js``), and the data it browses. It
holds no accounts, no keys and no way to place an order.
"""

import argparse
import json
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

from flask import render_template

from app import create_app
from app.backtest.export import population_bundle
from app.backtest.query import TIMEFRAMES, Filters
from app.backtest.runs import Population
from app.backtest.service import BacktestService
from app.config import PROJECT_ROOT, Config

STATIC = PROJECT_ROOT / "app" / "static"
ASSETS = [
    "style.css",
    "theme.js",
    "backtest.css",
    "landing.css",
    "js/api.js",
    "js/dom.js",
    "js/format.js",
    "js/charts.js",
    "js/backtest.js",
    "js/static-api.js",
    "vendor/lightweight-charts.LICENSE",
    "vendor/lightweight-charts.standalone.production.js",
]
GENERATED = ["index.html", "home.html", "backtest.html", "assets", "data", ".nojekyll", "README.md"]
# The website opens on the backtest (index.html); the sales page is home.html.
PAGE_LINKS = {"/": "home.html", "/backtest": "index.html"}

README = """# Strategy Desk (website)

A static copy of the NIFTY options backtest: real simulated trades you can filter by timeframe,
strategy, time of day, day of the week and date, each drawn on a chart. Everything runs in your
browser; there is no server, no login and nothing here can place an order.

Backtests are simulations on free community data and do not promise future results.

## Run it locally

```bash
python3 -m http.server 8000   # then open http://localhost:8000
```

Opening `index.html` straight from the folder will not work: browsers refuse to load the data
files from `file://`.

## Publish it

This folder is the GitHub Pages site for AutomatedTradingProject. Set **Settings → Pages →
branch `main`, folder `/docs`**. Rebuild it from the project root with `make site`.
"""


def relative(html: str) -> str:
    """Pages live in the site root, so every root-absolute link becomes a relative one."""
    html = html.replace('"/static/', '"assets/')
    for absolute, page in PAGE_LINKS.items():
        html = html.replace(f'href="{absolute}"', f'href="{page}"')
    leftovers = re.findall(r"""(?:href|src|action)="/(?!/)[^"]*""", html)
    if leftovers:
        msg = f"root-absolute links would break on a project page: {leftovers[:5]}"
        raise SystemExit(msg)
    return html


def pretty_day(day: date) -> str:
    return f"{day.day} {day:%b %Y}"


def write_json(path: Path, payload: Any) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text)


def clean(out: Path) -> None:
    """Remove only what this script writes, so a ``.git`` folder in *out* survives rebuilds."""
    out.mkdir(parents=True, exist_ok=True)
    for name in GENERATED:
        target = out / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()


def copy_assets(out: Path) -> None:
    for name in ASSETS:
        target = out / "assets" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(STATIC / name, target)


def build(out: Path, config: Config, say: Any = print) -> dict[str, int]:
    service = BacktestService(config.database_url)
    meta = service.meta()
    if not meta["has_data"]:
        msg = "There is no market data. Run `make hf-data` and `make import-csv` first."
        raise SystemExit(msg)
    if meta["synthetic"]:
        msg = "The database holds simulated data; a public website must show the real data."
        raise SystemExit(msg)

    clean(out)
    copy_assets(out)
    sizes = {"meta": write_json(out / "data" / "meta.json", meta), "bundles": 0, "details": 0}

    populations: dict[int, Population] = {}
    for tf in TIMEFRAMES:
        population = service.runs.population(tf)
        if population is None:
            msg = f"no results for the {tf}-minute timeframe"
            raise SystemExit(msg)
        populations[tf] = population
        bundle = population_bundle(population)
        sizes["bundles"] += write_json(out / "data" / f"tf-{tf}.json", bundle)
        for index in range(len(population.trades)):
            detail = service.trade_detail(tf, index, None, Filters(timeframe=tf))
            detail["trade"]["entry_no"] = 0  # the browser numbers entries for the filters in use
            path = out / "data" / "detail" / str(tf) / f"{index}.json"
            sizes["details"] += write_json(path, detail)
        say(f"  {tf}-minute: {len(population.trades)} trades")

    default = populations[15]
    facts = {
        "trades": len(default.trades),
        "first": pretty_day(default.first_day) if default.first_day else "",
        "last": pretty_day(default.last_day) if default.last_day else "",
    }
    app = create_app(config)
    pages: dict[str, tuple[str, dict[str, Any]]] = {
        "home.html": ("site_index.html", {"facts": facts}),
        "index.html": ("backtest.html", {"timeframes": TIMEFRAMES}),
    }
    with app.test_request_context("/"):
        for filename, (template, context) in pages.items():
            html = render_template(template, static_site=True, **context)
            (out / filename).write_text(relative(html), encoding="utf-8")

    (out / ".nojekyll").write_text("", encoding="utf-8")
    (out / "README.md").write_text(README, encoding="utf-8")
    return sizes


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "docs",
        help="folder to write the website into (default: docs/)",
    )
    args = parser.parse_args(argv)

    config = replace(Config.from_env(), keys_file=Path("/dev/null"))  # the site never needs a key
    out = args.out.resolve()
    inside_project = out == PROJECT_ROOT or PROJECT_ROOT in out.parents
    if inside_project and out != PROJECT_ROOT / "docs":
        print(
            "Refusing to write the website inside the project (only docs/ is allowed).",
            file=sys.stderr,
        )
        return 2

    print(f"building the website in {out}")
    sizes = build(out, config)
    total = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    files = sum(1 for f in out.rglob("*") if f.is_file())
    data_mb = sum(sizes.values()) / 1_000_000
    print(f"done: {files} files, {total / 1_000_000:.1f} MB (data {data_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
