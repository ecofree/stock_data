-- P0: replace fixed THS concept floors and mutable probe rows with explicit
-- snapshot/run provenance.  The bootstrap rows preserve already-certified
-- historical snapshots; new collectors must write status=success with the
-- fetched catalogue count.
CREATE TABLE IF NOT EXISTS ths_concept_snapshot_expectation (
    trade_date DATE PRIMARY KEY,
    expected_concepts INTEGER NOT NULL,
    provider VARCHAR NOT NULL,
    catalog_hash VARCHAR,
    status VARCHAR DEFAULT 'success',
    fetched_at TIMESTAMP DEFAULT current_timestamp
);

CREATE TABLE IF NOT EXISTS api_endpoint_probe_run (
    run_id VARCHAR PRIMARY KEY,
    base_url VARCHAR,
    probe_date DATE,
    endpoint_count INTEGER,
    status VARCHAR,
    started_at TIMESTAMP,
    completed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT current_timestamp
);

ALTER TABLE IF EXISTS intraday_stock_flow_reconciliation
    ADD COLUMN IF NOT EXISTS overlap_sign_disagreement INTEGER DEFAULT 0;
ALTER TABLE IF EXISTS intraday_stock_flow_reconciliation
    ADD COLUMN IF NOT EXISTS overlap_sign_disagreement_pct DOUBLE;
ALTER TABLE IF EXISTS intraday_stock_flow_reconciliation
    ADD COLUMN IF NOT EXISTS mean_abs_main_net_diff DOUBLE;
ALTER TABLE IF EXISTS intraday_stock_flow_reconciliation
    ADD COLUMN IF NOT EXISTS value_status VARCHAR DEFAULT 'not_observed';
