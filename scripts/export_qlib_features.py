from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BASE_FEATURE_COLUMNS = [
    "open", "high", "low", "close", "volume", "turnover", "change_pct",
    "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv",
    "net_mf_amount", "large_net_mf", "extra_large_net_mf",
    "ret_1d", "ret_5d", "volatility_5d", "moneyflow_5d", "volume_z20",
]

FLOW_FEATURE_COLUMNS = [
    "flow_main_net_1d", "flow_main_net_3d", "flow_main_net_5d",
    "flow_main_net_10d", "flow_main_net_20d", "flow_positive_days_3d",
    "flow_positive_days_5d", "flow_positive_days_10d", "flow_positive_days_20d",
    "flow_observed_days_20d", "flow_acceleration_5d", "flow_main_net_ratio_1d",
]

# Kept as a public compatibility alias for callers that imported the old name.
FEATURE_COLUMNS = BASE_FEATURE_COLUMNS


def _date_literal(value: str | None, fallback: str) -> str:
    text = "".join(ch for ch in str(value or fallback) if ch.isdigit())
    if len(text) >= 8:
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    raise ValueError(f"invalid date: {value or fallback}")


def _query(
    start_date: str,
    end_date: str,
    *,
    include_flow_features: bool = False,
    label_mode: str = "legacy",
) -> str:
    flow_select = ""
    flow_join = ""
    if include_flow_features:
        flow_select = """,
        sf.main_net_1d AS flow_main_net_1d,
        sf.main_net_3d AS flow_main_net_3d,
        sf.main_net_5d AS flow_main_net_5d,
        sf.main_net_10d AS flow_main_net_10d,
        sf.main_net_20d AS flow_main_net_20d,
        sf.positive_days_3d AS flow_positive_days_3d,
        sf.positive_days_5d AS flow_positive_days_5d,
        sf.positive_days_10d AS flow_positive_days_10d,
        sf.positive_days_20d AS flow_positive_days_20d,
        sf.observed_days_20d AS flow_observed_days_20d,
        sf.flow_acceleration_5d AS flow_acceleration_5d,
        sf.main_net_ratio_1d AS flow_main_net_ratio_1d"""
        flow_join = """
    LEFT JOIN qlib_stock_flow_features sf
      ON sf.trade_date = d.datetime AND sf.stock_code = d.instrument
    """
    if label_mode == "t1_exec":
        label_expr = "CASE WHEN d.next_open > 0 AND d.next2_close IS NOT NULL THEN (d.next2_close / d.next_open - 1.0) * 100.0 END"
        label_date_expr = "d.next2_date"
    else:
        label_expr = "CASE WHEN d.next_close IS NOT NULL AND d.close > 0 THEN (d.next_close / d.close - 1.0) * 100.0 END"
        label_date_expr = "d.next_date"
    return f"""
    WITH coverage AS (
        SELECT CAST(date AS DATE) AS datetime, count(DISTINCT stock_code) AS instruments
        FROM tushare_daily
        GROUP BY date
    ),
    valid_dates AS (
        SELECT datetime FROM coverage
        WHERE instruments >= CASE
            WHEN (SELECT coalesce(max(instruments), 0) FROM coverage) >= 1000 THEN 1000
            ELSE 1
        END
    ),
    daily AS (
        SELECT
            CAST(date AS DATE) AS datetime,
            CAST(stock_code AS VARCHAR) AS instrument,
            CAST(open AS DOUBLE) AS open,
            CAST(high AS DOUBLE) AS high,
            CAST(low AS DOUBLE) AS low,
            CAST(close AS DOUBLE) AS close,
            CAST(volume AS DOUBLE) AS volume,
            CAST(turnover AS DOUBLE) AS turnover,
            CAST(change_pct AS DOUBLE) AS change_pct,
            LEAD(CAST(close AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date) AS next_close,
            LEAD(CAST(date AS DATE)) OVER (PARTITION BY stock_code ORDER BY date) AS next_date,
            LEAD(CAST(open AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date) AS next_open,
            LEAD(CAST(close AS DOUBLE), 2) OVER (PARTITION BY stock_code ORDER BY date) AS next2_close,
            LEAD(CAST(date AS DATE), 2) OVER (PARTITION BY stock_code ORDER BY date) AS next2_date,
            LAG(CAST(close AS DOUBLE), 1) OVER (PARTITION BY stock_code ORDER BY date) AS prev_close,
            LAG(CAST(close AS DOUBLE), 5) OVER (PARTITION BY stock_code ORDER BY date) AS prev5_close,
            AVG(CAST(change_pct AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS avg_ret5,
            STDDEV_SAMP(CAST(change_pct AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS volatility_5d,
            AVG(CAST(volume AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume20,
            STDDEV_SAMP(CAST(volume AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS std_volume20
        FROM tushare_daily
        WHERE date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
          AND close IS NOT NULL AND close > 0
          AND CAST(date AS DATE) IN (SELECT datetime FROM valid_dates)
    ),
    basic AS (
        SELECT
            CAST(date AS DATE) AS datetime,
            CAST(stock_code AS VARCHAR) AS instrument,
            CAST(turnover_rate AS DOUBLE) AS turnover_rate,
            CAST(volume_ratio AS DOUBLE) AS volume_ratio,
            CAST(pe AS DOUBLE) AS pe,
            CAST(pb AS DOUBLE) AS pb,
            CAST(total_mv AS DOUBLE) AS total_mv,
            CAST(circ_mv AS DOUBLE) AS circ_mv
        FROM tushare_daily_basic
        WHERE date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
    ),
    money AS (
        SELECT
            CAST(date AS DATE) AS datetime,
            CAST(stock_code AS VARCHAR) AS instrument,
            CAST(net_mf_amount AS DOUBLE) AS net_mf_amount,
            CAST((buy_lg_amount - sell_lg_amount) AS DOUBLE) AS large_net_mf,
            CAST((buy_elg_amount - sell_elg_amount) AS DOUBLE) AS extra_large_net_mf,
            SUM(CAST(net_mf_amount AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS moneyflow_5d
        FROM tushare_moneyflow
        WHERE date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
    )
    SELECT
        d.datetime,
        d.instrument,
        {", ".join(f"d.{c}" for c in BASE_FEATURE_COLUMNS[:7])},
        b.turnover_rate, b.volume_ratio, b.pe, b.pb, b.total_mv, b.circ_mv,
        m.net_mf_amount, m.large_net_mf, m.extra_large_net_mf,
        CASE WHEN d.prev_close > 0 THEN (d.close / d.prev_close - 1.0) * 100.0 END AS ret_1d,
        CASE WHEN d.prev5_close > 0 THEN (d.close / d.prev5_close - 1.0) * 100.0 END AS ret_5d,
        d.volatility_5d,
        m.moneyflow_5d,
        CASE WHEN d.std_volume20 > 0 THEN (d.volume - d.avg_volume20) / d.std_volume20 END AS volume_z20
        {flow_select},
        {label_expr} AS label_next_ret,
        {label_date_expr} AS label_date
    FROM daily d
    LEFT JOIN basic b ON b.datetime = d.datetime AND b.instrument = d.instrument
    LEFT JOIN money m ON m.datetime = d.datetime AND m.instrument = d.instrument
    {flow_join}
    ORDER BY d.datetime, d.instrument
    """


