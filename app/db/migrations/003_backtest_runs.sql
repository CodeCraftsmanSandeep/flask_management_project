-- Cached backtest results, so the viewer opens instantly. One row per distinct
-- (engine version, strategy parameters, cost schedule, downloaded data). The cache key is a hash
-- of exactly those, so a change to any of them can never serve a stale result: it simply misses
-- and a fresh run is computed and stored.
CREATE TABLE backtest_runs (
    cache_key             text        PRIMARY KEY,
    timeframe             integer     NOT NULL,
    params_fingerprint    text        NOT NULL,
    engine_version        text        NOT NULL,
    data_version          text        NOT NULL,
    schedule_fingerprint  text        NOT NULL,
    trade_count           integer     NOT NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    payload               jsonb       NOT NULL
);
CREATE INDEX backtest_runs_recent ON backtest_runs (timeframe, params_fingerprint, created_at DESC);
