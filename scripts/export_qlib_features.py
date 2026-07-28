from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


FEATURE_COLUMNS = [
    "open", "high", "low", "close", "volume", "turnover", "change_pct",
    "turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv",
    "net_mf_amount", "large_net_mf", "extra_large_net_mf",
    "ret_1d", "ret_5d", "volatility_5d", "moneyflow_5d", "volume_z20",
]


def _date_literal(value: str | None, fallback: str) -> str:
    text = str(value or fallback).replace("'", "")
    return text[:10]


def _query(start_date: str, end_date: str) -> str:
    return f"""
    WITH daily AS (
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
            LAG(CAST(close AS DOUBLE), 1) OVER (PARTITION BY stock_code ORDER BY date) AS prev_close,
            LAG(CAST(close AS DOUBLE), 5) OVER (PARTITION BY stock_code ORDER BY date) AS prev5_close,
            AVG(CAST(change_pct AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS avg_ret5,
            STDDEV_SAMP(CAST(change_pct AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS volatility_5d,
            AVG(CAST(volume AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume20,
            STDDEV_SAMP(CAST(volume AS DOUBLE)) OVER (PARTITION BY stock_code ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS std_volume20
        FROM tushare_daily
        WHERE date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
          AND close IS NOT NULL AND close > 0
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
        {", ".join(f"d.{c}" for c in FEATURE_COLUMNS[:7])},
        b.turnover_rate, b.volume_ratio, b.pe, b.pb, b.total_mv, b.circ_mv,
        m.net_mf_amount, m.large_net_mf, m.extra_large_net_mf,
        CASE WHEN d.prev_close > 0 THEN (d.close / d.prev_close - 1.0) * 100.0 END AS ret_1d,
        CASE WHEN d.prev5_close > 0 THEN (d.close / d.prev5_close - 1.0) * 100.0 END AS ret_5d,
        d.volatility_5d,
        m.moneyflow_5d,
        CASE WHEN d.std_volume20 > 0 THEN (d.volume - d.avg_volume20) / d.std_volume20 END AS volume_z20,
        CASE WHEN d.next_close IS NOT NULL AND d.close > 0 THEN (d.next_close / d.close - 1.0) * 100.0 END AS label_next_ret,
        d.next_date AS label_date
    FROM daily d
    LEFT JOIN basic b ON b.datetime = d.datetime AND b.instrument = d.instrument
    LEFT JOIN money m ON m.datetime = d.datetime AND m.instrument = d.instrument
    ORDER BY d.datetime, d.instrument
    """


def export_features(
    db_path: str | Path,
    output: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    output_format: str = "csv",
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
        query = _query(start, end)
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
            "feature_columns": FEATURE_COLUMNS,
            "label_column": "label_next_ret",
            "label_definition": "next available daily close return in percent; training target only",
            "leakage_guard": "features use data through datetime; label_date/label_next_ret must not be used as model inputs",
            "source_tables": ["tushare_daily", "tushare_daily_basic", "tushare_moneyflow"],
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
    args = parser.parse_args()
    result = export_features(
        args.db, args.out, start_date=args.start_date, end_date=args.end_date, output_format=args.format
    )
    print(f"rows={result['rows']} labeled_rows={result['labeled_rows']} instruments={result['instruments']}")
    print(f"date_range={result['start_date']}..{result['end_date']}")
    print(f"outputs={result['outputs']}")
    print("leakage_guard=label_next_ret_and_label_date_are_targets_only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
