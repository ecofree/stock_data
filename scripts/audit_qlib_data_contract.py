"""Read-only Phase 1 audit for QLib data and target contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0])


def audit(db_path: str | Path, *, feature_metadata: str | Path | None = None) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        result = {"status": "ready_for_research", "checks": {}, "blocking_reasons": []}
        for name, table_name, query in (
            ("daily_history", "tushare_daily", "SELECT count(*), count(distinct date), min(date), max(date), count(distinct stock_code) FROM tushare_daily"),
            ("stock_flow_features", "qlib_stock_flow_features", "SELECT count(*), count(distinct trade_date), min(trade_date), max(trade_date), count(distinct stock_code) FROM qlib_stock_flow_features"),
            ("sector_flow_features", "qlib_sector_flow_features", "SELECT count(*), count(distinct trade_date), min(trade_date), max(trade_date), count(distinct sector_code) FROM qlib_sector_flow_features"),
        ):
            if _table(con, table_name):
                row = con.execute(query).fetchone()
                result["checks"][name] = {
                    "rows": int(row[0] or 0),
                    "dates": int(row[1] or 0),
                    "start": str(row[2])[:10] if row[2] else None,
                    "end": str(row[3])[:10] if row[3] else None,
                    "instruments": int(row[4] or 0),
                }
            else:
                result["checks"][name] = {"missing": True}
                result["blocking_reasons"].append(f"missing_{name}")
        if _table(con, "tushare_daily"):
            coverage = con.execute(
                """
                WITH counts AS (
                    SELECT CAST(date AS DATE) AS trade_date, count(DISTINCT stock_code) AS instruments
                    FROM tushare_daily GROUP BY date
                )
                SELECT count(*) FILTER (WHERE instruments < CASE WHEN (SELECT coalesce(max(instruments),0) FROM counts) >= 1000 THEN 1000 ELSE 1 END),
                       min(trade_date) FILTER (WHERE instruments < CASE WHEN (SELECT coalesce(max(instruments),0) FROM counts) >= 1000 THEN 1000 ELSE 1 END),
                       max(trade_date) FILTER (WHERE instruments < CASE WHEN (SELECT coalesce(max(instruments),0) FROM counts) >= 1000 THEN 1000 ELSE 1 END)
                FROM counts
                """
            ).fetchone()
            result["checks"]["daily_coverage"] = {
                "partial_dates": int(coverage[0] or 0),
                "partial_start": str(coverage[1])[:10] if coverage[1] else None,
                "partial_end": str(coverage[2])[:10] if coverage[2] else None,
                "qlib_export_excludes_partial_dates": True,
            }
            if coverage[0]:
                result["blocking_reasons"].append("partial_daily_history_dates")
        if _table(con, "qlib_stock_flow_features"):
            statuses = con.execute("SELECT coalesce(quality_status,'') AS quality_status, count(*) FROM qlib_stock_flow_features GROUP BY 1").fetchall()
            result["checks"]["stock_flow_quality_status"] = {str(row[0]): int(row[1]) for row in statuses}
        if _table(con, "ths_concept_stock_history"):
            result["checks"]["ths_membership"] = dict(zip(
                ("rows", "verified_rows", "dates", "latest"),
                con.execute("SELECT count(*), sum(CASE WHEN date_verified THEN 1 ELSE 0 END), count(distinct trade_date), max(trade_date) FROM ths_concept_stock_history").fetchone(),
            ))
        else:
            result["blocking_reasons"].append("missing_point_in_time_ths_membership")
    finally:
        con.close()
    metadata_path = Path(feature_metadata) if feature_metadata else None
    if metadata_path and metadata_path.exists():
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        features = metadata.get("feature_columns") or []
        result["checks"]["feature_metadata"] = {
            "rows": metadata.get("rows"),
            "labeled_rows": metadata.get("labeled_rows"),
            "label_mode": metadata.get("label_mode"),
            "feature_count": len(features),
            "label_in_features": any("label" in str(column).lower() for column in features),
        }
        if result["checks"]["feature_metadata"]["label_in_features"]:
            result["blocking_reasons"].append("label_column_in_feature_columns")
    stock_dates = result["checks"].get("stock_flow_features", {}).get("dates", 0)
    if stock_dates < 300:
        result["blocking_reasons"].append("flow_history_short_for_formal_promotion")
    if result["blocking_reasons"]:
        result["status"] = "research_only"
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Audit QLib data/label contract without modifying the database.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--feature-metadata", default=str(root / "reports" / "qlib_features_2026_exec.metadata.json"))
    parser.add_argument("--out", default=str(root / "reports" / "qlib_data_contract_latest.json"))
    args = parser.parse_args()
    result = audit(args.db, feature_metadata=args.feature_metadata)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
