from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ml.qlib_shadow import import_qlib_predictions
from trade_system.ml.shadow_evaluator import evaluate_qlib_shadow


class QlibFrameDataset:
    """Small DatasetH-compatible adapter for the project's exported wide file."""

    def __init__(self, frame: pd.DataFrame, features: list[str], train_end: str, valid_start: str, valid_end: str):
        self.frame = frame
        self.features = features
        self.segments = {"train": (train_end, train_end), "valid": (valid_start, valid_end)}

    def prepare(self, segment: str, col_set="feature", data_key=None):
        if segment == "train":
            data = self.frame[self.frame["datetime"] <= self.segments["train"][0]]
        elif segment == "valid":
            data = self.frame[
                (self.frame["datetime"] >= self.segments["valid"][0])
                & (self.frame["datetime"] <= self.segments["valid"][1])
            ]
        else:
            data = self.frame
        data = data.set_index(["datetime", "instrument"]).sort_index()
        if col_set == "feature":
            return data[self.features]
        if col_set == "label":
            return data[["label_next_ret"]]
        result = pd.concat(
            [
                data[self.features].rename(columns={c: ("feature", c) for c in self.features}),
                data[["label_next_ret"]].rename(columns={"label_next_ret": ("label", "label_next_ret")}),
            ],
            axis=1,
        )
        result.columns = pd.MultiIndex.from_tuples(result.columns)
        return result


