"""The HTTP layer for the backtest viewer and the data download."""

import re
from typing import Any

import pytest
from flask.testing import FlaskClient

from app import create_app
from app.backtest.service import BacktestService
from app.config import Config
from app.db.connection import connect, session
from app.groww_service import GrowwService
from app.history.store import Store
from tests.test_backfill import FakeSource, make, run
from tests.test_backtest_service import params_for
from tests.test_jobs import WAIT, make_job

CSRF = {"X-Requested-With": "fetch"}
PAGES = ["/backtest", "/data"]


def build_client(service: GrowwService, url: str, source: FakeSource | None = None) -> FlaskClient:
    app = create_app(
        Config(database_url=url),
        service=service,
        backtest=BacktestService(url, params_for=params_for),
        download=make_job(url, source or FakeSource()),
    )
    app.testing = True
    return app.test_client()


@pytest.fixture
def web(service: GrowwService, db_url: str) -> FlaskClient:
    """An app over a database that already holds the golden day."""
    conn = connect(db_url)
    try:
        run(make(FakeSource(), Store(conn)))
    finally:
        conn.close()
    return build_client(service, db_url)


@pytest.fixture
def empty_web(service: GrowwService, db_url: str) -> FlaskClient:
    return build_client(service, db_url)


def error_of(response: Any) -> dict[str, str]:
    assert response.is_json
    return response.get_json()["error"]  # type: ignore[no-any-return]


# ----------------------------------------------------------------------- pages


@pytest.mark.parametrize("page", PAGES)
def test_pages_load_with_security_headers(web: FlaskClient, page: str) -> None:
    response = web.get(page)
    assert response.status_code == 200
    assert "script-src 'self'" in response.headers["Content-Security-Policy"]
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize("page", PAGES)
def test_pages_have_no_inline_script_style_or_handlers(web: FlaskClient, page: str) -> None:
    """The Content-Security-Policy forbids them, so they would silently break the page."""
    html = web.get(page).get_data(as_text=True)
    assert not re.search(r"<script(?![^>]*\bsrc=)", html)
    assert not re.search(r"\sstyle\s*=", html)
    assert not re.search(r"<style", html)
    assert not re.search(r"\son[a-z]+\s*=", html)


@pytest.mark.parametrize("page", ["/", *PAGES])
def test_every_page_links_to_every_other_page(web: FlaskClient, page: str) -> None:
    html = web.get(page).get_data(as_text=True)
    for target in ("/", "/backtest", "/data"):
        assert f'href="{target}"' in html


@pytest.mark.parametrize("page", PAGES)
def test_pages_load_without_a_database(service: GrowwService, page: str) -> None:
    """Pages are static shells; the data arrives over the API and can show its own error."""
    client = build_client(service, "postgresql://trading@127.0.0.1:1/none")
    assert client.get(page).status_code == 200


def test_the_vendored_chart_library_and_page_scripts_are_served(web: FlaskClient) -> None:
    html = web.get("/backtest").get_data(as_text=True)
    scripts = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html)
    assert any("lightweight-charts" in s for s in scripts)
    for src in scripts:
        assert web.get(src).status_code == 200, src


def test_the_charting_library_credit_is_shown(web: FlaskClient) -> None:
    assert "tradingview.com" in web.get("/backtest").get_data(as_text=True).lower()


# ---------------------------------------------------------------------- meta


def test_meta_describes_the_filter_options_and_data(web: FlaskClient) -> None:
    body = web.get("/api/backtest/meta").get_json()
    assert body["has_data"] is True
    assert body["timeframes"] == [5, 15, 30]
    assert [m["value"] for m in body["modes"]] == ["itm5_buy", "atm_buy", "atm_sell"]
    assert body["starts"] == ["09:30", "10:00", "10:30"]
    assert body["cutoffs"][0] == "11:30"
    assert body["cutoffs"][-1] == "14:30"
    assert body["rules"]
    assert any(row["verified"] for row in body["cost_schedule"])


