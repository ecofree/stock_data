-- P0: tables used by production collectors must exist before the run starts.
-- Collectors must not create or alter their own schema while holding the
-- process-wide pipeline lock.
CREATE TABLE IF NOT EXISTS realtime_candidate_pool_snapshot (
    trade_date DATE PRIMARY KEY,
    source VARCHAR,
    row_count INTEGER,
    stock_count INTEGER,
    status VARCHAR,
    fetched_at TIMESTAMP DEFAULT current_timestamp,
    error VARCHAR
);

CREATE TABLE IF NOT EXISTS eastmoney_limit_up_pool (
    date DATE,
    board_level INTEGER,
    stock_code VARCHAR,
    stock_name VARCHAR,
    limit_up_time VARCHAR,
    fetched_at TIMESTAMP DEFAULT current_timestamp,
    raw_json VARCHAR,
    PRIMARY KEY(date, stock_code)
);

CREATE TABLE IF NOT EXISTS auction_collection_batch (
    trade_date DATE PRIMARY KEY,
    attempted_at TIMESTAMP,
    stock_codes INTEGER,
    tick_rows INTEGER,
    anomaly_rows INTEGER,
    quote_rows INTEGER DEFAULT 0,
    status VARCHAR,
    last_error VARCHAR
);

ALTER TABLE IF EXISTS auction_collection_batch
    ADD COLUMN IF NOT EXISTS quote_rows INTEGER DEFAULT 0;
ALTER TABLE IF EXISTS auction_quote_snapshot
    ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
ALTER TABLE IF EXISTS xdf_margin_detail
    ADD COLUMN IF NOT EXISTS provider VARCHAR;

CREATE UNIQUE INDEX IF NOT EXISTS uq_auction_quote_snapshot
    ON auction_quote_snapshot(date, stock_code, quote_time, provider);
