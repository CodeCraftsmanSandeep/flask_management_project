PYTHON ?= python3.13
VENV   ?= .venv
BIN    := $(VENV)/bin
HOST   ?= 127.0.0.1
PORT   ?= 5001

# Project-local PostgreSQL (its own data folder; nothing system-wide is touched).
PGBIN  ?= /Library/PostgreSQL/17/bin
PGDATA ?= pgdata
PGPORT ?= 5433
DBNAME ?= trading
PGPSQL := $(PGBIN)/psql -h 127.0.0.1 -p $(PGPORT) -U trading

.DEFAULT_GOAL := help
.PHONY: help venv install run dev smoke lint format typecheck test check clean \
	db-init db-start db-stop db-status db-psql db-reset migrate backfill import-csv hf-data demo demo-data test-js

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk -F ':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

$(BIN)/python:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip

venv: $(BIN)/python ## Create the virtualenv (.venv)

install: venv ## Install pinned runtime + dev dependencies
	$(BIN)/python -m pip install -r requirements-dev.txt

run: db-start migrate ## Start the app on http://127.0.0.1:5001 (localhost only)
	$(BIN)/python -m flask --app wsgi run --host $(HOST) --port $(PORT)

dev: db-start migrate ## Same as run, with debug mode + auto-reload
	$(BIN)/python -m flask --app wsgi run --host $(HOST) --port $(PORT) --debug

smoke: ## Log in to Groww and fetch profile/funds/holdings/positions (read-only, prints no values)
	$(BIN)/python -m scripts.check_connection

db-init: ## Create the local Postgres cluster in ./pgdata (once)
	@test -f $(PGDATA)/PG_VERSION || { $(PGBIN)/initdb -D $(PGDATA) -U trading --auth=trust -E UTF8 --locale=C >/dev/null && echo "cluster created in $(PGDATA)"; }

db-start: db-init ## Start Postgres on 127.0.0.1:$(PGPORT) and create the database if missing
	@$(PGBIN)/pg_ctl -D $(PGDATA) status >/dev/null 2>&1 || $(PGBIN)/pg_ctl -D $(PGDATA) -l $(PGDATA)/server.log -w -o "-p $(PGPORT) -c listen_addresses=127.0.0.1 -c unix_socket_directories=''" start >/dev/null
	@$(PGPSQL) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$(DBNAME)'" | grep -q 1 || $(PGPSQL) -d postgres -qc "CREATE DATABASE $(DBNAME)"
	@echo "postgres is up on 127.0.0.1:$(PGPORT) (database: $(DBNAME))"

db-stop: ## Stop the local Postgres
	@$(PGBIN)/pg_ctl -D $(PGDATA) -m fast stop || true

db-status: ## Is the local Postgres running?
	@$(PGBIN)/pg_ctl -D $(PGDATA) status || true

db-psql: ## Open a psql shell on the trading database
	$(PGPSQL) -d $(DBNAME)

db-reset: db-start ## DANGER: drop and recreate the trading database (deletes all downloaded data)
	$(PGPSQL) -d postgres -qc "DROP DATABASE IF EXISTS $(DBNAME)" -c "CREATE DATABASE $(DBNAME)"

migrate: db-start ## Apply database migrations
	$(BIN)/python -m app.db.migrate

backfill: db-start migrate ## Download NIFTY 1-minute data from Groww into Postgres (resumable). ARGS="--months 12"
	$(BIN)/python -m scripts.backfill $(ARGS)

import-csv: db-start migrate ## Load 1-minute CSV files from any data provider. ARGS="--spot a.csv --options b.csv --expiries e.csv --dry-run"
	$(BIN)/python -m scripts.import_csv $(ARGS)

hf-data: ## Download free NIFTY 1-minute data (Hugging Face) as CSV for import-csv. ARGS="--months 12"
	$(BIN)/python -m scripts.hf_dataset $(ARGS)

demo-data: db-start ## Build a throw-away database of SIMULATED prices (not real) to try the app. ARGS="--days 120"
	$(BIN)/python -m scripts.demo_data $(ARGS)

demo: db-start ## Run the app on the simulated demo database (built first if missing)
	@$(PGPSQL) -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='trading_demo'" | grep -q 1 || $(MAKE) demo-data
	DATABASE_URL=postgresql://trading@127.0.0.1:$(PGPORT)/trading_demo $(BIN)/python -m flask --app wsgi run --host $(HOST) --port $(PORT)

lint: ## Ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format: ## Auto-fix lint issues and format code
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck: ## mypy (strict)
	$(BIN)/mypy

test: db-start ## pytest (needs the local Postgres; it is started for you)
	$(BIN)/pytest

test-js: ## Unit tests for the browser helpers (needs Node 22+)
	node --test tests/js

check: lint typecheck test ## Everything CI would run: lint, types, tests (+ JS tests when Node is installed)
	@if command -v node >/dev/null 2>&1; then $(MAKE) --no-print-directory test-js; else echo "node not found: skipping JS tests"; fi

clean: ## Remove caches (keeps .venv and pgdata)
	rm -rf .pytest_cache .mypy_cache .ruff_cache
	find . -name __pycache__ -type d -not -path "./$(VENV)/*" -prune -exec rm -rf {} \;
