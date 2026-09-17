-- Preserve historical raw values and provenance; unknown units stay unknown.
ALTER TABLE IF EXISTS tushare_index_daily ADD COLUMN IF NOT EXISTS volume_unit VARCHAR;
ALTER TABLE IF EXISTS tushare_index_daily ADD COLUMN IF NOT EXISTS amount_unit VARCHAR;
ALTER TABLE IF EXISTS tushare_index_daily ADD COLUMN IF NOT EXISTS adjustment VARCHAR;
ALTER TABLE IF EXISTS tushare_index_daily ADD COLUMN IF NOT EXISTS provider VARCHAR;
