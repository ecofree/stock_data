from __future__ import annotations

import json

import duckdb

from scripts.export_qlib_features import export_features
from scripts.train_qlib_shadow import QlibFrameDataset, _load_features
from trade_system.ml.feature_artifacts import resolve_feature_path


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
    result = export_features(db, out, start_date="2026-01-02", end_date="2026-01-06", label_mode="legacy")
    assert result["rows"] == 3
    assert result["labeled_rows"] == 2
    metadata = json.loads(resolve_feature_path(out).with_suffix(".metadata.json").read_text(encoding="utf-8"))
    assert metadata["label_column"] == "label_next_ret"
    assert "label_next_ret" not in metadata["feature_columns"]

    # 默认即 T+1 合规口径，不再是 legacy。
    exec_out = tmp_path / "features_exec.csv"
    exec_result = export_features(
        db, exec_out, start_date="2026-01-02", end_date="2026-01-06"
    )
    assert exec_result["labeled_rows"] == 1
    exec_metadata = json.loads(resolve_feature_path(exec_out).with_suffix(".metadata.json").read_text(encoding="utf-8"))
    assert exec_metadata["label_mode"] == "t1_exec"
    assert "T+1 compliant" in exec_metadata["label_definition"]


def test_export_qlib_features_includes_canonical_flow_windows_when_available(tmp_path):
    db = tmp_path / "qlib_flow.duckdb"
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
    con.execute(
        "CREATE TABLE qlib_stock_flow_features (trade_date DATE, stock_code VARCHAR, main_net_1d DOUBLE, "
        "main_net_3d DOUBLE, main_net_5d DOUBLE, main_net_10d DOUBLE, main_net_20d DOUBLE, "
        "positive_days_3d INTEGER, positive_days_5d INTEGER, positive_days_10d INTEGER, "
        "positive_days_20d INTEGER, observed_days_20d INTEGER, flow_acceleration_5d DOUBLE, "
        "main_net_ratio_1d DOUBLE)"
    )
    for day, close in [("2026-01-02", 10), ("2026-01-05", 11), ("2026-01-06", 10.5)]:
        con.execute("INSERT INTO tushare_daily VALUES (?, '000001', 10, 11, 9, ?, 100, 1000, 1)", [day, close])
        con.execute("INSERT INTO tushare_daily_basic VALUES (?, '000001', 1, 1, 10, 1, 100, 80)", [day])
        con.execute("INSERT INTO tushare_moneyflow VALUES (?, '000001', 10, 5, 20, 10, 15)", [day])
        con.execute(
            "INSERT INTO qlib_stock_flow_features VALUES (?, '000001', 1, 2, 3, 4, 5, 1, 2, 3, 4, 5, 0.5, 0.001)",
            [day],
        )
    con.close()

    out = tmp_path / "features_with_flow.csv"
    result = export_features(db, out, start_date="2026-01-02", end_date="2026-01-06")
    assert result["flow_features_available"] is True
    assert "flow_main_net_20d" in result["feature_columns"]
    import pandas as pd

    frame = pd.read_csv(resolve_feature_path(out))
    assert frame.loc[0, "flow_main_net_1d"] == 1


def test_export_qlib_features_applies_adjustment_factor_to_prices_and_labels(tmp_path):
    db = tmp_path / "qlib_adjustment.duckdb"
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
    con.execute(
        "CREATE TABLE tushare_adj_factor (date DATE, stock_code VARCHAR, adj_factor DOUBLE)"
    )
    for day, close, factor in [
        ("2026-01-02", 10, 1), ("2026-01-05", 5, 2), ("2026-01-06", 6, 2),
    ]:
        con.execute("INSERT INTO tushare_daily VALUES (?, '000001', 10, 11, 9, ?, 100, 1000, 1)", [day, close])
        con.execute("INSERT INTO tushare_daily_basic VALUES (?, '000001', 1, 1, 10, 1, 100, 80)", [day])
        con.execute("INSERT INTO tushare_moneyflow VALUES (?, '000001', 10, 5, 20, 10, 15)", [day])
        con.execute("INSERT INTO tushare_adj_factor VALUES (?, '000001', ?)", [day, factor])
    con.close()

    out = tmp_path / "features_adjusted.csv"
    result = export_features(db, out, start_date="2026-01-02", end_date="2026-01-06", label_mode="legacy")
    assert result["adjustment_available"] is True
    assert "tushare_adj_factor" in result["source_tables"]
    import pandas as pd

    frame = pd.read_csv(resolve_feature_path(out))
    assert frame.loc[1, "close"] == 10
    assert frame.loc[1, "volume"] == 50
    assert frame.loc[0, "label_next_ret"] == 0


def test_qlib_frame_dataset_returns_multiindex_feature_label():
    import pandas as pd

    frame = pd.DataFrame(
        {
            "datetime": ["2026-01-02", "2026-01-05"],
            "instrument": ["000001", "000001"],
            "f": [1.0, 2.0],
            "label_next_ret": [1.0, -1.0],
            'label_end_time': ['2026-01-03', '2026-01-06'],
            'label_available_time': ['2026-01-03', '2026-01-06'],
        }
    )
    dataset = QlibFrameDataset(frame, ["f"], "2026-01-02", "2026-01-05", "2026-01-05")
    prepared = dataset.prepare("train", col_set=["feature", "label"])
    assert list(prepared.columns) == [("feature", "f"), ("label", "label_next_ret")]


def test_partitioned_parquet_loader_samples_before_pandas_materialization(tmp_path):
    db = tmp_path / "qlib_partitioned.duckdb"
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
    for day in ["2026-01-02", "2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09"]:
        for code in ["000001", "000002"]:
            con.execute(
                "INSERT INTO tushare_daily VALUES (?, ?, 10, 11, 9, 10, 100, 1000, 1)",
                [day, code],
            )
            con.execute(
                "INSERT INTO tushare_daily_basic VALUES (?, ?, 1, 1, 10, 1, 100, 80)",
                [day, code],
            )
            con.execute(
                "INSERT INTO tushare_moneyflow VALUES (?, ?, 10, 5, 20, 10, 15)",
                [day, code],
            )
    con.close()

    out = tmp_path / "features.parquet"
    metadata = export_features(
        db, out, start_date="2026-01-02", end_date="2026-01-07", output_format="parquet"
    )
    frame = _load_features(out, metadata["feature_columns"], max_rows=4)
    assert len(frame) <= 4
    assert frame["datetime"].nunique() == 4
    assert frame["label_next_ret"].notna().all()
