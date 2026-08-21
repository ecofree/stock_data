"""Deterministic QLib/flow candidate fusion; never creates executable orders."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.ml.qlib_shadow import ensure_qlib_shadow_tables


def _iso_date(value: str | None) -> str | None:
    if value is None:
        return None
    text = "".join(ch for ch in str(value) if ch.isdigit())
    if len(text) < 8:
        raise ValueError(f"invalid trade date: {value}")
    return f"{text[:4]}-{text[4:6]}-{text[6:8]}"


def _pct_rank(values: dict[str, float | None], *, higher_is_better: bool = True) -> dict[str, float]:
    valid = sorted(
        ((key, float(value)) for key, value in values.items() if value is not None),
        key=lambda item: item[1],
        reverse=higher_is_better,
    )
    if not valid:
        return {key: 0.0 for key in values}
    denominator = max(1, len(valid) - 1)
    ranks = {key: 100.0 * (1.0 - index / denominator) for index, (key, _) in enumerate(valid)}
    return {key: round(ranks.get(key, 0.0), 4) for key in values}


def ensure_candidate_pool_table(db_path: str | Path) -> None:
    con = duckdb.connect(str(db_path))
    try:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS qlib_candidate_pool (
                trade_date VARCHAR,
                model_id VARCHAR,
                stock_code VARCHAR,
                stock_name VARCHAR,
                qlib_score DOUBLE,
                qlib_rank INTEGER,
                stock_flow_5d DOUBLE,
                sector_flow_5d DOUBLE,
                stock_flow_rank DOUBLE,
                sector_flow_rank DOUBLE,
                rule_score DOUBLE,
                combined_score DOUBLE,
                model_status VARCHAR,
                candidate_status VARCHAR,
                risk_approved BOOLEAN DEFAULT false,
                is_executable BOOLEAN DEFAULT false,
                blocker_reason VARCHAR,
                evidence_json VARCHAR,
                generated_at TIMESTAMP DEFAULT current_timestamp,
                PRIMARY KEY (trade_date, model_id, stock_code)
            )
            """
        )
    finally:
        con.close()


