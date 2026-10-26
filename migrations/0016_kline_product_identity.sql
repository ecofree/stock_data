-- A price adjustment is a distinct product, not a revision of the raw price.
-- No row is deleted or relabelled. Conflicts abort the whole migration and
-- require explicit review; do not deduplicate different products together.
-- Standalone migration consumers may not have bootstrapped optional bar data.
CREATE TABLE IF NOT EXISTS multi_source_kline (
    source_date DATE, asset_type VARCHAR, asset_code VARCHAR, open DOUBLE,
    high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE,
    change_pct DOUBLE, provider VARCHAR, volume_unit VARCHAR, amount_unit VARCHAR,
    adjustment VARCHAR, fetched_at TIMESTAMP DEFAULT current_timestamp,
    is_stale BOOLEAN DEFAULT FALSE, raw_json VARCHAR
);
ALTER TABLE multi_source_kline ADD COLUMN IF NOT EXISTS adjustment VARCHAR;
CREATE UNIQUE INDEX IF NOT EXISTS uq_multi_source_kline_product
    ON multi_source_kline(source_date, asset_type, asset_code, provider, adjustment);
DROP INDEX IF EXISTS uq_multi_source_kline;
DROP INDEX IF EXISTS ix_multi_source_kline;
