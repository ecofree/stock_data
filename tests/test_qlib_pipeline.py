from __future__ import annotations

import json

import duckdb

from scripts.export_qlib_features import export_features
from scripts.train_qlib_shadow import QlibFrameDataset


def test_export_qlib_features_has_target_only_label(tmp_path):
    db = tmp_path / "qlib.duckdb"
    con = duckdb.connect(str(db))
    con.execute(
        "CREATE TABLE tushare_daily (date DATE, stock_code VARCHAR, open DOUBLE, high DOUBLE, low DOUBLE, "
        "close DOUBLE, volume DOUBLE, turnover DOUBLE, change_pct DOUBLE)"
    )
    con.execute(
        "CREATE TABLE tushare_daily_basic (date DATE, stock_code VARCHAR, turnover_rate DOUBLE, volume_ratio DOUBLE, "
        "pe DOUBLE, pb DOUBLE, total_mv DOUBLE, circ_mv DOUBLE)"
    )
    con.execute(
        "CREATE TABLE tushare_moneyflow (date DATE, stock_code VARCHAR, buy_lg_amount DOUBLE, sell_lg_amount DOUBLE, "
        "buy_elg_amount DOUBLE, sell_elg_amount DOUBLE, net_mf_amount DOUBLE)"
    )
    for day, close in [("2026-01-02", 10), ("2026-01-05", 11), ("2026-01-06", 10.5)]:
        con.execute("INSERT INTO tushare_daily VALUES (?, '000001', 10, 11, 9, ?, 100, 1000, 1)", [day, close])
        con.execute("INSERT INTO tushare_daily_basic VALUES (?, '000001', 1, 1, 10, 1, 100, 80)", [day])
        con.execute("INSERT INTO tushare_moneyflow VALUES (?, '000001', 10, 5, 20, 10, 15)", [day])
    con.close()

    out = tmp_path / "features.csv"
    result = export_features(db, out, start_date="2026-01-02", end_date="2026-01-06")
    assert result["rows"] == 3
    assert result["labeled_rows"] == 2
    metadata = json.loads(out.with_suffix(".metadata.json").read_text(encoding="utf-8"))
    assert metadata["label_column"] == "label_next_ret"
    assert "label_next_ret" not in metadata["feature_columns"]


def test_qlib_frame_dataset_returns_multiindex_feature_label():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "datetime": ["2026-01-02", "2026-01-05"],
            "instrument": ["000001", "000001"],
            "f": [1.0, 2.0],
            "label_next_ret": [1.0, -1.0],
        }
    )
    dataset = QlibFrameDataset(frame, ["f"], "2026-01-02", "2026-01-05", "2026-01-05")
    prepared = dataset.prepare("train", col_set=["feature", "label"])
    assert list(prepared.columns) == [("feature", "f"), ("label", "label_next_ret")]

