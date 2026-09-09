-- P0: make raw market-data semantics explicit before any further backfill.
-- Existing rows remain untouched and therefore stay auditable as NULL/unknown.
ALTER TABLE IF EXISTS tushare_daily ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
ALTER TABLE IF EXISTS tushare_daily ADD COLUMN IF NOT EXISTS amount_unit VARCHAR;
ALTER TABLE IF EXISTS tushare_daily ADD COLUMN IF NOT EXISTS adjustment VARCHAR;
ALTER TABLE IF EXISTS tushare_daily ADD COLUMN IF NOT EXISTS provider VARCHAR;

ALTER TABLE IF EXISTS kline ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
ALTER TABLE IF EXISTS kline ADD COLUMN IF NOT EXISTS amount_unit VARCHAR;
ALTER TABLE IF EXISTS kline ADD COLUMN IF NOT EXISTS adjustment VARCHAR;
ALTER TABLE IF EXISTS kline ADD COLUMN IF NOT EXISTS provider VARCHAR;

ALTER TABLE IF EXISTS multi_source_kline ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
ALTER TABLE IF EXISTS multi_source_kline ADD COLUMN IF NOT EXISTS amount_unit VARCHAR;
ALTER TABLE IF EXISTS multi_source_kline ADD COLUMN IF NOT EXISTS adjustment VARCHAR;

ALTER TABLE IF EXISTS multi_source_quote ADD COLUMN IF NOT EXISTS total_mv_unit VARCHAR;
ALTER TABLE IF EXISTS multi_source_quote ADD COLUMN IF NOT EXISTS circ_mv_unit VARCHAR;

ALTER TABLE IF EXISTS auction_tick ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
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
ALTER TABLE IF EXISTS auction_quote_snapshot ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
