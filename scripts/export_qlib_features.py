from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
import shutil
import sys
import uuid
import hashlib
import os

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trade_system.file_lock import FileLock


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
    include_adjustment: bool = False,
    label_mode: str = "t1_exec",
) -> str:
    if label_mode not in ('t1_exec', 'legacy'):
        raise ValueError('unsupported label mode')
    factor = 'a.adj_factor' if include_adjustment else '1.0'
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
        label_expr = "CASE WHEN d.close > 0 AND d.next_open > 0 AND d.next2_close IS NOT NULL THEN (d.next2_close / d.next_open - 1.0) * 100.0 END"
        label_date_expr = "d.next2_date"
    else:
        label_expr = "CASE WHEN d.next_close IS NOT NULL AND d.close > 0 THEN (d.next_close / d.close - 1.0) * 100.0 END"
        label_date_expr = "d.next_date"
    adjustment_cte = (
        """
    adjustments AS (
        SELECT CAST(date AS DATE) AS datetime,
               CAST(stock_code AS VARCHAR) AS instrument,
               max(CAST(adj_factor AS DOUBLE)) AS adj_factor
        FROM tushare_adj_factor
        WHERE adj_factor IS NOT NULL AND adj_factor > 0
        GROUP BY 1, 2
    ),
        """
        if include_adjustment
        else """
    adjustments AS (
        SELECT CAST(NULL AS DATE) AS datetime,
               CAST(NULL AS VARCHAR) AS instrument,
               CAST(NULL AS DOUBLE) AS adj_factor
        WHERE FALSE
    ),
        """
    )
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
    {adjustment_cte}
    bars AS (
        SELECT
            CAST(d.date AS DATE) AS datetime,
            CAST(d.stock_code AS VARCHAR) AS instrument,
            CAST(d.open AS DOUBLE) * {factor} AS open,
            CAST(d.high AS DOUBLE) * {factor} AS high,
            CAST(d.low AS DOUBLE) * {factor} AS low,
            CAST(d.close AS DOUBLE) * {factor} AS close,
            CAST(d.volume AS DOUBLE) / nullif({factor}, 0) AS volume,
            CAST(d.turnover AS DOUBLE) AS turnover,
            {factor} AS adj_factor
        FROM tushare_daily d
        LEFT JOIN adjustments a
          ON a.datetime = CAST(d.date AS DATE)
         AND a.instrument = CAST(d.stock_code AS VARCHAR)
        WHERE d.date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
          AND d.close IS NOT NULL AND d.close > 0
    ),
    daily_base AS (
        SELECT
            datetime, instrument, open, high, low, close, volume, turnover,
            LEAD(close) OVER (PARTITION BY instrument ORDER BY datetime) AS next_close,
            LEAD(datetime) OVER (PARTITION BY instrument ORDER BY datetime) AS next_date,
            LEAD(open) OVER (PARTITION BY instrument ORDER BY datetime) AS next_open,
            LEAD(close, 2) OVER (PARTITION BY instrument ORDER BY datetime) AS next2_close,
            LEAD(datetime, 2) OVER (PARTITION BY instrument ORDER BY datetime) AS next2_date,
            LAG(close, 1) OVER (PARTITION BY instrument ORDER BY datetime) AS prev_close,
            LAG(close, 5) OVER (PARTITION BY instrument ORDER BY datetime) AS prev5_close
        FROM bars
    ),
    daily AS (
        SELECT
            datetime, instrument, open, high, low, close, volume, turnover,
            next_close, next_date, next_open, next2_close, next2_date,
            prev_close, prev5_close,
            CASE WHEN prev_close > 0
                 THEN (close / prev_close - 1.0) * 100.0 END AS change_pct,
            STDDEV_SAMP(CASE WHEN prev_close > 0
                 THEN (close / prev_close - 1.0) * 100.0 END)
                 OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS volatility_5d,
            AVG(volume) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS avg_volume20,
            STDDEV_SAMP(volume) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS std_volume20
        FROM daily_base
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
        {label_date_expr} AS label_date,
        {label_date_expr} AS label_end_time,
        CAST({label_date_expr} AS TIMESTAMP) + INTERVAL '16 hours' AS label_available_time
    FROM daily d
    LEFT JOIN basic b ON b.datetime = d.datetime AND b.instrument = d.instrument
    LEFT JOIN money m ON m.datetime = d.datetime AND m.instrument = d.instrument
    {flow_join}
    ORDER BY d.datetime, d.instrument
    """


def _batch_ranges(start: str, end: str, *, max_date: str,
                  batch_days: int = 45, context_days: int = 25) -> list[tuple[str, str, str, str]]:
    """Yield bounded feature windows and their requested output windows.

    The feature query contains several window functions.  Running it once for
    a multi-year range creates a very large intermediate relation in DuckDB,
    even though the final QLib file is modest.  Each batch keeps enough prior
    sessions for the 20-day features and a short forward context for labels,
    then the outer query emits only the requested batch dates.
    """
    first = datetime.strptime(start, "%Y-%m-%d").date()
    last = datetime.strptime(end, "%Y-%m-%d").date()
    source_last = datetime.strptime(max_date, "%Y-%m-%d").date()
    if first > last:
        raise ValueError(f"start date is after end date: {start} > {end}")
    ranges = []
    cursor = first
    while cursor <= last:
        batch_end = min(cursor + timedelta(days=batch_days - 1), last)
        context_start = max(first, cursor - timedelta(days=context_days))
        context_end = min(source_last, batch_end + timedelta(days=7))
        ranges.append((
            context_start.isoformat(), context_end.isoformat(),
            cursor.isoformat(), batch_end.isoformat(),
        ))
        cursor = batch_end + timedelta(days=1)
    return ranges


def _remove_generated_target(path: Path) -> None:
    """Remove only the exact generated export target before a fresh export."""
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _copy_batched(
    con: duckdb.DuckDBPyConnection,
    *,
    start: str,
    end: str,
    max_date: str,
    target: Path,
    fmt: str,
    include_flow_features: bool,
    include_adjustment: bool,
    label_mode: str,
    materialized_table: str | None = None,
) -> dict[str, int | str]:
    """Export a format without materialising the full history in memory."""
    _remove_generated_target(target)
    part_dir = target if fmt == "parquet" else target.with_name(target.name + ".parts")
    _remove_generated_target(part_dir)
    part_dir.mkdir(parents=True, exist_ok=True)
    parts: list[Path] = []
    total_rows = 0
    labeled_rows = 0
    instrument_values: set[str] = set()
    ranges = _batch_ranges(start, end, max_date=max_date)

    for index, (context_start, context_end, output_start, output_end) in enumerate(ranges):
        query = f'SELECT * FROM {materialized_table}' if materialized_table else _query(
            context_start,
            context_end,
            include_flow_features=include_flow_features,
            include_adjustment=include_adjustment,
            label_mode=label_mode,
        )
        bounded_query = (
            f"SELECT * FROM ({query}) AS feature_batch "
            f"WHERE datetime BETWEEN DATE '{output_start}' AND DATE '{output_end}' "
            "ORDER BY datetime, instrument"
        )
        con.execute(f'CREATE OR REPLACE TEMP TABLE export_batch AS {bounded_query}')
        bounded_query = 'SELECT * FROM export_batch ORDER BY datetime,instrument'
        part = part_dir / f"part-{index:05d}.{fmt}"
        target_sql = str(part).replace("'", "''")
        if fmt == "parquet":
            con.execute(
                f"COPY ({bounded_query}) TO '{target_sql}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )
        else:
            con.execute(
                f"COPY ({bounded_query}) TO '{target_sql}' "
                "(HEADER, DELIMITER ',')"
            )
        summary = con.execute(
            f"SELECT count(*), count(label_next_ret) FROM ({bounded_query})"
        ).fetchone()
        total_rows += int(summary[0] or 0)
        labeled_rows += int(summary[1] or 0)
        instrument_values.update(
            str(row[0]) for row in con.execute(
                f"SELECT DISTINCT instrument FROM ({bounded_query})"
            ).fetchall()
        )
        parts.append(part)

    if fmt == "csv":
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("wb") as destination:
            for index, part in enumerate(parts):
                with part.open("rb") as source:
                    if index:
                        source.readline()
                    shutil.copyfileobj(source, destination)
        shutil.rmtree(part_dir)

    return {
        "rows": total_rows,
        "labeled_rows": labeled_rows,
        "instruments": len(instrument_values),
        "batches": len(parts),
        "storage": "partitioned_parquet_dataset" if fmt == "parquet" else "single_csv",
    }


def export_features(
    db_path: str | Path,
    output: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    output_format: str = "csv",
    label_mode: str = "t1_exec",
) -> dict[str, object]:
    out = Path(output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    guard = FileLock(out.with_suffix('.export.guard'))
    guard.__enter__()
    con = None
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        con.execute("SET memory_limit='1GB'")
        con.execute('SET threads=2')
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
        adjustment_available = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='tushare_adj_factor'"
        ).fetchone()[0] > 0
        feature_columns = BASE_FEATURE_COLUMNS + (FLOW_FEATURE_COLUMNS if flow_features_available else [])
        run_dir = out.parent / (out.stem + '.versions') / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        formats = {output_format} if output_format != "both" else {"csv", "parquet"}
        if not formats <= {'csv', 'parquet'}:
            raise ValueError('unsupported export format')
        # Compute the complete observation windows exactly once, from source
        # history (including pre-start warmup), then partition the materialized
        # result. Calendar-day padding is not a trading-observation window.
        query = _query(str(min_date)[:10], str(max_date)[:10],
                       include_flow_features=flow_features_available,
                       include_adjustment=adjustment_available, label_mode=label_mode)
        con.execute(f'CREATE TEMP TABLE all_features AS {query}')
        outputs: dict[str, str] = {}
        format_stats: dict[str, dict[str, int | str]] = {}
        for fmt in sorted(formats):
            target = run_dir / out.with_suffix(f'.{fmt}').name
            format_stats[fmt] = _copy_batched(
                con,
                start=start,
                end=end,
                max_date=str(max_date)[:10],
                target=target,
                fmt=fmt,
                include_flow_features=flow_features_available,
                include_adjustment=adjustment_available,
                label_mode=label_mode,
                materialized_table='all_features',
            )
            outputs[fmt] = str(target)
        primary_stats = format_stats["parquet" if "parquet" in format_stats else sorted(format_stats)[0]]
        rows = int(primary_stats["rows"])
        labeled = int(primary_stats["labeled_rows"])
        instruments = int(primary_stats["instruments"])
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
                "adjusted next open to adjusted T+2 close return in percent; T+1 compliant training target only"
                if label_mode == "t1_exec"
                else "adjusted current close to adjusted next available daily close return in percent; legacy research target only"
            ),
            "leakage_guard": "features use data through datetime; label_date/label_next_ret must not be used as model inputs",
            "label_mode": label_mode,
            "execution_assumption": "buy at next session open, sell at following session close" if label_mode == "t1_exec" else "not execution-aware",
            "source_tables": [
            "tushare_daily", "tushare_daily_basic", "tushare_moneyflow",
                *( ("tushare_adj_factor",) if adjustment_available else () ),
                *( ("qlib_stock_flow_features",) if flow_features_available else () ),
            ],
            "flow_features_available": flow_features_available,
            "adjustment_available": adjustment_available,
            "price_semantics": (
                "open/high/low/close and return labels use close*adj_factor; "
                "volume uses volume/adj_factor; missing factors remain NULL and affected labels are excluded"
                if adjustment_available else
                "raw daily prices; tushare_adj_factor is unavailable"
            ),
            "export_batches": int(primary_stats["batches"]),
            "export_storage": primary_stats["storage"],
            "export_memory_guard": "single materialization with full observation warmup; 1GB DuckDB limit; bounded output batches",
            "scope": "historical_research_only",
            "availability_assumption": "label available at 16:00 Asia/Shanghai on label end date; no actual historical receipt evidence",
            "generated_at": date.today().isoformat(),
        }
        metadata['artifact_hashes'] = {}
        for artifact in run_dir.rglob('*'):
            if artifact.is_file():
                digest = hashlib.sha256()
                with artifact.open('rb') as handle:
                    for block in iter(lambda: handle.read(8*1024*1024), b''):
                        digest.update(block)
                metadata['artifact_hashes'][artifact.relative_to(run_dir).as_posix()] = digest.hexdigest()
        meta_path = run_dir / out.with_suffix('.metadata.json').name
        with meta_path.open('x', encoding='utf-8') as handle:
            json.dump(metadata, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        pointer = {'outputs': {fmt: Path(path).relative_to(out.parent).as_posix() for fmt, path in outputs.items()},
                   'metadata_sha256': hashlib.sha256(meta_path.read_bytes()).hexdigest()}
        temp = out.with_suffix('.current.' + uuid.uuid4().hex + '.tmp')
        with temp.open('x', encoding='utf-8') as handle:
            json.dump(pointer, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temp.replace(out.with_suffix('.current.json'))
        return metadata
    finally:
        if con is not None:
            con.close()
        guard.__exit__(None, None, None)


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Export leakage-safe daily features in a QLib-compatible wide format.")
    parser.add_argument("--db", default=str(project_root / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(project_root / "reports" / "qlib_features_daily.csv"))
    parser.add_argument("--start-date")
    parser.add_argument("--end-date")
    parser.add_argument("--format", choices=["csv", "parquet", "both"], default="csv")
    parser.add_argument("--label-mode", choices=["legacy", "t1_exec"], default="t1_exec")
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
