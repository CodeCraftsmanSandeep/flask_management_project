-- Small facts about the downloaded data that must survive between runs, e.g. which timestamp
-- convention Groww uses (so later downloads are normalised exactly like the first one).
CREATE TABLE meta (
    key    text PRIMARY KEY,
    value  text NOT NULL
);
