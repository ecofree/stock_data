"""Run a frozen QLib model for one feature date (no training side effect)."""

from __future__ import annotations

import argparse
from datetime import date
import json
import pickle
from pathlib import Path
import sys
import hashlib

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.train_qlib_shadow import QlibFrameDataset
from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables, import_qlib_predictions_for_date
import duckdb
from trade_system.ml.feature_artifacts import resolve_feature_path


def _load_frame(path: Path, selected_date: str | None = None) -> tuple[pd.DataFrame, dict]:
    path = resolve_feature_path(path)
    metadata_path = path.with_suffix(".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    if path.suffix.lower() == ".parquet" and selected_date:
        # The daily research runner stores a partitioned Parquet dataset.  A
        # full pandas read would materialise millions of historical rows just
        # to score one session and can exhaust the desktop process.  PyArrow
        # pushes this predicate into the dataset scan and keeps the inference
        # memory bounded.  A single-file Parquet export also accepts filters.
        frame = pd.read_parquet(
            path,
            filters=[("datetime", "=", date.fromisoformat(selected_date))],
        )
    elif path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path, dtype={'instrument': 'string'})
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
    artifact_path = Path(model_file).resolve(strict=True)
    model_metadata = json.loads(artifact_path.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    if model_metadata.get('model_id') != model_id or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != model_metadata.get('model_sha256'):
        raise ValueError('model identity/hash is not verified; pickle will not be loaded')
    with duckdb.connect(str(db), read_only=True) as registry:
        model_ref = registry.execute('SELECT model_file_ref FROM qlib_model_registry WHERE model_id=?', [model_id]).fetchone()
    registered_path = Path(model_ref[0]) if model_ref and model_ref[0] else None
    if registered_path is None:
        raise ValueError('model artifact is not registered')
    if not registered_path.is_absolute():
        registered_path = db.parent / registered_path
    if registered_path.resolve() != artifact_path:
        raise ValueError('model path differs from registered artifact')
    requested_date = str(trade_date)[:10] if trade_date else None
    frame, metadata = _load_frame(path, requested_date)
    features = list(model_metadata.get("feature_columns") or [])
    if not features:
        raise RuntimeError("feature metadata has no feature_columns")
    missing = [column for column in features if column not in frame.columns]
    if missing:
        raise RuntimeError("feature file missing columns: " + ", ".join(missing[:10]))
    selected_date = requested_date or str(frame["datetime"].max())[:10]
    daily = frame[frame["datetime"] == selected_date].copy()
    if daily.empty:
        raise RuntimeError(f"no feature rows for trade_date={selected_date}")
    medians = model_metadata.get("feature_medians") or {}
    if not set(features) <= set(medians):
        raise ValueError('model is missing frozen training imputation values')
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
    with artifact_path.open("rb") as handle:
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
        rows=[{**row, "horizon": model_metadata.get("prediction_horizon", "t1_exec")} for row in rows],
        predict_horizon=str(model_metadata.get("prediction_horizon", "t1_exec")),
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
