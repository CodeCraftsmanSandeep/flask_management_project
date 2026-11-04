# Strategy Desk

A local Python + Flask app to **test a trading strategy on a year of real market data** and to keep
your **broker accounts** in one place.

* **Backtester and trade viewer.** A 9/21 EMA crossover on NIFTY index options: every simulated trade
  on a candlestick chart, slippage and the full charge stack counted, and filters for timeframe
  (15 min by default), strategy, entry window, crossover number, dates and days of the week. It runs
  on the free Hugging Face dataset described below. See
  [Backtester](#backtester-9--21-ema-crossover-on-nifty-options).
* **A home page that sells the scaling idea.** It asks two questions (copy one strategy into 10
  accounts on one machine, or run 100 machines on VPS servers) and sends visitors to your Upwork
  profile. The link is `CONTACT_URL` (an `https://` address; the default is the Upwork profile).
* **Accounts.** One circle per broker account (Zerodha, Groww or any other broker) and a "+" circle to
  add one. See [Accounts](#accounts).
* **A read-only Groww overview** (profile, funds, holdings, positions) for a Groww account, using
  Groww's official SDK and its **TOTP** sign-in.

Nothing here places, modifies or cancels an order. **Deploying a strategy on a connected account is
not built yet**; it needs a live market-data source and per-account safeguards first (see
[Accounts](#accounts)).

## Quick start

```bash
make install   # creates .venv (Python 3.13) and installs the pinned dependencies
make smoke     # optional: log in once and confirm every endpoint answers (prints no values)
make run       # dashboard on http://127.0.0.1:5001   (make dev = debug mode + auto-reload)
```

Open <http://127.0.0.1:5001>. The app never contacts a broker until you press a Connect button, and it
never logs in on page load.

Python 3.13 is required (`brew install python@3.13` on macOS). See
[Why Python 3.13](#why-python-313) below. If the port is taken: `make run PORT=5050`.

## Accounts

The first page shows one circle per account. Press the dashed **+** circle, pick a broker, name the
account and enter its keys. Select a circle to see the account (keys shown only as `••••` plus their
last four characters) or to remove it.

| Broker  | What is stored                                      | Status shown                                     |
| ------- | --------------------------------------------------- | ------------------------------------------------ |
| Zerodha | API key and API secret                              | *Saved* (nothing connects yet)                   |
| Groww   | Nothing: it uses `keys.txt` (below). One account.   | *Connected* / *Not connected*; shows the overview |
| Other   | Broker name, optional API key and secret            | *Saved* (nothing connects yet)                   |

* Accounts are kept in `secrets/accounts.json` (git-ignored; `ACCOUNTS_FILE` changes the path). The
  file is created readable by you only and replaced atomically, and the API never returns a full key.
* **Adding an account does not log in, read a balance or place an order.**
* **Not built yet: deploying a strategy on an account.** It needs (1) a live market-data source:
  Groww's and Zerodha's live-data APIs are paid plans, and the free data used for backtesting ends
  months ago; (2) each broker's own login (Zerodha requires a fresh login every trading day); and
  (3) safeguards before any real order: per-account limits, a daily loss cap, a kill switch and a
  paper-trading stage that proves the signals match the backtest. Real money is not touched until
  those exist and are tested.

## Credentials: `keys.txt`

`keys.txt` sits next to this file and holds one `NAME=value` per line (`NAME: value` also works;
blank lines, `#` comments and quotes around values are fine). Only these two entries are read:

| Needed        | Accepted labels                                                       | What it is                                      |
| ------------- | --------------------------------------------------------------------- | ----------------------------------------------- |
| TOTP token    | `GROWW_TOTP_TOKEN` (also `GROWW_TOP_TOKEN`)                           | The long string labelled "TOTP token" on Groww  |
| TOTP secret   | `GROWW_TOTP_SECRET` (also `GROWW_TOTP_QR_CODE`, `GROWW_TOP_QR_CODE`)  | The 32-character base32 code behind the QR code |

* Every other line (for example an API key / API secret for Groww's other login flow) is skipped
  and never loaded into memory.
* Environment variables with the same names override the file. `GROWW_KEYS_FILE=/path/to/file`
  points the app at a different file.
* `keys.txt` is git-ignored, together with `.env*`, `*.pem`, `*.key` and `token*.json`.
  Restrict it to yourself with `chmod 600 keys.txt` - the app logs a warning while other users on
  the machine can read it.

## Everyday commands

| Command          | What it does                                                                  |
| ---------------- | ----------------------------------------------------------------------------- |
| `make install`   | Create `.venv` and install the pinned runtime + dev dependencies              |
| `make run`       | Start the dashboard (localhost only)                                          |
| `make dev`       | Same, with Flask debug mode and auto-reload                                   |
| `make smoke`     | Real login + one call per endpoint; prints field names and types, never values |
| `make demo`      | Backtest app on **simulated** data (no Groww needed)                          |
| `make backfill`  | Download NIFTY / option 1-minute history into Postgres (`ARGS="--months 12"`)  |
| `make site`      | Build the static GitHub Pages website into `../AutomatedTradingSite`          |
| `make hf-data`   | Download the free Hugging Face NIFTY dataset and convert it for `import-csv` (`ARGS="--months 12"`) |
| `make import-csv` | Load 1-minute CSV files from any provider (`ARGS="--spot a.csv --options b.csv --expiries e.csv --dry-run"`) |
| `make db-start` / `db-stop` | Start / stop the project-local Postgres (port 5433)                |
| `make check`     | Everything CI would run: ruff, mypy `--strict`, pytest, JS tests (if Node)     |
| `make format`    | Auto-fix lint issues and format the code                                      |
| `make lint` / `make typecheck` / `make test` | The three parts of `make check` on their own      |
| `make clean`     | Remove tool caches (keeps `.venv`)                                            |

The test suite never touches the network or your real keys.

## Backtester: 9 / 21 EMA crossover on NIFTY options

> **Read this first.** Nothing here is a promise about future results. A backtest uses assumed
> fills and cost rates; real trading differs. The `make demo` data is **randomly generated, not
> market data**, and the app says so in a banner on every page.

### Try it now (simulated data, no Groww access needed)

```bash
make demo        # builds the throw-away database `trading_demo` once, then serves it
```

Open the printed address (default <http://127.0.0.1:5001/backtest>). `make demo-data ARGS="--days 200"`
rebuilds the demo database with a different length. The script refuses to touch any database
whose name does not contain `demo`.

### Use real Groww history

```bash
make db-start                       # project-local Postgres 17 on 127.0.0.1:5433 (data in ./pgdata)
make backfill ARGS="--months 1"     # small first run, resumable; then ARGS="--months 12"
make run                            # Backtest page: /backtest   Data page: /data
```

Downloading can also be started from the **Data** page after pressing *Connect to Groww*. The
download is read-only (candles, expiries and contract lists only), throttled, resumable (finished
days are not fetched again) and stops by itself after 15 consecutive errors. It checks how Groww
labels a candle (opening or closing minute) and aborts if that cannot be determined.

> **Known blocker for a free Groww plan.** Groww's *market-data* endpoints (historical candles,
> expiries, contracts) answer `HTTP 403 Access forbidden for this request` unless the account has
> the paid data / backtesting access, while profile and funds work. That is a plan permission, not
> a bug here. Use the free dataset in the next section, or the generic
> [CSV importer](#use-data-from-another-provider-csv). The Groww timestamp and symbol formats could
> therefore not be confirmed against live responses; the code detects the timestamp convention and
> refuses to continue if it is unclear.

### Use free real data (Hugging Face)

[`thetrademarkk/india-index-options-1m`](https://huggingface.co/datasets/thetrademarkk/india-index-options-1m)
is a free community dataset of NIFTY 1-minute index candles and weekly option candles. No account is
needed.

```bash
make hf-data ARGS="--months 12"      # downloads ~175 MB of Parquet, converts it (about 2 minutes)
make import-csv ARGS="--spot data/huggingface/nifty_spot.csv --options data/huggingface/nifty_options.csv.gz --expiries data/huggingface/expiries.csv --label huggingface --dry-run"
make import-csv ARGS="--spot data/huggingface/nifty_spot.csv --options data/huggingface/nifty_options.csv.gz --expiries data/huggingface/expiries.csv --label huggingface"   # about 8 minutes
make run
```

`make hf-data` prints these two import commands itself. Read the caveats before trusting a result:

* **Licence: CC BY-NC 4.0.** You must credit the dataset's authors and may not use it commercially.
  Check that this fits before you publish anything built on it (see the website section below).
* **Unverified community data** ("educational use only, verify against exchange data" in the
  dataset's own words). It was never compared with NSE here. Treat results as a rehearsal, not proof.
* **The window ends where the option data ends.** The index runs to July 2026, but the option files
  stop in early June 2026 and have no files for the weeks after 9 June, so the script ends the
  window at 3 Jun 2026 (it says so when it runs). The default window is the 12 months before that.
* **Gaps are listed, not guessed.** The dataset has no file for the 20 Oct 2025 expiry (the expiry
  moved to Monday around Diwali). It is added to the expiry list, taken from an independent
  list, so the engine does not trade the next expiry instead. The three days around it become listed
  skipped signals. Far-from-the-money strikes are sparse; a missing option price is a skip.
* Days without all 375 index minutes (for example the Diwali Muhurat session) are excluded and listed.
* The files go to `data/huggingface/` (git-ignored). Downloads are resumable and checked by size.

### Use data from another provider (CSV)

If Groww's market data is not available, any provider that can export **1-minute candles** for the
NIFTY 50 index and for NIFTY weekly options will do. (A MetaTrader terminal is not such a source:
forex/CFD brokers have no NSE option chains, and a CFD "NIFTY" is not the NSE index.)

```bash
# 1. check the files first: reads everything, prints what it found, writes nothing
make import-csv ARGS="--spot nifty.csv --options options.csv.gz --expiries expiries.csv --dry-run"
# 2. then import for real (same command without --dry-run; safe to repeat)
make import-csv ARGS="--spot nifty.csv --options options.csv.gz --expiries expiries.csv"
make run
```

| File        | Needs                                                                                        |
| ----------- | -------------------------------------------------------------------------------------------- |
| `--spot`    | one row per minute: a time (`timestamp`, or `date` + `time`), `open high low close`, optional `volume` |
| `--options` | the same, plus the contract as `expiry` + `strike` + `option_type` (CE/PE or CALL/PUT), or a `symbol` like `NSE-NIFTY-12May26-24000-CE`; optional `oi` |
| `--expiries`| one expiry date per line (a header is fine)                                                  |

* Column names are case-insensitive and extra columns are ignored. Files can be `.csv` or `.csv.gz`;
  `--delimiter tab|semicolon|pipe` for other separators.
* Times: `2026-05-12 09:15`, `2026-05-12T09:15:00+05:30`, `12-05-2026 09:15` (**day first**) or epoch
  seconds. A time with an offset (for example UTC) is converted to IST; one without is taken as IST.
  Dates that are alone (no time), seconds other than `:00` and prices with more than 4 decimals are
  rejected rather than rounded.
* **Start or end of the minute?** Providers differ (09:15 or 09:16 for the first candle). The
  importer works it out from the index's first and last candle of each day and shifts end-stamped
  data back one minute. If it cannot tell, it stops and asks for `--timestamps start|close`.
* **Nothing half-written:** every file is read and checked completely before the first row is
  stored, and the first unreadable row stops the import with its file and line number
  (`--skip-bad-rows` skips and counts them instead). Re-running is safe.
* **Other indices** (BANKNIFTY and so on) are skipped and counted when the file says which index a
  row belongs to (`underlying` / `name` / `symbol` column). Strikes far outside the index's range
  raise a warning.
* **Expiries** must be given with `--expiries`; `--derive-expiries` takes them from the option files
  but only gives the right nearest expiry if the files hold every weekly expiry in the range.
* Include about two weeks of index data **before** the first day you want to test (the EMAs
  need it), and option candles for the contracts the strategy would trade (ATM and 5 strikes ITM on
  each signal day). Days without all 375 index minutes are excluded, and missing option prices
  become listed skipped signals, as with Groww data.
* The Data page shows an import in its download log (detail `csv: <label>`). Do not import into a
  `make demo` database: simulated data is never mixed with real data.

### Rules the engine follows

The full rulebook lives in the docstring of `app/strategy/engine.py` and in the **How it works**
dialog on the page. In short:

| Topic            | Rule                                                                                         |
| ---------------- | -------------------------------------------------------------------------------------------- |
| Signal           | 9-EMA crosses the 21-EMA on a **closed** NIFTY bar (5 / 15 / 30 min, built from 1-minute candles anchored at 09:15). EMAs run continuously across days. |
| Entry window     | Start 09:30 / 10:00 / 10:30; no new entries after 11:30 ... 14:30 (every 30 min). A cross before the start does not carry in. |
| Fill             | Decision at bar close; fill at the **open of the option's 1-minute candle** starting then (up to 5 candles later, else the signal is skipped and listed). |
| Exit             | Opposite crossover, or mandatory square-off at 15:00. An opposite cross inside the window reverses immediately. A cross completing at/after 15:00 is not acted on. |
| Contracts        | Nearest weekly expiry on or after the trade date (expiry day = 0 DTE, traded). ATM = signal close rounded to 50 (ties up); ITM 5 = five strikes (250 points) in the money. |
| Modes            | Deep ITM buy, ATM buy (bull = call, bear = put), ATM sell (bull = sell put, bear = sell call). |
| Size             | 1 lot = 65 units.                                                                            |
| Friction         | 0.5% of premium + 0.5 points per leg (buys round up, sells round down to the 0.05 tick, never below 0.05), then brokerage, STT, exchange charges, SEBI fee, stamp duty and GST at the rates in force on the trade date. |
| Data gaps        | Days without exactly 375 spot minutes are excluded and listed; signals with missing option prices are skipped **and listed**; nothing is silently dropped. |
| Not modelled     | Stop-loss / target, bid-ask spread beyond the slippage setting, partial fills, margin, liquidity. |

Caveats worth knowing:

* The charge schedule (`app/strategy/costs.py`, `DEFAULT_SCHEDULE`) has dated rows. The rows from
  **2024-10-01** and **2026-04-01** were checked against published rates; older rows are marked
  *unverified* and the page shows that status next to each trade's costs. Check current rates with
  your broker before relying on net figures.
* Results are computed once for the widest entry window and then filtered, so changing the filters
  never changes a trade's own result; the *entry number* is its rank within the selected window.
* Whipsaw = an opposite-cross exit within 2 signal bars. A tie (net PnL exactly 0) is neither a
  win nor a loss.

### Pages and API

| Method | Path                                           | Purpose                                              |
| ------ | ---------------------------------------------- | ---------------------------------------------------- |
| GET    | `/backtest`                                    | Results page with filters, KPIs and the trade viewer |
| GET    | `/data`                                        | Download controls, stored-data summary, download log |
| GET    | `/api/backtest/meta`                           | Defaults, options, cost schedule, data range         |
| GET    | `/api/backtest/trades`                         | Filtered trades, KPIs, comparison, equity curve (ETag-cached) |
| GET    | `/api/backtest/trade/<timeframe>/<id>`         | One trade with spot and option candles               |
| GET    | `/api/backtest/skips`                          | Signals and days that did not become trades          |
| GET    | `/api/data/status`                             | What is stored and the state of the download job     |
| POST   | `/api/data/download`, `/api/data/stop`         | Start / stop the read-only download                  |

Keyboard on the Backtest page: `j` / `k` or arrow keys move through trades, `Home` / `End` jump to
the first / last. The URL holds the non-default filters and the selected trade (`#t=<id>`), so a
view can be bookmarked. Dark and light themes are available from the top bar.

**Filters.** Timeframe (15 min by default), strategy, outcome, entry window, crossover number, date
range and **days of the week** (`days=mon,tue,thu` in the URL; all seven are on by default, click a
day to leave it out). Weekdays only hide trades: every day is still simulated, because the EMAs run
across days. The odd Saturday or Sunday session (budget day, Muhurat) has its own button. The page
also states where the data came from (for the Hugging Face import: the dataset name, a link and its
"not verified against NSE" caveat).

### Tests

`make check` runs ruff, mypy `--strict`, the Python suite (engine golden cases, differential and
window-invariance tests, cost-schedule tests, store/backfill tests on a real throw-away Postgres,
route tests) and, if Node 22+ is installed, the browser-helper tests (`make test-js`).
`python -m scripts.mutation_check` deliberately breaks the engine in 14 ways and confirms the tests
catch every one.

## How the login works

1. `pyotp` computes the current 6-digit code from your TOTP secret.
2. `GrowwAPI.get_access_token(api_key=<TOTP token>, totp=<code>)` returns an access token.
   Groww expires access tokens every day at **06:00 IST**.
3. The token is kept **in memory only**. The app reuses it for every request and forgets it on
   **Disconnect** or when the server stops. After your first **Connect** it renews itself - when
   the token is about to expire (including the daily reset), or once if Groww rejects it
   mid-session - for as long as the server keeps running. It never logs in per request, because
   Groww rate-limits logins.
4. Data calls always carry an explicit timeout (default 10 s, `GROWW_REQUEST_TIMEOUT`); the SDK
   itself would otherwise wait forever.

## JSON API

All endpoints are local and read-only. Data responses look like
`{"data": ..., "fetched_at": "2026-11-03T22:45:00+05:30"}`.

| Method | Path                                                  | Purpose                                         |
| ------ | ----------------------------------------------------- | ----------------------------------------------- |
| GET    | `/`                                                   | The dashboard                                   |
| GET    | `/api/accounts`                                       | Saved accounts (keys masked) and the broker list |
| POST   | `/api/accounts`                                       | Add an account: `{broker, label, credentials}`  |
| DELETE | `/api/accounts/<id>`                                  | Remove an account                               |
| GET    | `/api/status`                                         | Session state; never logs in                    |
| POST   | `/api/connect`                                        | Log in (the only call that does)                |
| POST   | `/api/disconnect`                                     | Forget the token                                |
| GET    | `/api/profile`, `/api/funds`, `/api/holdings`, `/api/positions` | Account data (401 `not_connected` until you connect) |

Errors are `{"error": {"code": "...", "message": "..."}}` with stable codes: `credentials_error`,
`not_connected`, `login_failed`, `session_expired`, `forbidden`, `rate_limited`,
`upstream_timeout`, `upstream_error`. POST requests must send `X-Requested-With: fetch`
(CSRF protection); the dashboard does this for you.

## Security model

* Binds to `127.0.0.1` only, and rejects any `Host` header other than `localhost`, `127.0.0.1`
  and `::1` (DNS-rebinding defence; `ALLOWED_HOSTS=name1,name2` adds more).
* There is no login screen on the dashboard because it is meant to be reachable only from this
  machine. **Do not** start it with `--host 0.0.0.0`, tunnel it or port-forward it: anyone who can
  reach it can see your account details.
* Strict Content-Security-Policy (no inline scripts or styles, nothing loaded from a CDN), plus
  `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer` and `Cache-Control: no-store`
  on `/api/*`.
* Secrets are scrubbed from error messages, never written to logs and never sent to the browser.
* Read-only by design: no order-placement code exists.

## Publish a static website (GitHub Pages)

GitHub Pages only serves files, so it cannot run this Flask app. `make site` instead writes a
read-only copy, with the landing page and the real backtest, into `../AutomatedTradingSite`:

```bash
make site                                  # or: make site ARGS="--out /some/folder"
cd ../AutomatedTradingSite && python3 -m http.server 8000     # try it at http://localhost:8000
```

To serve it from this repository instead, build into `docs/` (`make site ARGS="--out docs"`), commit
it, and set **Settings → Pages → Branch `main`, folder `/docs`**. Without that folder, Pages shows
this readme instead of the website.

* **The website opens on the backtest** (`index.html`). A bar fixed to the bottom of that page asks
  "Like what you see?", links to the sales page (`home.html`, also under **Home** in the menu) and has
  the Upwork button. The sales page links back to the backtest.
* The backtest filters run in the browser (`static/js/static-api.js`), on JSON files holding the
  stored results and one chart file per trade (about 19 MB compressed, a few KB per page view).
* **It is the same maths as the live app**: `tests/test_static_api.py` runs the same trades through
  both under Node for hundreds of random filter combinations and requires identical JSON, down to
  the paise (money is added as exact integers and rounded half-to-even like Python's `Decimal`).
* The site has no accounts page, no keys and no way to place an order. The Upwork link is
  `CONTACT_URL`.
* **Before making it public, check the data licence.** The charts include the dataset's 1-minute
  candles (CC BY-NC 4.0: non-commercial, credit required), and a site that advertises paid work is
  commercial. Use data you are allowed to publish, or keep the site private.

## Project layout

```text
app/
  __init__.py        app factory and wiring (create_app, build_service)
  config.py          settings read from environment variables
  credentials.py     keys.txt parsing and validation
  errors.py          error types -> HTTP status + stable error codes
  broker.py          the Protocols the rest of the app depends on (BrokerClient, Gateway)
  groww_sdk.py       the ONLY module that imports growwapi; absorbs the SDK's quirks
  groww_service.py   the session: TOTP login, token cache, renewal, thread safety
  security.py        Host / CSRF checks and security headers
  routes.py          dashboard page + JSON API
  templates/, static/  UI for all pages (plain HTML, CSS and ES modules; no build step;
                     static/vendor holds TradingView lightweight-charts 4.2.3, Apache-2.0)
  strategy/          pure engine: bars, EMA signals, option selection, fills, costs, metrics
  history/           Groww candle parsing, data-quality checks, Postgres store, backfill, jobs,
                     CSV import (csv_import.py)
  backtest/          cached runs, filtering, presentation, routes for /backtest and /data,
                     export.py (results as JSON for the static website)
  db/                connection helper and plain-SQL migrations (app/db/migrations)
scripts/             check_connection (make smoke), backfill, import_csv, hf_dataset, demo_data, mutation_check,
                     build_site (make site)
tests/               pytest suite; tests/js holds the Node tests for the browser helpers
tests/                        pytest suite built on in-memory fakes
```

To add a new broker capability: add the method to `BrokerClient` in `broker.py`, implement it in
`groww_sdk.py`, expose it through `GrowwService`, add a route, and cover it with a test using the
fakes in `tests/fakes.py`. Anything beyond reading (orders) deserves its own explicit
confirmation step and tests before it is wired to a button.

## Why Python 3.13

`growwapi` 1.5.0 officially supports Python 3.9-3.13 (it pins `pandas<3` and `protobuf<6`), so
3.13 is the newest interpreter it supports. Only the direct dependencies are pinned in
`requirements.txt`; transitive packages float inside the ranges Flask and `growwapi` declare.
`.python-version` is provided for pyenv users. Virtualenvs are not relocatable - if you move this
folder, run `rm -rf .venv && make install`.

## Troubleshooting

| Symptom                                           | Likely cause and fix                                                                                          |
| ------------------------------------------------- | ------------------------------------------------------------------------------------------------------------- |
| "Credentials file not found" / "No TOTP token ..." | Check the labels in `keys.txt` against the table above.                                                       |
| "The TOTP secret is not valid base32"             | The secret should be about 32 characters of letters A-Z and digits 2-7; re-copy it from Groww.                |
| Login fails although the keys look right         | TOTP codes depend on the clock: turn on automatic date & time in macOS settings. Or the TOTP token was regenerated or revoked on Groww - create a new one and update `keys.txt`. |
| `rate_limited`                                    | Groww throttled the request. Wait a minute; avoid repeatedly pressing Connect.                                |
| Dashboard says "Not connected" after a restart    | The token lives in memory only, so a restarted server starts disconnected. Press **Connect to Groww**.        |
| `upstream_timeout`                                | Groww was slow; press Refresh, or raise `GROWW_REQUEST_TIMEOUT`.                                              |
| Holdings / Positions are empty                    | That is what Groww returned. The tables fill in once the account has holdings or open positions.             |

## Ideas for next steps

* Live prices and P&L through Groww's market-data endpoints.
* Order placement behind an explicit confirm step, with a dry-run mode first.
* Paper trading, then (much later, behind explicit confirmation) live orders: the engine is pure
  and takes bars in, trades out, so a live runner can reuse it.
* A scheduler for unattended jobs (note that the daily 06:00 IST token reset applies).
