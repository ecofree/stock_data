-- P0: pre-create checkpoint/log relations used by the canonical close path.
-- The production runner sets KPL_RUNTIME_SCHEMA_READY=1 for child collectors;
-- these relations must therefore already exist and collectors must not issue
-- CREATE/ALTER statements while the pipeline lock is held.

CREATE TABLE IF NOT EXISTS _collect_log (
    table_name VARCHAR,
    endpoint VARCHAR,
    rows_inserted INTEGER,
    status VARCHAR,
    fetched_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS intraday_stock_flow_batch (
    trade_date DATE PRIMARY KEY,
    run_id VARCHAR,
    provider VARCHAR,
    expected_rows INTEGER,
    fetched_rows INTEGER,
    expected_pages INTEGER,
    fetched_pages INTEGER,
    coverage_pct DOUBLE,
    status VARCHAR,
    last_error VARCHAR,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    source_rows INTEGER,
    unavailable_rows INTEGER
);

CREATE TABLE IF NOT EXISTS intraday_stock_flow_reconciliation (
    trade_date DATE PRIMARY KEY,
    primary_provider VARCHAR,
    primary_rows INTEGER,
    reference_provider VARCHAR,
    reference_rows INTEGER,
    overlap_rows INTEGER,
    primary_only_rows INTEGER,
    reference_only_rows INTEGER,
    overlap_reference_pct DOUBLE,
    status VARCHAR,
    last_error VARCHAR,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS intraday_stock_flow_page_checkpoint (
    trade_date DATE,
    page_no INTEGER,
    pages_expected INTEGER,
    status VARCHAR,
    rows_written INTEGER DEFAULT 0,
    last_error VARCHAR,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, page_no)
);

CREATE TABLE IF NOT EXISTS intraday_stock_flow_missing (
    trade_date DATE,
    stock_code VARCHAR,
    provider VARCHAR,
    reason VARCHAR,
    detected_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, stock_code, provider)
);

CREATE TABLE IF NOT EXISTS intraday_stock_flow_exchange_coverage (
    trade_date DATE,
    exchange VARCHAR,
    provider VARCHAR,
    expected_rows INTEGER,
    fetched_rows INTEGER,
    coverage_pct DOUBLE,
    status VARCHAR,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, exchange, provider)
);

CREATE TABLE IF NOT EXISTS intraday_sector_flow_batch (
    trade_date DATE PRIMARY KEY,
    run_id VARCHAR,
    provider VARCHAR,
    expected_rows INTEGER,
    fetched_rows INTEGER,
    expected_pages INTEGER,
    fetched_pages INTEGER,
    coverage_pct DOUBLE,
    status VARCHAR,
    last_error VARCHAR,
    updated_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS intraday_sector_flow_taxonomy (
    trade_date DATE,
    taxonomy VARCHAR,
    provider VARCHAR,
    expected_rows INTEGER,
    fetched_rows INTEGER,
    coverage_pct DOUBLE,
    status VARCHAR,
    last_error VARCHAR,
    updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, taxonomy)
);

CREATE TABLE IF NOT EXISTS review_supplement_batch (
    trade_date DATE PRIMARY KEY,
    attempted_at TIMESTAMP,
    status VARCHAR,
    rows_written INTEGER,
    api_success INTEGER,
    api_error INTEGER
);

CREATE TABLE IF NOT EXISTS auction_quote_snapshot (
    date DATE,
    stock_code VARCHAR,
    quote_time VARCHAR,
    indicative_price DOUBLE,
    cumulative_volume BIGINT,
    volume_unit VARCHAR,
    bid1_price DOUBLE,
    bid1_volume BIGINT,
    ask1_price DOUBLE,
    ask1_volume BIGINT,
    order_imbalance DOUBLE,
    provider VARCHAR,
    raw_json VARCHAR,
    fetched_at TIMESTAMP DEFAULT current_timestamp
);

ALTER TABLE IF EXISTS intraday_stock_flow_batch
    ADD COLUMN IF NOT EXISTS source_rows INTEGER;
ALTER TABLE IF EXISTS intraday_stock_flow_batch
    ADD COLUMN IF NOT EXISTS unavailable_rows INTEGER;
ALTER TABLE IF EXISTS auction_quote_snapshot
    ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;

CREATE UNIQUE INDEX IF NOT EXISTS uq_auction_quote_snapshot
    ON auction_quote_snapshot(date, stock_code, quote_time, provider);
