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
    include_calendar: bool = False,
    calendar_relation: str = "tushare_trade_cal",
    adjustment_relation: str = "tushare_adj_factor",
    money_relation: str = "tushare_moneyflow",
) -> str:
    if label_mode not in ('t1_exec', 'legacy'):
        raise ValueError('unsupported label mode')
    factor = 'a.adj_factor' if include_adjustment else '1.0'
    if calendar_relation not in ('tushare_trade_cal','verified_calendar_overlay'):
        raise ValueError('unsupported calendar relation')
    if adjustment_relation not in ('tushare_adj_factor','verified_adjustment') or money_relation not in ('tushare_moneyflow','verified_moneyflow'):
        raise ValueError('unsupported repair relation')
    session_source = (f"SELECT DISTINCT CAST(cal_date AS DATE) AS datetime FROM {calendar_relation} WHERE is_open"
                      if include_calendar else "SELECT datetime FROM coverage")
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
    LEFT JOIN qlib_stock_flow_features_v2 sf
      ON sf.trade_date = d.datetime AND sf.stock_code = d.instrument
      AND sf.quality_status IN ('research_candidate_not_certified','research_partial_features_not_certified')
    """

    if label_mode == "t1_exec":
        label_expr = "CASE WHEN d.close > 0 AND d.next_open > 0 AND d.next2_close IS NOT NULL THEN (d.next2_close / d.next_open - 1.0) * 100.0 END"
        label_date_expr = "d.next2_date"
    else:
        label_expr = "CASE WHEN d.next_close IS NOT NULL AND d.close > 0 THEN (d.next_close / d.close - 1.0) * 100.0 END"
        label_date_expr = "d.next_date"
    adjustment_cte = (
        f"""
    adjustments AS (
        SELECT CAST(date AS DATE) AS datetime,
               CAST(stock_code AS VARCHAR) AS instrument,
               max(CAST(adj_factor AS DOUBLE)) AS adj_factor
        FROM {adjustment_relation}
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
    money_window="PARTITION BY m.stock_code ORDER BY m.date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW"
    money_sum=f"SUM(CAST(m.net_mf_amount AS DOUBLE)) OVER ({money_window})"
    if money_relation=='verified_moneyflow':
        money_sum=f"CASE WHEN COUNT(m.net_mf_amount) OVER ({money_window})=5 AND MAX(s.session_seq) OVER ({money_window})-MIN(s.session_seq) OVER ({money_window})=4 THEN {money_sum} END"
    strict = adjustment_relation == 'verified_adjustment'
    one_guard = ' AND session_seq-prev_seq=1' if strict else ''
    five_guard = ' AND d.session_seq-d.prev5_seq=5 AND d.price_count6=6' if strict else ''
    volume_guard = ' AND d.volume_count20=20 AND d.volume_span20=19' if strict else ''
    volatility_expr = ('CASE WHEN d.return_count5=5 AND d.return_span5=4 THEN d.volatility_5d END'
                      if strict else 'd.volatility_5d')
    warmup_select = (", d.volume_count20 AS warmup_price_observations_20d, "
                     "(d.volume_count20=20 AND d.volume_span20=19) AS warmup_price_contiguous_20d"
                     if strict else '')
    return f"""
    WITH coverage AS (
        SELECT CAST(date AS DATE) AS datetime, count(DISTINCT stock_code) AS instruments
        FROM tushare_daily
        GROUP BY date
    ),
    session_dates AS ({session_source}),
    sessions AS (
        SELECT datetime, row_number() OVER (ORDER BY datetime) AS session_seq, lead(datetime) OVER (ORDER BY datetime) AS next_date,
               lead(datetime,2) OVER (ORDER BY datetime) AS next2_date FROM session_dates
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
            b.datetime, b.instrument, b.open, b.high, b.low, b.close, b.volume, b.turnover, s.session_seq,
            LAG(s.session_seq,1) OVER (PARTITION BY b.instrument ORDER BY b.datetime) AS prev_seq,
            LAG(s.session_seq,5) OVER (PARTITION BY b.instrument ORDER BY b.datetime) AS prev5_seq,
            COUNT(b.close) OVER (PARTITION BY b.instrument ORDER BY b.datetime ROWS BETWEEN 5 PRECEDING AND CURRENT ROW) AS price_count6,
            n.close AS next_close, s.next_date, n.open AS next_open,
            n2.close AS next2_close, s.next2_date,
            LAG(b.close, 1) OVER (PARTITION BY b.instrument ORDER BY b.datetime) AS prev_close,
            LAG(b.close, 5) OVER (PARTITION BY b.instrument ORDER BY b.datetime) AS prev5_close
        FROM bars b
        LEFT JOIN sessions s ON s.datetime=b.datetime
        LEFT JOIN bars n ON n.instrument=b.instrument AND n.datetime=s.next_date
        LEFT JOIN bars n2 ON n2.instrument=b.instrument AND n2.datetime=s.next2_date
    ),
    daily AS (
        SELECT
            datetime, instrument, open, high, low, close, volume, turnover, session_seq, prev_seq, prev5_seq, price_count6,
            next_close, next_date, next_open, next2_close, next2_date,
            prev_close, prev5_close,
            CASE WHEN prev_close > 0{one_guard}
                 THEN (close / prev_close - 1.0) * 100.0 END AS change_pct,
            STDDEV_SAMP(CASE WHEN prev_close > 0{one_guard}
                 THEN (close / prev_close - 1.0) * 100.0 END)
                 OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS volatility_5d,
            COUNT(CASE WHEN prev_close > 0{one_guard} THEN close END) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS return_count5,
            MAX(session_seq) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)-MIN(session_seq) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS return_span5,
            COUNT(volume) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS volume_count20,
            MAX(session_seq) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 19 PRECEDING AND CURRENT ROW)-MIN(session_seq) OVER (PARTITION BY instrument ORDER BY datetime ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS volume_span20,
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
            {money_sum} AS moneyflow_5d
        FROM {money_relation} m
        LEFT JOIN sessions s ON s.datetime=CAST(m.date AS DATE)
        WHERE m.date BETWEEN DATE '{start_date}' AND DATE '{end_date}'
    )
    SELECT
        d.datetime,
        d.instrument,
        {", ".join(f"d.{c}" for c in BASE_FEATURE_COLUMNS[:7])},
        b.turnover_rate, b.volume_ratio, b.pe, b.pb, b.total_mv, b.circ_mv,
        m.net_mf_amount, m.large_net_mf, m.extra_large_net_mf,
        CASE WHEN d.prev_close > 0{one_guard.replace('session_seq','d.session_seq').replace('prev_seq','d.prev_seq')} THEN (d.close / d.prev_close - 1.0) * 100.0 END AS ret_1d,
        CASE WHEN d.prev5_close > 0{five_guard} THEN (d.close / d.prev5_close - 1.0) * 100.0 END AS ret_5d,
        {volatility_expr} AS volatility_5d,
        m.moneyflow_5d,
        CASE WHEN d.std_volume20 > 0{volume_guard} THEN (d.volume - d.avg_volume20) / d.std_volume20 END AS volume_z20
        {warmup_select}
        {flow_select},
        {label_expr} AS label_next_ret,
        CASE WHEN {label_expr} IS NULL THEN 'missing_session_price_not_zero'
             ELSE 'historical_price_target_not_execution' END AS label_status,
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


def apply_calendar_overlay(con, folder):
    """Temporary research-only calendar replacement; source DB remains read-only."""
    from trade_system.v2.domain import file_hash,identity,now_utc,utc
    from tools.v2.probe_native_gaps import calendar_overlay
    folder=Path(folder).resolve(strict=True)
    paths=list(folder.iterdir())
    if any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError('flat sealed calendar receipt package required')
    members={p.name:file_hash(p) for p in paths if p.name!='completed.json'}
    if json.loads((folder/'completed.json').read_text(encoding='utf-8'))!={'members':members,'manifest_id':identity(members)}:
        raise ValueError('calendar receipt package changed')
    receipt=json.loads((folder/'calendar-relay-receipt.json').read_text(encoding='utf-8'))
    if (receipt['provider']!='xiaodefa_relay' or receipt['api']!='trade_cal'
        or receipt['params']!={'exchange':'SSE','start_date':'20240101','end_date':'20241231'}
        or utc(receipt['received_at'])>now_utc()):
        raise ValueError('exact historical relay calendar receipt required')
    overlay={**calendar_overlay(receipt['rows']),'receipt_sha256':identity(receipt)}
    if json.loads((folder/'calendar-overlay.json').read_text(encoding='utf-8'))!=overlay:
        raise ValueError('calendar overlay derivation changed')
    con.execute('CREATE TEMP TABLE verified_calendar_overlay(cal_date DATE, is_open BOOLEAN)')
    con.execute("INSERT INTO verified_calendar_overlay SELECT CAST(cal_date AS DATE),is_open FROM tushare_trade_cal WHERE year(CAST(cal_date AS DATE))<>2024")
    con.executemany('INSERT INTO verified_calendar_overlay VALUES (?,true)',[(d,) for d in overlay['open_days']])
    return {'manifest_id':identity(members),'receipt_sha256':identity(receipt),
            'scope':overlay['scope'],'year_replaced':2024,'open_days':len(overlay['open_days']),
            'source_database_modified':False,'historical_PIT_qualified':False}


def apply_research_overlay(con,folder,calendar_repair,start,end):
    from trade_system.v2.research_receipts import verify
    reg,data,metadata=verify(folder)
    if reg['origin']!='xiaodefa_relay':raise ValueError('fixtures cannot supply historical research repair')
    if not calendar_repair or calendar_repair['manifest_id']!=reg['calendar_manifest_id']:
        raise ValueError('repair requires same verified calendar')
    if not reg['start']<=start<=end<=reg['end']:raise ValueError('export outside repaired interval')
    expected=[str(r[0]) for r in con.execute('SELECT cal_date FROM verified_calendar_overlay WHERE is_open AND cal_date>=? ORDER BY cal_date LIMIT ?',
                                             [reg['start'],len(reg['days'])]).fetchall()]
    if expected!=reg['days'] or reg['days'][-3]!=reg['end']:raise ValueError('exact consecutive repaired sessions and T+2 required')
    # No mixing new factor anchors or CNY flows with unqualified legacy units.
    con.execute('CREATE TEMP TABLE verified_adjustment AS SELECT CAST(value->>\'date\' AS DATE) AS date, value->>\'stock_code\' AS stock_code, CAST(value->>\'adj_factor\' AS DOUBLE) AS adj_factor FROM json_each(?)',[json.dumps(data['adj_factor'])])
    cols=['net_mf_amount','buy_lg_amount','sell_lg_amount','buy_elg_amount','sell_elg_amount']
    values=','.join(f"CAST(value->>'{c}' AS DOUBLE) AS {c}" for c in cols)
    con.execute("CREATE TEMP TABLE verified_moneyflow AS SELECT CAST(value->>'date' AS DATE) AS date,value->>'stock_code' AS stock_code,"+values+' FROM json_each(?)',[json.dumps(data['moneyflow'])])
    coverage=[]
    for day in reg['days']:
        counts=con.execute('''SELECT count(DISTINCT d.stock_code),count(DISTINCT a.stock_code),count(DISTINCT m.stock_code)
          FROM tushare_daily d LEFT JOIN verified_adjustment a ON CAST(d.date AS DATE)=a.date AND d.stock_code=a.stock_code
          LEFT JOIN verified_moneyflow m ON CAST(d.date AS DATE)=m.date AND d.stock_code=m.stock_code WHERE d.date=?''',[day]).fetchone()
        coverage.append({'date':day,'price_instruments':counts[0],'factor_observed':counts[1],'moneyflow_observed':counts[2]})
    return {**metadata,'coverage':coverage,'factor_source':'sealed_daily_adj_factor_no_legacy_anchor_mix',
            'receipt_start':reg['days'][0],'receipt_end':reg['days'][-1],
            'moneyflow_5d_policy':'five_consecutive_calendar_sessions_nonnull_required',
            'source_database_modified':False,'receipt_time_used_for_prospective_qualification':False}


def assert_unique_research_keys(con, start, end, *, adjustment_relation=None,
                                money_relation='tushare_moneyflow', include_flow=False):
    """Fail before window calculations; code-format duplicates are not two instruments."""
    if adjustment_relation not in (None, 'tushare_adj_factor', 'verified_adjustment') or money_relation not in ('tushare_moneyflow', 'verified_moneyflow'):
        raise ValueError('unsupported uniqueness relation')
    relations = [('tushare_daily', 'date'), ('tushare_daily_basic', 'date'), (money_relation, 'date')]
    if adjustment_relation:
        relations.append((adjustment_relation, 'date'))
    if include_flow:
        relations.append(('qlib_stock_flow_features_v2', 'trade_date'))
    for table, day in relations:
        duplicate = con.execute(f'''SELECT stock_code,CAST({day} AS DATE),count(*) FROM {table}
            WHERE {day} BETWEEN ? AND ? GROUP BY stock_code,CAST({day} AS DATE)
            HAVING count(*)>1 ORDER BY stock_code,CAST({day} AS DATE) LIMIT 1''', [start, end]).fetchone()
        if duplicate:
            raise ValueError(f'duplicate_research_source_key:{table}:{duplicate[0]}:{duplicate[1]}:{duplicate[2]}; explicit evidence-bound resolution required')


def export_features(
    db_path: str | Path,
    output: str | Path,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    output_format: str = "csv",
    label_mode: str = "t1_exec",
    calendar_overlay: str | Path | None = None,
    research_overlay: str | Path | None = None,
    semantics: str | Path | None = None,
    canonical_prices: str | Path | None = None,
    price_receipts: str | Path | None = None,
    identity_candidate: str | Path | None = None,
    identity_price_layer: str | Path | None = None,
    identity_receipts: str | Path | None = None,
) -> dict[str, object]:
    candidate_options = (identity_candidate, identity_price_layer, identity_receipts)
    if any(x is not None for x in candidate_options) and (
            any(x is None for x in candidate_options) or semantics is None
            or calendar_overlay is None or research_overlay is None):
        raise ValueError('candidate requires paired corpus, price layer, receipts and sealed semantic research protocol')
    if canonical_prices is not None or price_receipts is not None:
        if canonical_prices is None or price_receipts is None or any(x is not None for x in (calendar_overlay,research_overlay,semantics,*candidate_options)) or label_mode!='t1_exec':
            raise ValueError('canonical v7 requires paired layer/receipts and excludes unqualified mixed protocols')
        from tools.v2.canonical_price_research import export_features as export_canonical
        return export_canonical(db_path, output, layer=canonical_prices, receipts=price_receipts,
            start_date=start_date, end_date=end_date, output_format=output_format)
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
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name='qlib_stock_flow_features_v2'"
        ).fetchone()[0] > 0
        adjustment_available = con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name='tushare_adj_factor'"
        ).fetchone()[0] > 0
        calendar_available = con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='tushare_trade_cal'").fetchone()[0] > 0
        if calendar_available:
            calendar_available = con.execute('SELECT count(*) FROM tushare_trade_cal WHERE is_open').fetchone()[0] > 0
        repair=None
        if calendar_overlay is not None:
            if not calendar_available:raise ValueError('existing calendar outside overlay required')
            repair=apply_calendar_overlay(con,calendar_overlay)
        research_repair=None
        if research_overlay is not None:
            research_repair=apply_research_overlay(con,research_overlay,repair,start,end)
            adjustment_available=True
            # Old derived flows have not been rebuilt from this sealed CNY corpus.
            flow_features_available=False
        feature_columns = BASE_FEATURE_COLUMNS + (FLOW_FEATURE_COLUMNS if flow_features_available else [])
        flow_versions = []
        if flow_features_available:
            flow_cols = {r[1] for r in con.execute("PRAGMA table_info('qlib_stock_flow_features_v2')").fetchall()}
            flow_versions = ([r[0] for r in con.execute('SELECT DISTINCT feature_version FROM qlib_stock_flow_features_v2 ORDER BY 1').fetchall()]
                             if 'feature_version' in flow_cols else ['unversioned_legacy'])
        query_start = research_repair['receipt_start'] if research_repair else str(min_date)[:10]
        query_end = research_repair['receipt_end'] if research_repair else str(max_date)[:10]
        assert_unique_research_keys(con, query_start, query_end,
            adjustment_relation=('verified_adjustment' if research_repair else 'tushare_adj_factor') if adjustment_available else None,
            money_relation='verified_moneyflow' if research_repair else 'tushare_moneyflow',
            include_flow=flow_features_available)
        run_dir = out.parent / (out.stem + '.versions') / uuid.uuid4().hex
        run_dir.mkdir(parents=True)
        formats = {output_format} if output_format != "both" else {"csv", "parquet"}
        if not formats <= {'csv', 'parquet'}:
            raise ValueError('unsupported export format')
        # Compute the complete observation windows exactly once, from source
        # history (including pre-start warmup), then partition the materialized
        # result. Calendar-day padding is not a trading-observation window.
        query = _query(query_start, query_end,
                       include_flow_features=flow_features_available,
                       include_adjustment=adjustment_available, label_mode=label_mode, include_calendar=calendar_available,
                       calendar_relation='verified_calendar_overlay' if repair else 'tushare_trade_cal',
                       adjustment_relation='verified_adjustment' if research_repair else 'tushare_adj_factor',
                       money_relation='verified_moneyflow' if research_repair else 'tushare_moneyflow')
        con.execute(f'CREATE TEMP TABLE all_features AS {query}')
        semantic_meta=None
        if semantics is not None:
            if not research_repair or label_mode!='t1_exec':
                raise ValueError('semantics requires sealed research repair and exact T1/T2 labels')
            from trade_system.v2.research_semantics import apply
            semantic_meta=apply(con,semantics)
            semantic_meta['output_label_status_counts']=dict(con.execute(
                'SELECT label_status,count(*) FROM semantic_features WHERE datetime BETWEEN ? AND ? GROUP BY label_status ORDER BY label_status',
                [start,end]).fetchall())
        candidate_meta = None
        if identity_candidate is not None:
            from tools.v2 import build_identity_candidate, identity_research_columns
            candidate_payload = build_identity_candidate.verify(identity_candidate,
                identity_price_layer, identity_receipts, db_path, semantics)
            candidate_meta = identity_research_columns.apply(con, candidate_payload, start=start, end=end)
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
                materialized_table='candidate_features' if candidate_meta else 'semantic_features' if semantic_meta else 'all_features',
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
            "label_version": "market_session_aligned_v6_semantic_exclusions" if semantic_meta else "market_session_aligned_v5_sealed_warmup" if research_repair else "market_session_aligned_v3_calendar_overlay" if repair else "market_session_aligned_v2",
            "research_semantics":semantic_meta,
            "identity_candidate":candidate_meta,
            "candidate_feature_columns":candidate_meta['feature_columns'] if candidate_meta else [],
            "research_ready":False,
            "execution_ready":False,
            "warmup_policy": "repair: exact contiguous full price and money windows; legacy derived flow features excluded" if research_repair else "legacy_observation_windows",
            "research_repair":research_repair,
            "moneyflow_unit":"CNY" if research_repair else "legacy_source_unit_not_certified",
            "calendar_repair":repair,
            "flow_feature_versions": flow_versions,
            "calendar_evidence": "stored_plus_sealed_2024_relay_overlay_not_PIT" if repair else "stored_calendar_not_native_authenticated" if calendar_available else "observed_market_dates_unverified",
            "execution_assumption": "buy at next session open, sell at following session close" if label_mode == "t1_exec" else "not execution-aware",
            "source_tables": [
            "tushare_daily", "tushare_daily_basic", "verified_moneyflow" if research_repair else "tushare_moneyflow",
                *( (("verified_adjustment" if research_repair else "tushare_adj_factor"),) if adjustment_available else () ),
                *( ("qlib_stock_flow_features_v2",) if flow_features_available else () ),
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
        if candidate_meta:
            checked_candidate = build_identity_candidate.verify(identity_candidate,
                identity_price_layer, identity_receipts, db_path, semantics)
            if checked_candidate != candidate_payload:
                raise ValueError('candidate evidence changed during export')
            from trade_system.v2.domain import file_hash
            if file_hash(identity_research_columns.__file__) != candidate_meta['consumer_source_sha256']:
                raise ValueError('candidate consumer changed during export')
        if semantic_meta:
            from trade_system.v2.research_semantics import validate
            checked=validate(semantics)[1]
            if any(semantic_meta[k]!=v for k,v in checked.items()):
                raise ValueError('semantic evidence changed during export')
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
    parser.add_argument("--calendar-overlay", help="Sealed receipt package; replaces 2024 calendar in temporary research state only")
    parser.add_argument("--research-overlay", help="Sealed daily factors and CNY flows; no legacy anchor/unit mixing")
    parser.add_argument("--semantics", help="Hash-bound retrospective identity and suspension exclusions; not tradability approval")
    parser.add_argument("--canonical-prices", help="Sealed native reconciled price slice; explicit v7 six-field diagnostic protocol")
    parser.add_argument("--price-receipts", help="Raw batch receipts required to replay canonical price resolution")
    parser.add_argument("--identity-candidate", help="Sealed candidate-only corpus, never default model inputs")
    parser.add_argument("--identity-price-layer", help="Verified unit layer bound to candidate")
    parser.add_argument("--identity-receipts", help="Original identity observations required for candidate replay")
    args = parser.parse_args()
    result = export_features(
        args.db,
        args.out,
        start_date=args.start_date,
        end_date=args.end_date,
        output_format=args.format,
        label_mode=args.label_mode,
        calendar_overlay=args.calendar_overlay,
        research_overlay=args.research_overlay,
        semantics=args.semantics,
        canonical_prices=args.canonical_prices,
        price_receipts=args.price_receipts,
        identity_candidate=args.identity_candidate,
        identity_price_layer=args.identity_price_layer,
        identity_receipts=args.identity_receipts,
    )
    print(f"rows={result['rows']} labeled_rows={result['labeled_rows']} instruments={result['instruments']}")
    print(f"date_range={result['start_date']}..{result['end_date']}")
    print(f"outputs={result['outputs']}")
    print("leakage_guard=label_next_ret_and_label_date_are_targets_only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