def test_results_are_cacheable_but_always_revalidated(web: FlaskClient) -> None:
    response = web.get("/api/backtest/trades")
    cache = response.headers["Cache-Control"]
    assert "no-cache" in cache
    assert "private" in cache
    assert "no-store" not in cache
    etag = response.headers["ETag"]

    again = web.get("/api/backtest/trades", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.get_data() == b""


def test_a_changed_filter_changes_the_etag(web: FlaskClient) -> None:
    a = web.get("/api/backtest/trades?timeframe=5")
    b = web.get("/api/backtest/trades?timeframe=5&mode=atm_buy")
    assert a.headers["ETag"] != b.headers["ETag"]
    stale = web.get(
        "/api/backtest/trades?timeframe=5&mode=atm_buy",
        headers={"If-None-Match": a.headers["ETag"]},
    )
    assert stale.status_code == 200


def test_account_data_is_still_never_cached(web: FlaskClient) -> None:
    assert web.post("/api/connect", headers=CSRF).status_code == 200
    for path in ("/api/status", "/api/profile", "/api/data/status"):
        assert web.get(path).headers["Cache-Control"] == "no-store", path


# -------------------------------------------------------------------- trades


def test_the_default_timeframe_is_15_minutes(web: FlaskClient) -> None:
    assert web.get("/api/backtest/trades").get_json()["timeframe"] == 15


def test_trades_default_view(web: FlaskClient) -> None:
    body = web.get("/api/backtest/trades?timeframe=5").get_json()
    assert body["timeframe"] == 5
    assert body["kpis"]["trades"] == len(body["trades"]) == 9
    assert set(body["compare"]) == {"itm5_buy", "atm_buy", "atm_sell"}
    assert body["run"]
    ids = [row["id"] for row in body["trades"]]
    assert len(set(ids)) == len(ids)


def test_filters_in_the_query_string_apply(web: FlaskClient) -> None:
    everything = web.get("/api/backtest/trades?timeframe=5").get_json()
    one_mode = web.get("/api/backtest/trades?timeframe=5&mode=atm_buy").get_json()
    assert 0 < len(one_mode["trades"]) < len(everything["trades"])
    assert {t["mode"] for t in one_mode["trades"]} == {"atm_buy"}
    # The mode comparison stays on all modes so the tabs can show every column.
    assert one_mode["compare"] == everything["compare"]

    wins = web.get("/api/backtest/trades?timeframe=5&outcome=win").get_json()
    assert {t["outcome"] for t in wins["trades"]} == {"win"}
    nothing = web.get("/api/backtest/trades?timeframe=5&from=2030-01-01").get_json()
    assert nothing["trades"] == []
    assert nothing["kpis"]["trades"] == 0


def test_the_weekday_filter_keeps_or_drops_the_trading_day(web: FlaskClient) -> None:
    # the fixture trades on Tuesday 2026-05-12
    tuesday = web.get("/api/backtest/trades?timeframe=5&days=tue").get_json()
    assert tuesday["kpis"]["trades"] == 9
    other = web.get("/api/backtest/trades?timeframe=5&days=mon,wed,thu,fri").get_json()
    assert other["trades"] == []
    assert other["kpis"]["trades"] == 0
    assert {m["trades"] for m in other["compare"].values()} == {0}  # the compare table follows
    every = web.get("/api/backtest/trades?timeframe=5&days=mon,tue,wed,thu,fri,sat,sun").get_json()
    assert every["kpis"]["trades"] == 9


@pytest.mark.parametrize(
    "query",
    [
        "timeframe=7",
        "timeframe=abc",
        "mode=nonsense",
        "outcome=maybe",
        "start=09:31",
        "cutoff=16:00",
        "cross=0",
        "cross=x",
        "days=funday",
        "days=mon,,tue",
        "from=2026-13-40",
        "from=2026-05-12&to=2026-05-01",
    ],
)
def test_bad_filters_are_a_clear_400(web: FlaskClient, query: str) -> None:
    response = web.get(f"/api/backtest/trades?{query}")
    assert response.status_code == 400
    error = error_of(response)
    assert error["code"] == "bad_filter"
    assert error["message"]


def test_without_data_the_api_says_so_plainly(empty_web: FlaskClient) -> None:
    meta = empty_web.get("/api/backtest/meta").get_json()
    assert meta["has_data"] is False
    response = empty_web.get("/api/backtest/trades")
    assert response.status_code == 404
    assert error_of(response)["code"] == "no_data"
    assert "Data page" in error_of(response)["message"]


# -------------------------------------------------------------------- detail


def test_a_trade_detail_has_chart_data_and_a_cost_breakdown(web: FlaskClient) -> None:
    listing = web.get("/api/backtest/trades?timeframe=5").get_json()
    first = listing["trades"][0]
    response = web.get(f"/api/backtest/trade/5/{first['id']}?run={listing['run']}")
    assert response.status_code == 200
    body = response.get_json()
    assert body["id"] == first["id"]
    assert body["trade"]["date"] == first["date"]
    assert body["spot"]["bars"]
    assert len(body["spot"]["bars"][0]) == 7
    assert body["option"]["candles"]
    costs = body["trade"]["costs"]
    parts = (
        costs["slippage"]
        + costs["brokerage"]
        + costs["stt"]
        + costs["exchange"]
        + costs["sebi"]
        + costs["stamp"]
        + costs["gst"]
    )
    assert costs["net"] == pytest.approx(costs["gross"] - parts, abs=0.05)


def test_a_detail_with_an_old_run_key_asks_for_a_reload(web: FlaskClient) -> None:
    response = web.get("/api/backtest/trade/5/0?run=stale")
    assert response.status_code == 409
    assert error_of(response)["code"] == "results_changed"


@pytest.mark.parametrize("index", [9, 999])
def test_a_missing_trade_is_not_a_crash(web: FlaskClient, index: int) -> None:
    response = web.get(f"/api/backtest/trade/5/{index}")
    assert response.status_code == 409
    assert error_of(response)["code"] == "results_changed"


@pytest.mark.parametrize("path", ["/api/backtest/trade/5/-1", "/api/backtest/trade/5/abc"])
def test_malformed_trade_ids_are_404(web: FlaskClient, path: str) -> None:
    assert web.get(path).status_code == 404


def test_detail_for_a_bad_timeframe_is_a_400(web: FlaskClient) -> None:
    assert web.get("/api/backtest/trade/7/0").status_code == 400


def test_the_entry_number_follows_the_requested_window(web: FlaskClient) -> None:
    listing = web.get("/api/backtest/trades?timeframe=5&start=10:00").get_json()
    row = listing["trades"][0]
    detail = web.get(f"/api/backtest/trade/5/{row['id']}?start=10:00").get_json()
    assert detail["trade"]["entry_no"] == row["entry_no"]


# --------------------------------------------------------------------- skips


def test_skips_lists_what_was_not_simulated(web: FlaskClient) -> None:
    body = web.get("/api/backtest/skips?timeframe=5").get_json()
    assert "skipped_trades" in body
    assert "incomplete_days" in body
    assert isinstance(body["skip_reasons"], dict)


def test_skips_rejects_bad_dates_and_timeframes(web: FlaskClient) -> None:
    assert web.get("/api/backtest/skips?from=nope").status_code == 400
    assert web.get("/api/backtest/skips?timeframe=7").status_code == 400


# ----------------------------------------------------------------- data status


def test_data_status_reports_what_is_stored_and_the_job(web: FlaskClient) -> None:
    body = web.get("/api/data/status").get_json()
    assert body["has_data"] is True
    assert body["spot_rows"] > 0
    assert body["timestamp_convention"] == "start"
    assert body["job"]["state"] == "idle"


def test_data_status_on_an_empty_database(empty_web: FlaskClient) -> None:
    body = empty_web.get("/api/data/status").get_json()
    assert body["has_data"] is False
    assert body["spot_rows"] == 0


# --------------------------------------------------------------- download / stop


def test_starting_a_download_needs_the_csrf_header(empty_web: FlaskClient) -> None:
    assert empty_web.post("/api/data/download").status_code == 403
    assert empty_web.post("/api/data/stop").status_code == 403
    assert empty_web.get("/api/data/status").get_json()["job"]["state"] == "idle"


def test_a_download_runs_and_fills_the_database(service: GrowwService, db_url: str) -> None:
    client = build_client(service, db_url)
    started = client.post("/api/data/download", json={"months": 1, "timeframes": [5]}, headers=CSRF)
    assert started.status_code == 200
    assert started.get_json()["request"]["timeframes"] == [5]

    app_job = client.application.extensions["download"]
    assert app_job.wait(WAIT)
    status = client.get("/api/data/status").get_json()
    assert status["job"]["state"] == "done"
    assert status["has_data"] is True


@pytest.mark.parametrize(
    "body",
    [
        {"months": "12"},
        {"months": 0},
        {"months": True},
        {"months": 99},
        {"timeframes": "5"},
        {"timeframes": [5, "15"]},
        {"timeframes": [9]},
        {"end": "yesterday"},
        {"end": "2999-01-01"},
        [1, 2, 3],
    ],
)
def test_a_bad_download_request_is_a_400_and_starts_nothing(
    empty_web: FlaskClient, body: Any
) -> None:
    response = empty_web.post("/api/data/download", json=body, headers=CSRF)
    assert response.status_code == 400, response.get_data(as_text=True)
    assert error_of(response)["code"] == "bad_request"
    assert empty_web.get("/api/data/status").get_json()["job"]["state"] == "idle"


def test_starting_without_connecting_says_to_connect(service: GrowwService, db_url: str) -> None:
    app = create_app(
        Config(database_url=db_url),
        service=service,  # never connected
        backtest=BacktestService(db_url),
    )
    app.testing = True
    response = app.test_client().post("/api/data/download", json={}, headers=CSRF)
    assert response.status_code == 401
    assert "Connect" in error_of(response)["message"]


def test_stopping_when_nothing_runs_is_harmless(empty_web: FlaskClient) -> None:
    response = empty_web.post("/api/data/stop", headers=CSRF)
    assert response.status_code == 200
    assert response.get_json()["state"] == "idle"


# ----------------------------------------------------------------------- errors


def test_a_stopped_database_gives_a_helpful_503_and_the_account_page_still_works(
    service: GrowwService,
) -> None:
    client = build_client(service, "postgresql://trading@127.0.0.1:1/none")
    response = client.get("/api/backtest/trades")
    assert response.status_code == 503
    assert error_of(response)["code"] == "database_unavailable"
    assert "make db-start" in error_of(response)["message"]
    assert client.get("/api/status").status_code == 200
    assert client.get("/").status_code == 200


def test_an_unmigrated_database_says_to_migrate(service: GrowwService, db_url: str) -> None:
    with session(db_url) as conn:
        conn.execute("DROP TABLE backtest_runs")
        conn.execute("DROP TABLE meta")
    client = build_client(service, db_url)
    response = client.get("/api/data/status")
    assert response.status_code == 503
    assert error_of(response)["code"] == "database_not_migrated"
    assert "make migrate" in error_of(response)["message"]


def test_unexpected_failures_return_json_without_internals(
    web: FlaskClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = web.application.extensions["backtest"]

    def boom() -> None:
        msg = "password=hunter2 /private/path"
        raise RuntimeError(msg)

    monkeypatch.setattr(service, "meta", boom)
    response = web.get("/api/backtest/meta")
    assert response.status_code == 500
    assert error_of(response)["code"] == "internal_error"
    assert "hunter2" not in response.get_data(as_text=True)


def test_unknown_api_paths_are_json_404(web: FlaskClient) -> None:
    response = web.get("/api/backtest/nope")
    assert response.status_code == 404
    assert error_of(response)["code"] == "not_found"


def test_the_host_header_check_covers_the_new_routes(web: FlaskClient) -> None:
    response = web.get("/api/backtest/trades", headers={"Host": "evil.example"})
    assert response.status_code == 400
