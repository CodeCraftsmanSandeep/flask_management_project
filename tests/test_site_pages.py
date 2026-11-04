"""The static website opens on the backtest and keeps a way back to the sales page in view."""

import re
from pathlib import Path

import pytest
from flask import Flask, render_template
from flask.testing import FlaskClient

from app import create_app
from app.accounts import AccountBook
from app.backtest.query import TIMEFRAMES
from app.config import Config
from app.groww_service import GrowwService
from scripts.build_site import GENERATED, PAGE_LINKS, relative

FACTS = {"trades": 537, "first": "4 Jun 2025", "last": "3 Jun 2026"}


@pytest.fixture
def app(service: GrowwService, tmp_path: Path) -> Flask:
    return create_app(Config(), service=service, accounts=AccountBook(tmp_path / "accounts.json"))


def render_site(app: Flask) -> dict[str, str]:
    """The two website pages, rendered and rewritten the way ``make site`` writes them."""
    with app.test_request_context("/"):
        backtest = render_template("backtest.html", static_site=True, timeframes=TIMEFRAMES)
        home = render_template("site_index.html", static_site=True, facts=FACTS)
    return {"index.html": relative(backtest), "home.html": relative(home)}


def nav_labels(html: str) -> list[str]:
    nav = re.search(r'<nav class="nav".*?</nav>', html, re.S)
    assert nav
    return re.findall(r">([^<]+)</a>", nav.group(0))


def test_the_website_opens_on_the_backtest_and_home_is_the_sales_page() -> None:
    assert PAGE_LINKS == {"/": "home.html", "/backtest": "index.html"}
    assert {"index.html", "home.html"} <= set(GENERATED)


def test_the_website_menu_lists_the_backtest_first(app: Flask) -> None:
    pages = render_site(app)
    assert nav_labels(pages["index.html"]) == ["Backtest", "Home"]
    assert nav_labels(pages["home.html"]) == ["Backtest", "Home"]


def test_the_backtest_page_keeps_a_link_back_to_the_sales_page_in_view(app: Flask) -> None:
    html = render_site(app)["index.html"]
    bar = re.search(r'<aside class="pitch".*?</aside>', html, re.S)
    assert bar
    assert 'href="home.html"' in bar.group(0)
    assert "10 accounts" in bar.group(0)
    assert "100 VPS machines" in bar.group(0)
    assert "upwork.com" in bar.group(0)


def test_the_sales_page_has_no_pitch_bar_and_links_back_to_the_backtest(app: Flask) -> None:
    html = render_site(app)["home.html"]
    assert 'class="pitch"' not in html
    assert 'href="index.html"' in html  # the "Explore every trade" button


@pytest.mark.parametrize("page", ["index.html", "home.html"])
def test_website_pages_have_no_links_that_break_on_a_project_page(app: Flask, page: str) -> None:
    html = render_site(app)[page]
    assert not re.findall(r"""(?:href|src|action)="/(?!/)""", html)
    assert "/data" not in re.findall(r'href="([^"]*)"', html)


def test_the_live_app_is_unchanged(client: FlaskClient) -> None:
    home = client.get("/").get_data(as_text=True)
    backtest = client.get("/backtest").get_data(as_text=True)
    assert nav_labels(home) == ["Home", "Backtest", "Data"]
    assert 'class="pitch"' not in home
    assert 'class="pitch"' not in backtest
