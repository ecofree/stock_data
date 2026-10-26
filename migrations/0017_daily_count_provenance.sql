-- Development schema only until a separately authorized production migration.
-- No historical provenance is guessed or backfilled.
ALTER TABLE IF EXISTS daily_summary ADD COLUMN IF NOT EXISTS count_provenance_json VARCHAR;
