-- Operational indexes are schema state, not a per-run repair task.
-- The THS checkpoint is created by the bootstrap schema and is therefore safe
-- to migrate for every database shape. Optional batch tables are created by
-- their feature collectors and remain covered by the explicit recovery tool.
CREATE INDEX IF NOT EXISTS idx_ths_member_date_status_updated
    ON ths_concept_member_checkpoint(trade_date, status, updated_at);
