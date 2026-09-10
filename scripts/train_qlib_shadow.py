from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import hashlib
import re

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ml.qlib_shadow import import_qlib_predictions
from trade_system.ml.shadow_evaluator import evaluate_qlib_shadow
from trade_system.ml.feature_artifacts import resolve_feature_path
from trade_system.v2.ml_protocol import holdout_diagnostics


def _date_literal(value: str | None) -> str | None:
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isdigit())
    if len(text) < 8:
        raise ValueError(f"invalid date: {value}")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


class QlibFrameDataset:
    """Small DatasetH-compatible adapter for the project's exported wide file."""

    def __init__(self, frame: pd.DataFrame, features: list[str], train_end: str, valid_start: str, valid_end: str,
                 test_start: str | None = None, test_end: str | None = None):
        self.frame = frame
        self.features = features
        self.segments = {"train": (train_end, train_end), "valid": (valid_start, valid_end)}
        if test_start is not None:
            if not train_end < valid_start <= valid_end < test_start <= test_end:
                raise ValueError('train/validation/test ranges must be strictly separated')
            self.segments['test'] = (test_start, test_end)

    def prepare(self, segment: str, col_set="feature", data_key=None):
        if segment == "train":
            if not {'label_end_time', 'label_available_time'} <= set(self.frame.columns):
                raise ValueError('training requires label end and availability times')
            data = self.frame[(self.frame["datetime"] <= self.segments["train"][0])
                              & (self.frame['label_end_time'] < self.segments['valid'][0])
                              & (self.frame['label_available_time'] < self.segments['valid'][0])]
        elif segment == "valid":
            data = self.frame[
                (self.frame["datetime"] >= self.segments["valid"][0])
                & (self.frame["datetime"] <= self.segments["valid"][1])
            ]
            if 'test' in self.segments:
                data = data[(data['label_end_time'] < self.segments['test'][0])
                            & (data['label_available_time'] < self.segments['test'][0])]
        elif segment == 'test' and 'test' in self.segments:
            data = self.frame[(self.frame['datetime'] >= self.segments['test'][0])
                              & (self.frame['datetime'] <= self.segments['test'][1])]
        elif segment == 'all':
            data = self.frame
        else:
            raise ValueError(f'unknown dataset segment: {segment}')
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
    path = resolve_feature_path(path)
    reserved = {'datetime', 'instrument', 'label_next_ret', 'label_date', 'label_end_time', 'label_available_time'}
    if not features or set(features) & reserved or len(features) != len(set(features)):
        raise ValueError('feature list must be unique and cannot include labels or identity')
    required = ["datetime", "instrument", "label_next_ret", 'label_end_time', 'label_available_time', *features]
    escaped = str(path / '*.parquet' if path.is_dir() else path).replace('\\', '/').replace("'", "''")
    relation = f"read_parquet('{escaped}')" if path.suffix == '.parquet' else f"read_csv('{escaped}', types={{'instrument':'VARCHAR'}})"
    columns_sql = ', '.join('"' + c.replace('"', '""') + '"' for c in required if c not in ('datetime', 'instrument'))
    from trade_system.db_utils import legacy_connect
    con = legacy_connect()
    try:
        con.execute(f"""CREATE TEMP VIEW qlib_features AS SELECT CAST(datetime AS DATE) AS datetime,
            CAST(instrument AS VARCHAR) AS instrument, {columns_sql} FROM {relation}
            WHERE isfinite(TRY_CAST(label_next_ret AS DOUBLE))""")
        duplicate = con.execute('SELECT 1 FROM qlib_features GROUP BY datetime,instrument HAVING count(*)>1 LIMIT 1').fetchone()
        if duplicate:
            raise ValueError('duplicate feature identity')
        if max_rows > 0:
            date_count = con.execute('SELECT count(DISTINCT datetime) FROM qlib_features').fetchone()[0]
            if date_count > max_rows:
                raise ValueError('max_rows must cover at least one sample per trading date')
            per_day = max(1, max_rows // max(1, date_count))
            frame = con.execute("""SELECT * EXCLUDE (_sample_rank) FROM (
                SELECT *, row_number() OVER (PARTITION BY datetime ORDER BY hash(datetime,instrument), instrument) AS _sample_rank
                FROM qlib_features) WHERE _sample_rank<=? ORDER BY datetime,instrument""", [per_day]).fetchdf()
        else:
            frame = con.execute('SELECT * FROM qlib_features ORDER BY datetime,instrument').fetchdf()
    finally:
        con.close()
    frame['datetime'] = pd.to_datetime(frame['datetime']).dt.strftime('%Y-%m-%d')
    for column in ('label_end_time', 'label_available_time'):
        frame[column] = pd.to_datetime(frame[column]).dt.strftime('%Y-%m-%d %H:%M:%S')
    frame['instrument'] = frame['instrument'].astype(str)
    for column in features + ['label_next_ret']:
        frame[column] = pd.to_numeric(frame[column], errors='coerce')
    return frame.sort_values(['datetime', 'instrument']).reset_index(drop=True)

def train_shadow(
    db_path: str | Path,
    feature_file: str | Path,
    *,
    model_id: str,
    train_end: str | None = None,
    valid_start: str | None = None,
    valid_end: str | None = None,
    test_start: str | None = None,
    test_end: str | None = None,
    max_rows: int = 300_000,
    num_boost_round: int = 120,
) -> dict[str, object]:
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}', model_id):
        raise ValueError('invalid model identifier')
    model_dir = Path('reports') / 'qlib_models'
    model_path = model_dir / f'{model_id}.pkl'
    if model_path.exists() or model_path.with_suffix('.metadata.json').exists():
        raise FileExistsError('model versions are immutable; choose a new model_id')
    feature_path = resolve_feature_path(feature_file)
    metadata_path = feature_path.with_suffix(".metadata.json")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8")) if metadata_path.exists() else {}
    label_mode = str(metadata.get("label_mode") or "legacy")
    prediction_horizon = "t1_exec" if label_mode == "t1_exec" else "t1"
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
    train_end_value = _date_literal(train_end) or dates[max(1, int(len(dates) * 0.6)) - 1]
    valid_start_value = _date_literal(valid_start) or dates[min(len(dates) - 1, dates.index(train_end_value) + 1)]
    valid_end_value = _date_literal(valid_end) or dates[max(1, int(len(dates) * 0.8)) - 1]
    test_start_value = _date_literal(test_start) or dates[min(len(dates)-1, dates.index(valid_end_value)+1)]
    test_end_value = _date_literal(test_end) or dates[-1]
    dataset = QlibFrameDataset(frame, features, train_end_value, valid_start_value, valid_end_value, test_start_value, test_end_value)
    train = dataset.prepare('train')
    valid = dataset.prepare('valid')
    test = dataset.prepare('test')
    if train.empty or valid.empty or test.empty:
        raise RuntimeError(f"empty train/valid split train_end={train_end_value} valid={valid_start_value}..{valid_end_value}")
    medians = train[features].replace([float('inf'), -float('inf')], float('nan')).median(numeric_only=True).fillna(0.0)
    frame[features] = frame[features].fillna(medians).replace([float("inf"), float("-inf")], 0.0).fillna(0.0)

    import qlib
    from qlib.contrib.model.gbdt import LGBModel

    # QLib's recorder is only used for shadow diagnostics.  Keep it local and
    # opt into MLflow's file backend explicitly; no remote experiment service
    # or trading signal side effect is needed here.
    os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")
    qlib.init(provider_uri=str(Path("reports") / "qlib_data").replace("\\", "/"), region="cn")
    dataset = QlibFrameDataset(frame, features, train_end_value, valid_start_value, valid_end_value, test_start_value, test_end_value)
    evals_result: dict = {}
    model = LGBModel(
        loss="mse",
        num_boost_round=num_boost_round,
        early_stopping_rounds=20,
        learning_rate=0.05,
        num_leaves=31,
        feature_fraction=0.8,
        verbosity=-1,
        num_threads=2,
    )
    model.fit(dataset, verbose_eval=40, evals_result=evals_result)
    predictions = model.predict(dataset, segment="test")
    holdout = holdout_diagnostics(predictions, dataset.prepare('test', col_set='label')['label_next_ret'],
                                  dataset.prepare('train', col_set='label')['label_next_ret'])
    pred_frame = pd.DataFrame({"score": predictions}).reset_index()
    pred_frame = pred_frame.rename(columns={"datetime": "trade_date", "instrument": "symbol"})
    pred_frame["rank"] = pred_frame.groupby("trade_date")["score"].rank(method="first", ascending=False).astype(int)
    pred_frame["horizon"] = prediction_horizon
    rows = pred_frame[["trade_date", "symbol", "score", "rank", "horizon"]].to_dict("records")
    model_dir.mkdir(parents=True, exist_ok=True)
    model.to_pickle(str(model_path))
    artifact_metadata = {
        "model_id": model_id,
        "feature_columns": features,
        "feature_medians": {key: float(value) for key, value in medians.to_dict().items()},
        "label_mode": label_mode,
        "prediction_horizon": prediction_horizon,
        "train_start": str(dates[0]),
        "train_end": str(train_end_value),
        "valid_start": str(valid_start_value),
        "valid_end": str(valid_end_value),
        'test_start': test_start_value,
        'test_end': test_end_value,
        'max_rows': max_rows,
        'num_boost_round': num_boost_round,
        'holdout_diagnostics': holdout,
        'model_sha256': hashlib.sha256(model_path.read_bytes()).hexdigest(),
        'dataset_metadata_sha256': hashlib.sha256(metadata_path.read_bytes()).hexdigest(),
        'evaluation_scope': 'independent_holdout_shadow_not_rolling_validation',
        "source_feature_file": str(feature_path),
    }
    model_path.with_suffix(".metadata.json").write_text(
        json.dumps(artifact_metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    imported = import_qlib_predictions(
        db_path,
        model_id=model_id,
        model_name="Project daily moneyflow + price shadow LGBM",
        factor_set="tushare_daily,daily_basic,moneyflow,derived_returns",
        rows=rows,
        train_start=str(dates[0]),
        train_end=str(train_end_value),
        predict_horizon=prediction_horizon,
        source_project="stock_data",
        model_file_ref=str(model_path),
        status="shadow",
        notes=(
            "QLib LGBModel; prediction impact remains disabled until manual outcome gate passes; "
            f"label_mode={label_mode}."
        ),
    )
    evaluation = evaluate_qlib_shadow(db_path)
    result = {
        "model_id": model_id,
        "train_rows": len(train),
        "valid_rows": len(valid),
        'test_rows': len(test),
        'holdout_diagnostics': holdout,
        "prediction_rows": len(rows),
        "train_start": str(dates[0]),
        "train_end": str(train_end_value),
        "valid_start": str(valid_start_value),
        "valid_end": str(valid_end_value),
        "label_mode": label_mode,
        "prediction_horizon": prediction_horizon,
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
    parser.add_argument('--test-start')
    parser.add_argument('--test-end')
    parser.add_argument("--max-rows", type=int, default=300_000)
    parser.add_argument("--num-boost-round", type=int, default=120)
    args = parser.parse_args()
    result = train_shadow(
        args.db, args.features, model_id=args.model_id, train_end=args.train_end,
        valid_start=args.valid_start, valid_end=args.valid_end,
        test_start=args.test_start, test_end=args.test_end,
        max_rows=args.max_rows, num_boost_round=args.num_boost_round,
    )
    print(f"model_id={result['model_id']} train_rows={result['train_rows']} valid_rows={result['valid_rows']}")
    print(f"predictions={result['prediction_rows']} model_file={result['model_file']}")
    print(f"shadow_samples={result['evaluation']['sample_count']} signal_impact=disabled")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
