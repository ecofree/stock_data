"""Run a frozen QLib model for one feature date (no training side effect)."""

from __future__ import annotations

import argparse
import json
import pickle
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.train_qlib_shadow import QlibFrameDataset
from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables, import_qlib_predictions_for_date
import duckdb


def _load_frame(path: Path) -> tuple[pd.DataFrame, dict]:
    metadata_path = path.with_suffix(".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    frame = pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
    frame["instrument"] = frame["instrument"].astype(str)
    return frame, metadata


def _latest_model_status(db_path: Path, model_id: str) -> str:
    ensure_qlib_shadow_tables(db_path)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute("SELECT status FROM qlib_model_registry WHERE model_id = ?", [model_id]).fetchone()
        if not row:
            raise RuntimeError(f"model is not registered: {model_id}")
        return str(row[0] or "shadow")
    finally:
        con.close()


def predict_daily(
    db_path: str | Path,
    feature_file: str | Path,
    model_file: str | Path,
    *,
    model_id: str,
    trade_date: str | None = None,
    allow_shadow: bool = True,
) -> dict[str, object]:
    db = Path(db_path)
    status = _latest_model_status(db, model_id)
    if status not in {"champion", "challenger", "shadow"}:
        raise RuntimeError(f"model status blocks prediction: {status}")
    if status == "shadow" and not allow_shadow:
        raise RuntimeError("shadow model prediction is disabled by policy")
    path = Path(feature_file)
    frame, metadata = _load_frame(path)
    features = list(metadata.get("feature_columns") or [])
    if not features:
        raise RuntimeError("feature metadata has no feature_columns")
    missing = [column for column in features if column not in frame.columns]
    if missing:
        raise RuntimeError("feature file missing columns: " + ", ".join(missing[:10]))
    selected_date = str(trade_date or frame["datetime"].max())[:10]
    daily = frame[frame["datetime"] == selected_date].copy()
    if daily.empty:
        raise RuntimeError(f"no feature rows for trade_date={selected_date}")
    medians = metadata.get("feature_medians") or {}
    for column in features:
        daily[column] = pd.to_numeric(daily[column], errors="coerce")
        fallback = float(medians.get(column, 0.0) or 0.0)
        daily[column] = (
            daily[column]
            .replace([float("inf"), float("-inf")], pd.NA)
            .fillna(fallback)
            .astype(float)
        )
    if "label_next_ret" not in daily.columns:
        daily["label_next_ret"] = pd.NA
    dataset = QlibFrameDataset(
        daily[["datetime", "instrument", "label_next_ret", *features]].copy(),
        features,
        selected_date,
        selected_date,
        selected_date,
    )
    with Path(model_file).open("rb") as handle:
        model = pickle.load(handle)
    prediction = model.predict(dataset, segment="all")
    pred_frame = pd.DataFrame({"score": prediction}).reset_index()
    pred_frame = pred_frame.rename(columns={"datetime": "trade_date", "instrument": "symbol"})
    pred_frame["trade_date"] = pred_frame["trade_date"].astype(str).str[:10]
    pred_frame["rank"] = pred_frame["score"].rank(method="first", ascending=False).astype(int)
    rows = pred_frame[["symbol", "score", "rank"]].to_dict("records")
    persisted = import_qlib_predictions_for_date(
        db,
        model_id=model_id,
        trade_date=selected_date,
        rows=[{**row, "horizon": metadata.get("prediction_horizon", "t1_exec")} for row in rows],
        predict_horizon=str(metadata.get("prediction_horizon", "t1_exec")),
    )
    return {
        "model_id": model_id,
        "model_status": status,
        "trade_date": selected_date,
        "rows": len(rows),
        "persisted": persisted,
        "signal_impact": "disabled" if status == "shadow" else "candidate_only",
    }


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Predict one date with a frozen QLib model.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--features", default=str(root / "reports" / "qlib_features_2026_exec.parquet"))
    parser.add_argument("--model-file", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--trade-date")
    parser.add_argument("--allow-shadow", action="store_true")
    args = parser.parse_args()
    result = predict_daily(
        args.db,
        args.features,
        args.model_file,
        model_id=args.model_id,
        trade_date=args.trade_date,
        allow_shadow=args.allow_shadow,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