def build_candidate_pool(
    db_path: str | Path,
    *,
    model_id: str,
    trade_date: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    ensure_qlib_shadow_tables(db_path)
    ensure_candidate_pool_table(db_path)
    con = duckdb.connect(str(db_path))
    try:
        status_row = con.execute("SELECT status FROM qlib_model_registry WHERE model_id = ?", [model_id]).fetchone()
        if not status_row:
            raise RuntimeError(f"model is not registered: {model_id}")
        model_status = str(status_row[0] or "shadow")
        selected_date = _iso_date(trade_date) or str(con.execute("SELECT max(trade_date) FROM qlib_prediction WHERE model_id = ?", [model_id]).fetchone()[0] or "")[:10]
        if not selected_date:
            return {"trade_date": None, "model_id": model_id, "rows": 0, "risk_approved": 0, "executable": 0}
        predictions = con.execute(
            """
            SELECT symbol, score, rank, horizon
            FROM qlib_prediction
            WHERE model_id = ? AND trade_date = ?
            ORDER BY rank NULLS LAST, symbol
            """,
            [model_id, selected_date],
        ).fetchall()
        if not predictions:
            raise RuntimeError(f"no predictions for {model_id} on {selected_date}")
        codes = [str(row[0]) for row in predictions]
        placeholders = ",".join("?" for _ in codes)
        stock_flow = {
            str(row[0]): dict(zip(("stock_code", "main_net_5d", "observed_days_20d", "quality_status"), row))
            for row in con.execute(
                f"SELECT stock_code, main_net_5d, observed_days_20d, quality_status FROM qlib_stock_flow_features WHERE trade_date = ? AND stock_code IN ({placeholders})",
                [selected_date, *codes],
            ).fetchall()
        }
        # Use the latest THS membership snapshot not later than the prediction
        # date.  This avoids joining today's membership to an older signal.
        membership_date = con.execute(
            "SELECT max(trade_date) FROM ths_concept_stock_history WHERE trade_date <= ? AND date_verified = true",
            [selected_date],
        ).fetchone()[0]
        sector_flow: dict[str, float | None] = {}
        if membership_date:
            rows = con.execute(
                f"""
                SELECT m.stock_code, avg(sf.main_net_5d) AS sector_flow_5d
                FROM ths_concept_stock_history m
                JOIN qlib_sector_flow_features sf
                  ON sf.trade_date = m.trade_date AND sf.sector_code = m.concept_code
                WHERE m.trade_date = ? AND m.date_verified = true AND m.stock_code IN ({placeholders})
                GROUP BY m.stock_code
                """,
                [membership_date, *codes],
            ).fetchall()
            sector_flow = {str(row[0]): (float(row[1]) if row[1] is not None else None) for row in rows}
        rule_scores = {
            str(row[0]): float(row[1]) if row[1] is not None else None
            for row in con.execute(
                f"SELECT stock_code, score FROM stock_candidate_score WHERE trade_date = ? AND stock_code IN ({placeholders})",
                [selected_date, *codes],
            ).fetchall()
        }
        qlib_scores = {str(row[0]): float(row[1]) if row[1] is not None else None for row in predictions}
        stock_values = {code: (stock_flow.get(code) or {}).get("main_net_5d") for code in codes}
        sector_values = {code: sector_flow.get(code) for code in codes}
        rule_values = {code: rule_scores.get(code) for code in codes}
        stock_ranks = _pct_rank(stock_values)
        sector_ranks = _pct_rank(sector_values)
        rule_ranks = _pct_rank(rule_values)
        qlib_ranks = _pct_rank(qlib_scores)
        output_rows = []
        for symbol, score, rank, horizon in predictions:
            code = str(symbol)
            sf = stock_flow.get(code) or {}
            stock_net = sf.get("main_net_5d")
            sector_net = sector_values.get(code)
            combined = round(
                0.50 * qlib_ranks.get(code, 0.0)
                + 0.25 * stock_ranks.get(code, 0.0)
                + 0.15 * sector_ranks.get(code, 0.0)
                + 0.10 * rule_ranks.get(code, 0.0),
                4,
            )
            blockers = []
            if model_status != "champion":
                blockers.append("model_not_champion")
            if stock_net is None:
                blockers.append("stock_flow_missing")
            if sector_net is None:
                blockers.append("sector_flow_missing")
            if sf.get("quality_status") not in ("real", "success", "usable"):
                blockers.append("stock_flow_quality_not_real")
            candidate_status = "model_qualified" if not blockers else "research_only"
            evidence = {
                "trade_date": selected_date,
                "model_id": model_id,
                "model_status": model_status,
                "prediction_horizon": horizon,
                "membership_date": str(membership_date)[:10] if membership_date else None,
                "stock_flow_quality": sf.get("quality_status"),
                "blockers": blockers,
            }
            output_rows.append({
                "trade_date": selected_date,
                "model_id": model_id,
                "stock_code": code,
                "stock_name": None,
                "qlib_score": float(score) if score is not None else None,
                "qlib_rank": int(rank) if rank is not None else None,
                "stock_flow_5d": stock_net,
                "sector_flow_5d": sector_net,
                "stock_flow_rank": stock_ranks.get(code, 0.0),
                "sector_flow_rank": sector_ranks.get(code, 0.0),
                "rule_score": rule_values.get(code),
                "combined_score": combined,
                "model_status": model_status,
                "candidate_status": candidate_status,
                "risk_approved": False,
                "is_executable": False,
                "blocker_reason": ";".join(blockers) or "risk_gate_not_approved",
                "evidence_json": json.dumps(evidence, ensure_ascii=False, sort_keys=True),
            })
        output_rows.sort(key=lambda row: (-row["combined_score"], row["stock_code"]))
        output_rows = output_rows[: max(1, int(limit))]
        con.execute("DELETE FROM qlib_candidate_pool WHERE trade_date = ? AND model_id = ?", [selected_date, model_id])
        if output_rows:
            con.executemany(
                """
                INSERT INTO qlib_candidate_pool (
                    trade_date, model_id, stock_code, stock_name, qlib_score, qlib_rank,
                    stock_flow_5d, sector_flow_5d, stock_flow_rank, sector_flow_rank,
                    rule_score, combined_score, model_status, candidate_status,
                    risk_approved, is_executable, blocker_reason, evidence_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [[row[key] for key in (
                    "trade_date", "model_id", "stock_code", "stock_name", "qlib_score", "qlib_rank",
                    "stock_flow_5d", "sector_flow_5d", "stock_flow_rank", "sector_flow_rank",
                    "rule_score", "combined_score", "model_status", "candidate_status",
                    "risk_approved", "is_executable", "blocker_reason", "evidence_json"
                )] for row in output_rows],
            )
        return {
            "trade_date": selected_date,
            "model_id": model_id,
            "model_status": model_status,
            "rows": len(output_rows),
            "risk_approved": sum(1 for row in output_rows if row["risk_approved"]),
            "executable": sum(1 for row in output_rows if row["is_executable"]),
            "membership_date": str(membership_date)[:10] if membership_date else None,
        }
    finally:
        con.close()
