"""Create the small set of indexes used by the live operator path."""

from __future__ import annotations

import argparse
from pathlib import Path
import duckdb


INDEX_SPECS = (
    ("idx_realtime_candidate_date_fetched", "realtime_candidate_pool_snapshot", ("trade_date", "fetched_at")),
    ("idx_intraday_stock_batch_date_updated", "intraday_stock_flow_batch", ("trade_date", "updated_at")),
    ("idx_intraday_sector_batch_date_updated", "intraday_sector_flow_batch", ("trade_date", "updated_at")),
    ("idx_pipeline_task_date_phase_updated", "pipeline_task_audit", ("trade_date", "phase", "updated_at")),
    ("idx_ths_member_date_status_updated", "ths_concept_member_checkpoint", ("trade_date", "status", "updated_at")),
    ("idx_operator_outcome_date_code", "operator_trade_outcome", ("trade_date", "stock_code")),
)


def ensure_indexes(db_path: str | Path) -> dict[str, int]:
    con = duckdb.connect(str(db_path))
    created = 0
    skipped = 0
    try:
        for name, table, columns in INDEX_SPECS:
            present = int(
                con.execute(
                    "SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table]
                ).fetchone()[0]
            )
            if not present:
                skipped += 1
                continue
            available = {row[0] for row in con.execute(f"DESCRIBE \"{table}\"").fetchall()}
            if not set(columns).issubset(available):
                skipped += 1
                continue
            before = int(con.execute("SELECT count(*) FROM duckdb_indexes() WHERE index_name=?", [name]).fetchone()[0])
            if before:
                skipped += 1
                continue
            column_sql = ", ".join(f'"{column}"' for column in columns)
            con.execute(f'CREATE INDEX "{name}" ON "{table}" ({column_sql})')
            created += 1
        return {"created": created, "skipped": skipped}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure operational DuckDB indexes.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    args = parser.parse_args()
    result = ensure_indexes(args.db)
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