def _load_features(path: Path, features: list[str], max_rows: int) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        frame = pd.read_parquet(path)
    else:
        frame = pd.read_csv(path, parse_dates=["datetime"])
    frame["datetime"] = pd.to_datetime(frame["datetime"]).dt.strftime("%Y-%m-%d")
    required = ["datetime", "instrument", "label_next_ret", *features]
    frame = frame[[c for c in required if c in frame.columns]].copy()
    frame = frame.dropna(subset=["label_next_ret"])
    frame["instrument"] = frame["instrument"].astype(str)
    for column in features + ["label_next_ret"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["label_next_ret"])
    if max_rows > 0 and len(frame) > max_rows:
        per_day = max(1, max_rows // max(1, frame["datetime"].nunique()))
        frame = frame.sort_values(["datetime", "instrument"]).groupby("datetime", group_keys=False).head(per_day)
    return frame.sort_values(["datetime", "instrument"]).reset_index(drop=True)


def train_shadow(
    db_path: str | Path,
    feature_file: str | Path,
    *,
    model_id: str,
    train_end: str | None = None,
    valid_start: str | None = None,
    valid_end: str | None = None,
    max_rows: int = 300_000,
    num_boost_round: int = 120,
) -> dict[str, object]:
    feature_path = Path(feature_file)
    metadata_path = feature_path.with_suffix(".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    features = list(metadata.get("feature_columns") or [
        "open", "high", "low", "close", "volume", "turnover", "change_pct",
        "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv",
        "net_mf_amount", "large_net_mf", "extra_large_net_mf",
        "ret_1d", "ret_5d", "volatility_5d", "moneyflow_5d", "volume_z20",
    ])
    frame = _load_features(feature_path, features, max_rows)
    dates = sorted(frame["datetime"].unique())
    if len(dates) < 20:
        raise RuntimeError(f"need at least 20 trading dates for shadow training, got {len(dates)}")
    train_end_value = train_end or dates[max(1, int(len(dates) * 0.8)) - 1]
    valid_start_value = valid_start or dates[min(len(dates) - 1, dates.index(train_end_value) + 1)]
    valid_end_value = valid_end or dates[-1]
    train = frame[frame["datetime"] <= train_end_value].copy()
    valid = frame[(frame["datetime"] >= valid_start_value) & (frame["datetime"] <= valid_end_value)].copy()
    if train.empty or valid.empty:
        raise RuntimeError(f"empty train/valid split train_end={train_end_value} valid={valid_start_value}..{valid_end_value}")
    medians = train[features].median(numeric_only=True).fillna(0.0)
    frame[features] = frame[features].fillna(medians).replace([float("inf"), float("-inf")], 0.0).fillna(0.0)

    import qlib
    from qlib.contrib.model.gbdt import LGBModel

    # QLib's recorder is only used for shadow diagnostics.  Keep it local and
    # opt into MLflow's file backend explicitly; no remote experiment service
    # or trading signal side effect is needed here.
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    qlib.init(provider_uri=str(Path("reports") / "qlib_data").replace("\\", "/"), region="cn")
    dataset = QlibFrameDataset(frame, features, train_end_value, valid_start_value, valid_end_value)
    evals_result: dict = {}
    model = LGBModel(
        loss="mse",
        num_boost_round=num_boost_round,
        early_stopping_rounds=20,
        learning_rate=0.05,
        num_leaves=31,
        feature_fraction=0.8,
        verbosity=-1,
    )
    model.fit(dataset, verbose_eval=40, evals_result=evals_result)
    predictions = model.predict(dataset, segment="valid")
    valid_index = valid.set_index(["datetime", "instrument"]).index
    pred_frame = pd.DataFrame({"score": predictions}).reset_index()
    pred_frame = pred_frame.rename(columns={"datetime": "trade_date", "instrument": "symbol"})
    pred_frame["rank"] = pred_frame.groupby("trade_date")["score"].rank(method="first", ascending=False).astype(int)
    pred_frame["horizon"] = "t1"
    rows = pred_frame[["trade_date", "symbol", "score", "rank", "horizon"]].to_dict("records")
    model_dir = Path("reports") / "qlib_models"
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"{model_id}.pkl"
    model.to_pickle(str(model_path))
    imported = import_qlib_predictions(
        db_path,
        model_id=model_id,
        model_name="Project daily moneyflow + price shadow LGBM",
        factor_set="tushare_daily,daily_basic,moneyflow,derived_returns",
        rows=rows,
        train_start=str(dates[0]),
        train_end=str(train_end_value),
        predict_horizon="t1",
        source_project="stock_data",
        model_file_ref=str(model_path),
        status="shadow",
        notes="QLib LGBModel; prediction impact remains disabled until manual outcome gate passes.",
    )
    evaluation = evaluate_qlib_shadow(db_path)
    result = {
        "model_id": model_id,
        "train_rows": len(train),
        "valid_rows": len(valid),
        "prediction_rows": len(rows),
        "train_start": str(dates[0]),
        "train_end": str(train_end_value),
        "valid_start": str(valid_start_value),
        "valid_end": str(valid_end_value),
        "features": features,
        "model_file": str(model_path),
        "imported": imported,
        "evaluation": evaluation,
        "signal_impact": "disabled",
    }
    report = Path("reports") / "qlib_shadow_training_latest.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return result


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Train a QLib LGBModel in shadow mode and import predictions.")
    parser.add_argument("--db", default=str(root / "kpl_data.duckdb"))
    parser.add_argument("--features", default=str(root / "reports" / "qlib_features_2026.parquet"))
    parser.add_argument("--model-id", default="qlib_shadow_lgbm_2026_ytd")
    parser.add_argument("--train-end")
    parser.add_argument("--valid-start")
    parser.add_argument("--valid-end")
    parser.add_argument("--max-rows", type=int, default=300_000)
    parser.add_argument("--num-boost-round", type=int, default=120)
    args = parser.parse_args()
    result = train_shadow(
        args.db, args.features, model_id=args.model_id, train_end=args.train_end,
        valid_start=args.valid_start, valid_end=args.valid_end,
        max_rows=args.max_rows, num_boost_round=args.num_boost_round,
    )
    print(f"model_id={result['model_id']} train_rows={result['train_rows']} valid_rows={result['valid_rows']}")
    print(f"predictions={result['prediction_rows']} model_file={result['model_file']}")
    print(f"shadow_samples={result['evaluation']['sample_count']} signal_impact=disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
