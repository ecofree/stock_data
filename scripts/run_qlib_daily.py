"""Daily frozen-model inference and candidate fusion; no training or orders."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.predict_qlib_daily import predict_daily
from trade_system.candidate_pool import build_candidate_pool
from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables
from trade_system.paper_execution import ensure_paper_tables
from trade_system.ml.feature_artifacts import resolve_feature_path


def run(db_path: str | Path, *, feature_file: str | Path, trade_date: str | None = None,
        out: str | Path, allow_shadow: bool = False, model_id: str | None = None) -> dict:
    db = Path(db_path)
    ensure_qlib_shadow_tables(db)
    ensure_paper_tables(db)
    con = duckdb.connect(str(db), read_only=True)
    try:
        if model_id:
            row = con.execute(
                "SELECT model_id, model_file_ref, status FROM qlib_model_registry "
                "WHERE model_id=? AND (status='champion' OR (? AND status IN ('challenger','shadow'))) "
                "LIMIT 1", [model_id, allow_shadow]
            ).fetchone()
        else:
            row = con.execute(
                "SELECT model_id, model_file_ref, status FROM qlib_model_registry "
                "WHERE status='champion' OR (? AND status IN ('challenger','shadow')) "
                "ORDER BY CASE status WHEN 'champion' THEN 0 WHEN 'challenger' THEN 1 ELSE 2 END, "
                "train_end DESC NULLS LAST, model_id LIMIT 1", [allow_shadow]
            ).fetchone()
    finally:
        con.close()
    if not row:
        result = {"status": "no_champion", "signal_impact": "disabled", "reason": "model_promotion_gate_closed"}
    elif not resolve_feature_path(feature_file).exists():
        result = {"status": "missing_features", "signal_impact": "disabled", "reason": str(feature_file)}
    else:
        model_id, model_file, model_status = str(row[0]), Path(str(row[1])), str(row[2] or "shadow")
        if not model_file.is_absolute():
            model_file = db.parent / model_file
        prediction = predict_daily(
            db,
            feature_file,
            model_file,
            model_id=model_id,
            trade_date=trade_date,
            allow_shadow=allow_shadow,
        )
        candidate = build_candidate_pool(db, model_id=model_id, trade_date=prediction["trade_date"])
        result = {
            "status": "success",
            "signal_impact": "disabled" if model_status == "shadow" else "candidate_only",
            "model_status": model_status,
            "prediction": prediction,
            "candidate": candidate,
        }
    target = Path(out)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run approved QLib daily inference without training or orders.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--features", default=str(root / "reports" / "qlib_features_2026_exec.parquet"))
    parser.add_argument("--trade-date")
    parser.add_argument("--model-id", default="")
    parser.add_argument("--allow-shadow", action="store_true",
                        help="Use the best registered shadow/challenger model when no champion exists; output remains non-executable.")
    parser.add_argument("--out", default=str(root / "reports" / "qlib_daily_latest.json"))
    args = parser.parse_args()
    result = run(args.db, feature_file=args.features, trade_date=args.trade_date, out=args.out,
                 allow_shadow=args.allow_shadow, model_id=args.model_id or None)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
