from __future__ import annotations

import argparse
import csv
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from trade_system.ml.qlib_shadow import import_qlib_predictions


def load_prediction_csv(path: str | Path) -> list[dict]:
    with Path(path).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Import external qlib predictions in shadow mode.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--file", required=True)
    parser.add_argument("--model-id", required=True)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--factor-set", default="")
    parser.add_argument("--horizon", default="t1")
    args = parser.parse_args()

    rows = load_prediction_csv(args.file)
    result = import_qlib_predictions(
        args.db,
        model_id=args.model_id,
        model_name=args.model_name or args.model_id,
        factor_set=args.factor_set,
        predict_horizon=args.horizon,
        rows=rows,
    )
    print(f"qlib_model_registry={result['qlib_model_registry']}")
    print(f"qlib_prediction={result['qlib_prediction']}")
    print("shadow_mode=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
