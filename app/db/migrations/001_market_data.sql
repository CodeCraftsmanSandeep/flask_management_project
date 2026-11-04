-- Market data downloaded from Groww. All timestamps are naive IST wall-clock times (NSE's clock);
-- a candle's ts is the START of its minute. Prices are exact decimals, never floats.

CREATE TABLE spot_candles (
    symbol  text           NOT NULL,
    ts      timestamp      NOT NULL,
    open    numeric(14, 4) NOT NULL,
    high    numeric(14, 4) NOT NULL,
    low     numeric(14, 4) NOT NULL,
    close   numeric(14, 4) NOT NULL,
    volume  bigint         NOT NULL DEFAULT 0,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE option_candles (
    symbol  text           NOT NULL,
    ts      timestamp      NOT NULL,
    open    numeric(14, 4) NOT NULL,
    high    numeric(14, 4) NOT NULL,
    low     numeric(14, 4) NOT NULL,
    close   numeric(14, 4) NOT NULL,
    volume  bigint         NOT NULL DEFAULT 0,
    oi      bigint,
    PRIMARY KEY (symbol, ts)
);

CREATE TABLE expiries (
    underlying  text NOT NULL,
    expiry      date NOT NULL,
    PRIMARY KEY (underlying, expiry)
);

CREATE TABLE contracts (
    symbol      text PRIMARY KEY,
    underlying  text NOT NULL,
    expiry      date NOT NULL
);
CREATE INDEX contracts_by_expiry ON contracts (underlying, expiry);

-- What has been fetched, so a download can stop and resume without repeating work.
--   spot       key = index symbol            day = trading day
--   option     key = option symbol           day = trading day
--   expiries   key = underlying              day = first day of the month asked for
--   contracts  key = underlying              day = the expiry date asked for
CREATE TABLE fetch_log (
    kind        text        NOT NULL CHECK (kind IN ('spot', 'option', 'expiries', 'contracts')),
    key         text        NOT NULL,
    day         date        NOT NULL,
    status      text        NOT NULL CHECK (status IN ('ok', 'empty', 'error')),
    rows        integer     NOT NULL DEFAULT 0,
    detail      text,
    fetched_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (kind, key, day)
);