def export_features(
    db_path: str | Path,
    output: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    output_format: str = "csv",
    label_mode: str = "legacy",
) -> dict[str, object]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        min_date, max_date = con.execute(
            "SELECT min(date), max(date) FROM tushare_daily WHERE close IS NOT NULL AND close > 0"
        ).fetchone()
        if min_date is None:
            raise RuntimeError("tushare_daily is empty; sync daily data before exporting QLib features")
        start = _date_literal(start_date, str(min_date)[:10])
        end = _date_literal(end_date, str(max_date)[:10])
        flow_features_available = con.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name='qlib_stock_flow_features'"
        ).fetchone()[0] > 0
        query = _query(
            start,
            end,
            include_flow_features=flow_features_available,
            label_mode=label_mode,
        )
        feature_columns = BASE_FEATURE_COLUMNS + (FLOW_FEATURE_COLUMNS if flow_features_available else [])
        out = Path(output)
        out.parent.mkdir(parents=True, exist_ok=True)
        formats = {output_format} if output_format != "both" else {"csv", "parquet"}
        outputs: dict[str, str] = {}
        for fmt in sorted(formats):
            target = out if out.suffix.lower() == f".{fmt}" else out.with_suffix(f".{fmt}")
            target_sql = str(target).replace("'", "''")
            if fmt == "parquet":
                con.execute(f"COPY ({query}) TO '{target_sql}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            else:
                con.execute(f"COPY ({query}) TO '{target_sql}' (HEADER, DELIMITER ',')")
            outputs[fmt] = str(target)
        rows = int(con.execute(f"SELECT count(*) FROM ({query})").fetchone()[0])
        labeled = int(con.execute(f"SELECT count(*) FROM ({query}) WHERE label_next_ret IS NOT NULL").fetchone()[0])
        instruments = int(con.execute(f"SELECT count(DISTINCT instrument) FROM ({query})").fetchone()[0])
        metadata = {
            "format": output_format,
            "outputs": outputs,
            "rows": rows,
            "labeled_rows": labeled,
            "instruments": instruments,
            "start_date": start,
            "end_date": end,
            "feature_columns": feature_columns,
            "label_column": "label_next_ret",
            "label_definition": (
                "next open to T+2 close return in percent; T+1 compliant training target only"
                if label_mode == "t1_exec"
                else "current close to next available daily close return in percent; legacy research target only"
            ),
            "leakage_guard": "features use data through datetime; label_date/label_next_ret must not be used as model inputs",
            "label_mode": label_mode,
            "execution_assumption": "buy at next session open, sell at following session close" if label_mode == "t1_exec" else "not execution-aware",
            "source_tables": [
                "tushare_daily", "tushare_daily_basic", "tushare_moneyflow",
                *( ("qlib_stock_flow_features",) if flow_features_available else () ),
            ],
            "flow_features_available": flow_features_available,
            "generated_at": date.today().isoformat(),
        }
        meta_path = out.with_suffix(".metadata.json")
        meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        return metadata
    finally:
        con.close()


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Export leakage-safe daily features in a QLib-compatible wide format.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "qlib_features_daily.csv"))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--format", choices=["csv", "parquet", "both"], default="csv")
    parser.add_argument("--label-mode", choices=["legacy", "t1_exec"], default="legacy")
    args = parser.parse_args()
    result = export_features(
        args.db,
        args.out,
        start_date=args.start_date,
        end_date=args.end_date,
        output_format=args.format,
        label_mode=args.label_mode,
    )
    print(f"rows={result['rows']} labeled_rows={result['labeled_rows']} instruments={result['instruments']}")
    print(f"date_range={result['start_date']}..{result['end_date']}")
    print(f"outputs={result['outputs']}")
    print("leakage_guard=label_next_ret_and_label_date_are_targets_only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
