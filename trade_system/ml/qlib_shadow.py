"""Qlib shadow-mode prediction import and schema helpers."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import duckdb


PREDICTION_COLUMNS = [
    "trade_date",
    "symbol",
    "model_id",
    "score",
    "rank",
    "horizon",
    "prediction_payload_hash",
]


def _relation_exists(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    table_count = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0]
    view_count = con.execute(
        "SELECT count(*) FROM information_schema.views WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0]
    return bool(table_count or view_count)


def ensure_qlib_shadow_tables(db_path: str | Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS qlib_model_registry (
                model_id VARCHAR PRIMARY KEY,
                model_name VARCHAR,
                factor_set VARCHAR,
                train_start VARCHAR,
                train_end VARCHAR,
                predict_horizon VARCHAR,
                source_project VARCHAR,
                model_file_ref VARCHAR,
                status VARCHAR,
                notes VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS qlib_prediction (
                trade_date VARCHAR,
                symbol VARCHAR,
                model_id VARCHAR,
                score DOUBLE,
                rank INTEGER,
                horizon VARCHAR,
                prediction_payload_hash VARCHAR
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS qlib_shadow_evaluation (
                model_id VARCHAR,
                sample_start VARCHAR,
                sample_end VARCHAR,
                sample_count INTEGER,
                ic DOUBLE,
                rank_ic DOUBLE,
                avg_forward_return DOUBLE,
                top_quantile_return DOUBLE,
                bottom_quantile_return DOUBLE,
                top_bottom_spread DOUBLE,
                hit_rate DOUBLE,
                top_hit_rate DOUBLE,
                daily_top_hit_rate DOUBLE,
                max_drawdown DOUBLE
            )
            """
        )
        for column, kind in (
            ("avg_forward_return", "DOUBLE"),
            ("avg_net_return", "DOUBLE"),
            ("top_bottom_spread", "DOUBLE"),
            ("net_top_bottom_spread", "DOUBLE"),
            ("top_quantile_net_return", "DOUBLE"),
            ("bottom_quantile_net_return", "DOUBLE"),
            ("top_hit_rate", "DOUBLE"),
            ("daily_top_hit_rate", "DOUBLE"),
            ("evaluation_method", "VARCHAR"),
            ("quantile", "DOUBLE"),
            ("updated_at", "TIMESTAMP"),
        ):
            con.execute(
                f"ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS {column} {kind}"
            )
        if _relation_exists(con, "strategy_scan_result"):
            con.execute(
                """
                CREATE OR REPLACE VIEW v_qlib_shadow_candidate_overlap AS
                SELECT
                    p.trade_date,
                    p.symbol,
                    p.model_id,
                    p.score AS qlib_score,
                    p.rank AS qlib_rank,
                    p.horizon,
                    s.stage AS candidate_stage,
                    s.strategy_id,
                    s.score AS strategy_score
                FROM qlib_prediction p
                LEFT JOIN strategy_scan_result s
                  ON p.trade_date = s.trade_date AND p.symbol = s.symbol
                """
            )
        else:
            con.execute(
                """
                CREATE OR REPLACE VIEW v_qlib_shadow_candidate_overlap AS
                SELECT
                    trade_date,
                    symbol,
                    model_id,
                    score AS qlib_score,
                    rank AS qlib_rank,
                    horizon,
                    CAST(NULL AS VARCHAR) AS candidate_stage,
                    CAST(NULL AS VARCHAR) AS strategy_id,
                    CAST(NULL AS DOUBLE) AS strategy_score
                FROM qlib_prediction
                """
            )
    finally:
        con.close()


def _hash_payload(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def import_qlib_predictions(
    db_path: str | Path,
    *,
    model_id: str,
    model_name: str,
    factor_set: str,
    rows: list[dict[str, Any]],
    train_start: str = "",
    train_end: str = "",
    predict_horizon: str = "t1",
    source_project: str = "external",
    model_file_ref: str = "",
    status: str = "shadow",
    notes: str = "",
) -> dict[str, int]:
    ensure_qlib_shadow_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute("DELETE FROM qlib_model_registry WHERE model_id = ?", [model_id])
        con.execute(
            """
            INSERT INTO qlib_model_registry (
                model_id, model_name, factor_set, train_start, train_end,
                predict_horizon, source_project, model_file_ref, status, notes
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                model_id,
                model_name,
                factor_set,
                train_start,
                train_end,
                predict_horizon,
                source_project,
                model_file_ref,
                status,
                notes,
            ],
        )
        con.execute("DELETE FROM qlib_prediction WHERE model_id = ?", [model_id])
        values_batch = []
        for row in rows:
            payload_hash = _hash_payload(row)
            values_batch.append([
                str(row.get("trade_date", "")),
                str(row.get("symbol", "")),
                model_id,
                float(row.get("score", 0) or 0),
                int(row.get("rank", 0) or 0),
                str(row.get("horizon", predict_horizon)),
                payload_hash,
            ])
        if values_batch:
            # Register one in-memory frame and insert it in a single SQL
            # statement.  Executemany is still effectively row-at-a-time for
            # large prediction sets and leaves partial rows if a run is
            # interrupted.
            import pandas as pd

            batch = pd.DataFrame(values_batch, columns=PREDICTION_COLUMNS)
            con.register("_qlib_prediction_batch", batch)
            try:
                con.execute(
                    "INSERT INTO qlib_prediction "
                    "SELECT trade_date, symbol, model_id, score, rank, horizon, prediction_payload_hash "
                    "FROM _qlib_prediction_batch"
                )
            finally:
                con.unregister("_qlib_prediction_batch")
        # Do not open a second DuckDB connection while ``con`` is still
        # active.  DuckDB serializes writers and the previous nested call
        # could wait forever after the model file had already been written.
        return {
            "qlib_model_registry": int(con.execute("SELECT count(*) FROM qlib_model_registry").fetchone()[0]),
            "qlib_prediction": int(
                con.execute("SELECT count(*) FROM qlib_prediction WHERE model_id = ?", [model_id]).fetchone()[0]
            ),
        }
    finally:
        con.close()


def import_qlib_predictions_for_date(
    db_path: str | Path,
    *,
    model_id: str,
    trade_date: str,
    rows: list[dict[str, Any]],
    predict_horizon: str = "t1_exec",
) -> int:
    """Replace one prediction date while preserving the model registry.

    Daily inference must not delete historical predictions or downgrade a
    champion's registry status.  This helper is intentionally date-scoped.
    """
    ensure_qlib_shadow_tables(db_path)
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            "DELETE FROM qlib_prediction WHERE model_id = ? AND trade_date = ?",
            [model_id, str(trade_date)],
        )
        values = []
        for row in rows:
            payload = dict(row)
            values.append([
                str(trade_date),
                str(payload.get("symbol", "")),
                model_id,
                float(payload.get("score", 0.0) or 0.0),
                int(payload.get("rank", 0) or 0),
                str(payload.get("horizon", predict_horizon)),
                _hash_payload(payload),
            ])
        if values:
            import pandas as pd

            batch = pd.DataFrame(values, columns=PREDICTION_COLUMNS)
            con.register("_qlib_prediction_daily_batch", batch)
            try:
                con.execute(
                    "INSERT INTO qlib_prediction "
                    "SELECT trade_date, symbol, model_id, score, rank, horizon, prediction_payload_hash "
                    "FROM _qlib_prediction_daily_batch"
                )
            finally:
                con.unregister("_qlib_prediction_daily_batch")
        return len(values)
    finally:
        con.close()
