"""Trading signal generation for market regime, sectors, candidates, and alerts."""

from __future__ import annotations

import json
from pathlib import Path

import duckdb

from trade_system.integrity import ensure_unique_indexes
from trade_system.normalize import build_normalized_views
from trade_system.quality import table_columns, table_exists
from trade_system.readiness import assess_trade_date_readiness
from trade_system.stage_signals import ensure_stage_signal_schema
from trade_system.db_utils import fetch_dicts as _fetch_dicts
from trade_system.kline_access import canonical_daily_kline_relation


_SIGNAL_INDEX_SPECS = (
    ("uq_market_regime_date", "market_regime_snapshot", ("trade_date",)),
    ("uq_sector_rotation_date_code", "sector_rotation_score", ("trade_date", "sector_code")),
    ("uq_candidate_date_code", "stock_candidate_score", ("trade_date", "stock_code")),
    (
        "uq_stage_signal_date_stage_code",
        "stock_candidate_stage_signal",
        ("trade_date", "stage", "stock_code"),
    ),
)


def classify_market_regime(row: dict) -> dict:
    limit_up = int(row.get("limit_up_count") or 0)
    limit_down = int(row.get("limit_down_count") or 0)
    rise = int(row.get("rise_count") or 0)
    fall = int(row.get("fall_count") or 0)
    consecutive = int(row.get("consecutive_count") or 0)
    earning_effect = row.get("earning_effect_score")
    acute_drop_risk = row.get("acute_drop_risk_score")

    if limit_up >= 80 and limit_down <= 5:
        regime = "高潮"
        position = 30
        score = 85
    elif limit_up >= 40 and limit_down <= 15 and consecutive >= 5:
        regime = "主升"
        position = 70
        score = 75
    elif limit_up >= 20 and rise >= fall:
        regime = "启动"
        position = 45
        score = 55
    elif limit_up < 20 and limit_down >= 30:
        regime = "冰点"
        position = 15
        score = 20
    elif limit_down >= 30 or fall > rise * 1.5:
        regime = "退潮"
        position = 5
        score = 15
    else:
        regime = "震荡"
        position = 25
        score = 40

    return {
        "regime": regime,
        "regime_score": score,
        "suggested_position_pct": position,
        "evidence": {
            "limit_up_count": limit_up,
            "limit_down_count": limit_down,
            "rise_count": rise,
            "fall_count": fall,
            "consecutive_count": consecutive,
            "broken_limit_up_count": row.get("broken_limit_up_count"),
            "blown_limit_up_count": row.get("blown_limit_up_count"),
            "blown_limit_up_rate": row.get("blown_limit_up_rate"),
            "success_rate": row.get("success_rate"),
            "earning_effect_score": earning_effect,
            "acute_drop_risk_score": acute_drop_risk,
            "input_source": row.get("source_table"),
            "is_fallback": bool(row.get("is_fallback")) if row.get("is_fallback") is not None else None,
            "score_components": {
                "rule_score": score,
                "limit_up_count": limit_up,
                "limit_down_count": limit_down,
                "rise_fall_balance": rise - fall,
                "earning_effect_score": earning_effect,
                "acute_drop_risk_score": acute_drop_risk,
            },
            "rule": (
                "limit_up>=80 and limit_down<=5 -> 高潮; "
                "limit_up>=40 and limit_down<=15 and consecutive>=5 -> 主升; "
                "limit_down>=30 or fall>rise*1.5 -> 退潮"
            ),
        },
    }



def _relation_has_rows(con: duckdb.DuckDBPyConnection, relation_name: str) -> bool:
    try:
        row = con.execute(f'SELECT count(*) FROM "{relation_name}"').fetchone()
    except Exception:
        return False
    return bool(row and row[0] > 0)


def _sample_stats(
    con: duckdb.DuckDBPyConnection,
    relation_name: str,
    trade_date: str,
    date_column: str = "trade_date",
    extra_where: str = "",
    extra_params: list | None = None,
) -> dict:
    if relation_name == "v_kline_daily" and table_exists(con, "tushare_daily"):
        # This function runs inside signal generation's write transaction. An
        # OOM while expanding the all-history compatibility view would abort
        # that transaction before the candidate INSERTs are attempted.
        relation_name = "tushare_daily"
        date_column = "date"
    params = [trade_date] + list(extra_params or [])
    try:
        row = con.execute(
            f"""
            SELECT
                count(*) AS sample_count,
                min(CAST({date_column} AS VARCHAR)) AS first_date,
                max(CAST({date_column} AS VARCHAR)) AS last_date
            FROM "{relation_name}"
            WHERE CAST({date_column} AS VARCHAR) <= ? {extra_where}
            """,
            params,
        ).fetchone()
    except Exception:
        return {"relation": relation_name, "sample_count": 0, "first_date": None, "last_date": None}
    return {
        "relation": relation_name,
        "sample_count": int(row[0] or 0),
        "first_date": row[1],
        "last_date": row[2],
    }


def _latest_trade_date(con: duckdb.DuckDBPyConnection) -> str | None:
    row = con.execute("SELECT max(trade_date) FROM v_market_daily").fetchone()
    return row[0] if row else None


def _ensure_signal_tables(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS market_regime_snapshot (
            trade_date VARCHAR,
            regime VARCHAR,
            regime_score DOUBLE,
            suggested_position_pct INTEGER,
            evidence_json VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS sector_rotation_score (
            trade_date VARCHAR,
            sector_code VARCHAR,
            sector_name VARCHAR,
            taxonomy VARCHAR DEFAULT 'unknown',
            score DOUBLE,
            strength_value DOUBLE,
            limit_up_count INTEGER,
            seal_rate DOUBLE,
            evidence_json VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_candidate_score (
            trade_date VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            score DOUBLE,
            source VARCHAR,
            sector_code VARCHAR,
            evidence_json VARCHAR,
            is_actionable BOOLEAN DEFAULT false,
            candidate_status VARCHAR DEFAULT 'research_only',
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    candidate_columns = {
        "is_actionable": "BOOLEAN DEFAULT false",
        "candidate_status": "VARCHAR DEFAULT 'research_only'",
    }
    rotation_columns = {"taxonomy": "VARCHAR DEFAULT 'unknown'"}
    existing_rotation_columns = set(table_columns(con, "sector_rotation_score"))
    for column, data_type in rotation_columns.items():
        if column not in existing_rotation_columns:
            con.execute(f'ALTER TABLE sector_rotation_score ADD COLUMN "{column}" {data_type}')
    existing_candidate_columns = set(table_columns(con, "stock_candidate_score"))
    for column, data_type in candidate_columns.items():
        if column not in existing_candidate_columns:
            con.execute(f'ALTER TABLE stock_candidate_score ADD COLUMN "{column}" {data_type}')
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS stock_candidate_stage_signal (
            trade_date VARCHAR,
            stage VARCHAR,
            stock_code VARCHAR,
            stock_name VARCHAR,
            score DOUBLE,
            decision VARCHAR,
            evidence_json VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS alert_events (
            trade_date VARCHAR,
            severity VARCHAR,
            category VARCHAR,
            message VARCHAR,
            evidence_json VARCHAR,
            generated_at TIMESTAMP DEFAULT current_timestamp
        )
        """
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_market_regime_date "
        "ON market_regime_snapshot(trade_date)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_sector_rotation_date_code "
        "ON sector_rotation_score(trade_date, sector_code)"
    )
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_candidate_date_code "
        "ON stock_candidate_score(trade_date, stock_code)"
    )
    ensure_stage_signal_schema(con)


def _clear_signal_date(con: duckdb.DuckDBPyConnection, trade_date: str) -> None:
    for table in (
        "market_regime_snapshot",
        "sector_rotation_score",
        "stock_candidate_score",
        "alert_events",
    ):
        con.execute(f"DELETE FROM {table} WHERE trade_date = ?", [trade_date])


def _drop_signal_indexes(con: duckdb.DuckDBPyConnection) -> None:
    """Drop replace-snapshot indexes before a delete/insert transaction."""
    for index_name, _, _ in _SIGNAL_INDEX_SPECS:
        con.execute(f'DROP INDEX IF EXISTS "{index_name}"')


def _create_signal_indexes(con: duckdb.DuckDBPyConnection) -> None:
    for index_name, table, columns in _SIGNAL_INDEX_SPECS:
        column_sql = ", ".join(f'"{column}"' for column in columns)
        con.execute(
            f'CREATE UNIQUE INDEX "{index_name}" ON "{table}" ({column_sql})'
        )


def _generate_regime(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict:
    rows = _fetch_dicts(con, "SELECT * FROM v_market_state_inputs WHERE trade_date = ?", [trade_date])
    if not rows:
        rows = _fetch_dicts(con, "SELECT * FROM v_market_daily WHERE trade_date = ?", [trade_date])
    if not rows:
        regime = {
            "regime": "数据缺失",
            "regime_score": 0,
            "suggested_position_pct": 0,
            "evidence": {"reason": "v_market_daily has no row for trade_date"},
        }
    else:
        regime = classify_market_regime(rows[0])
    regime["evidence"]["sample_stats"] = _sample_stats(con, "v_market_daily", trade_date)
    con.execute(
        """
        INSERT INTO market_regime_snapshot
        (trade_date, regime, regime_score, suggested_position_pct, evidence_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            trade_date,
            regime["regime"],
            regime["regime_score"],
            regime["suggested_position_pct"],
            json.dumps(regime["evidence"], ensure_ascii=False),
        ],
    )
    return regime


def _generate_sectors(con: duckdb.DuckDBPyConnection, trade_date: str) -> int:
    # Prefer the THS-only mainline view.  A live multi-source snapshot with no
    # THS rows is a genuine missing-concept condition, not permission to fall
    # back to a mixed BK/THS ranking.  Tiny legacy/test databases without the
    # multi-source contract retain the old industry fallback for compatibility.
    theme_rows = 0
    if table_exists(con, "v_theme_mainline_evidence"):
        theme_rows = int(con.execute(
            "SELECT count(*) FROM v_theme_mainline_evidence WHERE trade_date=?",
            [trade_date],
        ).fetchone()[0] or 0)
    multi_source_live = table_exists(con, "multi_source_sector_flow") and int(con.execute(
        "SELECT count(*) FROM multi_source_sector_flow WHERE source_date=CAST(? AS DATE) "
        "AND coalesce(is_stale,false)=false",
        [trade_date],
    ).fetchone()[0] or 0) > 0 if table_exists(con, "multi_source_sector_flow") else False
    if theme_rows:
        sector_source = "v_theme_mainline_evidence"
    elif multi_source_live:
        sector_source = "v_theme_mainline_evidence"
    else:
        sector_source = "v_sector_capital"
    order_expr = (
        "coalesce(mainline_score, strength_value, 0) DESC, coalesce(limit_up_count, 0) DESC"
        if sector_source == "v_theme_mainline_evidence"
        else "coalesce(strength_value, 0) DESC, coalesce(limit_up_count, 0) DESC"
    )
    sectors = _fetch_dicts(
        con,
        f"""
        SELECT *
        FROM {sector_source}
        WHERE trade_date = ?
        ORDER BY {order_expr}
        LIMIT 50
        """,
        [trade_date],
    )
    count = 0
    for sector in sectors:
        strength = float(sector.get("strength_value") or 0)
        limit_up = int(sector.get("limit_up_count") or 0)
        seal_rate = float(sector.get("seal_rate") or 0)
        main_net_inflow = sector.get("main_net_inflow")
        mainline_score = sector.get("mainline_score")
        component_count = int(sector.get("component_count") or 0)
        son_plate_count = int(sector.get("son_plate_count") or 0)
        sub_concept_count = int(sector.get("sub_concept_count") or 0)
        capital_component = 0.0
        if main_net_inflow is not None:
            capital_component = max(-8.0, min(10.0, float(main_net_inflow) / 100000000.0))
        diffusion_component = min(8.0, component_count * 0.10 + son_plate_count * 0.45 + sub_concept_count * 0.45)
        catalyst_component = 2.0 if sector.get("boom_reason") else 0.0
        capital_source_table = sector.get("source_table") or (
            "sector_strength" if sector.get("is_fallback") else "sector_capital"
        )
        score_components = {
            "strength_component": strength * 0.42,
            "limit_up_component": min(16.0, limit_up * 0.85),
            "seal_rate_component": seal_rate * 0.08,
            "capital_component": capital_component,
            "diffusion_component": diffusion_component,
            "catalyst_component": catalyst_component,
        }
        if mainline_score is not None:
            score_components["mainline_score_anchor"] = min(4.0, float(mainline_score) * 0.04)
        score = max(0.0, min(100.0, sum(score_components.values())))
        evidence = {
            **sector,
            "score_components": score_components,
            "score_formula": (
                "clamp(0, 100, strength*0.42 + min(16, limit_up_count*0.85) + seal_rate*0.08 "
                "+ capital_component + diffusion_component + catalyst_component "
                "+ capped mainline_score_anchor)"
            ),
            "capital_source": {
                "source_table": capital_source_table,
                "is_fallback": bool(sector.get("is_fallback")),
                "main_net_inflow": main_net_inflow,
            },
            "theme_mainline_evidence": {
                "source_view": sector_source,
                "mainline_score": mainline_score,
                "component_count": component_count,
                "son_plate_count": son_plate_count,
                "sub_concept_count": sub_concept_count,
                "boom_reason": sector.get("boom_reason"),
                "source_tables": sector.get("source_tables"),
            },
            "sample_stats": _sample_stats(
                con,
                sector_source,
                trade_date,
                extra_where="AND sector_code = ?",
                extra_params=[sector.get("sector_code")],
            ),
            "interpretation": (
                "Higher score means stronger sector trend confirmed by sector strength, "
                "limit-up count, seal rate, and capital-flow evidence when available."
            ),
        }
        con.execute(
            """
            INSERT INTO sector_rotation_score
            (trade_date, sector_code, sector_name, taxonomy, score, strength_value,
             limit_up_count, seal_rate, evidence_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade_date,
                sector.get("sector_code"),
                sector.get("sector_name") or "",
                sector.get("sector_type") or (
                    "ths_concept" if str(sector.get("sector_code") or "").startswith("THS-")
                    else "em_industry"
                ),
                score,
                strength,
                limit_up,
                seal_rate,
                json.dumps(evidence, ensure_ascii=False, default=str),
            ],
        )
        count += 1
    return count


def _candidate_gate(item: dict, *, allow_fallback: bool = False, regime_penalty: float = 0.0) -> bool:
    """Require complete same-day evidence before a candidate is executable.

    情绪只降分降仓，不整批否决：regime_penalty 进入 score 与 risk_points，
    不再以 `penalty < 0` 全批 research_only。回滚：恢复下两行即回硬切换。
    """
    if item.get("candidate_pool_status") not in (None, "success"):
        return False
    if not allow_fallback and item.get("candidate_pool_fallback"):
        return False
    return bool(
        item.get("kline_source_table")
        and item.get("sector_score") is not None
        and item.get("auction_source_table")
        and not bool(item.get("kline_is_fallback"))
        and not bool(item.get("auction_is_fallback"))
        and item.get("kline_close") is not None
        and float(item.get("kline_close") or 0) > 0
    )


def _generate_candidates(con: duckdb.DuckDBPyConnection, trade_date: str) -> int:
    candidate_pool_status = "unverified"
    if table_exists(con, "realtime_candidate_pool_snapshot"):
        row = con.execute(
            "SELECT status FROM realtime_candidate_pool_snapshot WHERE trade_date=CAST(? AS DATE) "
            "ORDER BY fetched_at DESC NULLS LAST LIMIT 1",
            [trade_date],
        ).fetchone()
        candidate_pool_status = str(row[0] or "unverified").lower() if row else "unverified"
    kline_relation = canonical_daily_kline_relation(con)
    rows = _fetch_dicts(
        con,
        f"""
        SELECT
            l.trade_date,
            l.stock_code,
            l.stock_name,
            l.board_level,
            s.sector_code,
            sr.score AS sector_score,
            k.source_table AS kline_source_table,
            k.is_fallback AS kline_is_fallback,
            k.change_pct AS kline_change_pct,
            k.close AS kline_close,
            coalesce(a.source_table, ma.source_table) AS auction_source_table,
            coalesce(a.is_fallback, ma.is_fallback) AS auction_is_fallback,
            coalesce(a.confirmation, ma.confirmation) AS auction_confirmation,
            coalesce(a.auction_strength, ma.auction_strength) AS auction_strength
        FROM v_limit_pool l
        LEFT JOIN v_stock_pool s
          ON l.trade_date = s.trade_date AND l.stock_code = s.stock_code
        LEFT JOIN sector_rotation_score sr
          ON l.trade_date = sr.trade_date AND s.sector_code = sr.sector_code
        LEFT JOIN {kline_relation} k
          ON l.trade_date = k.trade_date AND l.stock_code = k.stock_code
        LEFT JOIN v_auction_status a
          ON l.trade_date = a.trade_date AND l.stock_code = a.stock_code
        LEFT JOIN v_auction_status ma
          ON l.trade_date = ma.trade_date AND ma.stock_code IS NULL
        WHERE l.trade_date = ?
        LIMIT 100
        """,
        [trade_date],
    )
    if not rows and table_exists(con, "v_default_concept_stock_history"):
        # A same-day limit-up feed may be unavailable during the session. Use
        # the THS snapshot only to keep a research candidate pool visible;
        # readiness remains blocked because this is not a real limit-up pool.
        rows = _fetch_dicts(
            con,
            f"""
            WITH ranked_members AS (
                SELECT h.trade_date,
                       regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '') AS stock_code,
                       max(h.stock_name) AS stock_name,
                       min(h.concept_rank) AS concept_rank,
                       count(DISTINCT h.concept_code) AS concept_hits
                FROM v_default_concept_stock_history h
                WHERE h.trade_date = ?
                GROUP BY h.trade_date, regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
                ORDER BY concept_hits DESC, concept_rank NULLS LAST, stock_code
                LIMIT 100
            ), prior_kline AS (
                SELECT stock_code, source_table, is_fallback, change_pct, close
                FROM {kline_relation}
                WHERE trade_date = (SELECT max(trade_date) FROM {kline_relation} WHERE trade_date < ?)
                QUALIFY row_number() OVER (PARTITION BY stock_code ORDER BY fetched_at DESC NULLS LAST) = 1
            )
            SELECT r.trade_date, r.stock_code, r.stock_name,
                   1 AS board_level, NULL AS sector_code, NULL AS sector_score,
                   k.source_table AS kline_source_table, k.is_fallback AS kline_is_fallback,
                   k.change_pct AS kline_change_pct, k.close AS kline_close,
                   NULL AS auction_source_table, true AS auction_is_fallback,
                   NULL AS auction_confirmation, NULL AS auction_strength,
                   r.concept_rank, r.concept_hits, true AS candidate_pool_fallback
            FROM ranked_members r
            LEFT JOIN prior_kline k ON r.stock_code = k.stock_code
            """,
            [trade_date, trade_date],
        )
    count = 0
    for item in rows:
        item["candidate_pool_status"] = candidate_pool_status
        board_level = int(item.get("board_level") or 1)
        score = min(100, 45 + board_level * 12)
        evidence = {
            **item,
            "score_components": {
                "base": 45,
                "board_level_component": board_level * 12,
            },
            "entry_reason": (
                "THS concept snapshot fallback candidate; requires same-day confirmation."
                if item.get("candidate_pool_fallback")
                else f"Listed in real-time limit pool with board level {board_level}."
            ),
            "risk_points": [
                "Opening auction data may be incomplete if auction tables are missing.",
                "Limit-up pool candidates can fail quickly in weak or retreating regimes.",
                "Liquidity and gap-open tradability must be checked manually before action.",
            ],
            "invalidation": "Remove from active candidates if sector score weakens, board opens repeatedly, or market regime turns 退潮/冰点.",
        }
        is_actionable = _candidate_gate(item, allow_fallback=False)
        candidate_status = "actionable_candidate" if is_actionable else "research_only"
        evidence["candidate_status"] = candidate_status
        evidence["is_actionable"] = is_actionable
        con.execute(
            """
            INSERT INTO stock_candidate_score
            (trade_date, stock_code, stock_name, score, source, sector_code, evidence_json,
             is_actionable, candidate_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade_date,
                item.get("stock_code"),
                item.get("stock_name"),
                score,
                "ths_snapshot" if item.get("candidate_pool_fallback") else "limit_pool",
                item.get("sector_code"),
                json.dumps(evidence, ensure_ascii=False, default=str),
                is_actionable,
                candidate_status,
            ],
        )
        count += 1
    return count


def _generate_candidates_enriched(con: duckdb.DuckDBPyConnection, trade_date: str) -> int:
    candidate_pool_status = "unverified"
    if table_exists(con, "realtime_candidate_pool_snapshot"):
        row = con.execute(
            "SELECT status FROM realtime_candidate_pool_snapshot WHERE trade_date=CAST(? AS DATE) "
            "ORDER BY fetched_at DESC NULLS LAST LIMIT 1",
            [trade_date],
        ).fetchone()
        candidate_pool_status = str(row[0] or "unverified").lower() if row else "unverified"
    regime_rows = _fetch_dicts(
        con,
        """
        SELECT regime, regime_score, suggested_position_pct, evidence_json
        FROM market_regime_snapshot
        WHERE trade_date = ?
        ORDER BY generated_at DESC NULLS LAST
        LIMIT 1
        """,
        [trade_date],
    )
    regime = regime_rows[0] if regime_rows else {}
    regime_evidence = {}
    if regime.get("evidence_json"):
        try:
            regime_evidence = json.loads(regime["evidence_json"])
        except Exception:
            regime_evidence = {}
    suggested_position = int(regime.get("suggested_position_pct") or 0)
    acute_drop_risk = float(regime_evidence.get("acute_drop_risk_score") or 0)
    regime_penalty = 0.0
    if suggested_position <= 15:
        regime_penalty -= 25.0
    elif suggested_position <= 30:
        regime_penalty -= 10.0
    if acute_drop_risk >= 60:
        regime_penalty -= 12.0
    elif acute_drop_risk >= 45:
        regime_penalty -= 6.0
    kline_relation = canonical_daily_kline_relation(con)
    rows = _fetch_dicts(
        con,
        f"""
        SELECT
            l.trade_date,
            l.stock_code,
            l.stock_name,
            l.board_level,
            s.sector_code,
            sr.score AS sector_score,
            k.source_table AS kline_source_table,
            k.is_fallback AS kline_is_fallback,
            k.change_pct AS kline_change_pct,
            k.close AS kline_close,
            coalesce(a.source_table, ma.source_table) AS auction_source_table,
            coalesce(a.is_fallback, ma.is_fallback) AS auction_is_fallback,
            coalesce(a.confirmation, ma.confirmation) AS auction_confirmation,
            coalesce(a.auction_strength, ma.auction_strength) AS auction_strength
        FROM v_limit_pool l
        LEFT JOIN v_stock_pool s
          ON l.trade_date = s.trade_date AND l.stock_code = s.stock_code
        LEFT JOIN sector_rotation_score sr
          ON l.trade_date = sr.trade_date AND s.sector_code = sr.sector_code
        LEFT JOIN {kline_relation} k
          ON l.trade_date = k.trade_date AND l.stock_code = k.stock_code
             AND (k.ktype = 'D' OR k.ktype IS NULL)
        LEFT JOIN v_auction_status a
          ON l.trade_date = a.trade_date AND l.stock_code = a.stock_code
        LEFT JOIN v_auction_status ma
          ON l.trade_date = ma.trade_date AND ma.stock_code IS NULL
        WHERE l.trade_date = ?
        LIMIT 100
        """,
        [trade_date],
    )
    count = 0
    for item in rows:
        item["candidate_pool_status"] = candidate_pool_status
        board_level = int(item.get("board_level") or 1)
        sector_score = float(item.get("sector_score") or 0)
        kline_change_pct = item.get("kline_change_pct")
        kline_component = 0.0
        if kline_change_pct is not None:
            kline_component = max(-8.0, min(12.0, float(kline_change_pct)))
        score_components = {
            "base": 45,
            "board_level_component": board_level * 12,
            "sector_score_component": sector_score * 0.15,
            "kline_component": kline_component,
            "regime_penalty": regime_penalty,
        }
        score = max(0, min(100, sum(score_components.values())))
        risk_points = [
            "Limit-up pool candidates can fail quickly in weak or retreating regimes.",
            "Liquidity and gap-open tradability must be checked manually before action.",
        ]
        if item.get("auction_is_fallback"):
            risk_points.append("Opening auction evidence is fallback-level, not stock-level anomaly confirmation.")
        if not item.get("kline_source_table"):
            risk_points.append("No usable K-line row joined for this candidate.")
        if regime_penalty < 0:
            risk_points.append("Market regime/risk state reduces candidate priority and position budget.")
        evidence = {
            **item,
            "score_components": score_components,
            "entry_reason": f"Listed in real-time limit pool with board level {board_level}.",
            "risk_points": risk_points,
            "invalidation": "Remove from active candidates if sector score weakens, board opens repeatedly, or market regime turns defensive.",
            "kline_filter": {
                "source_table": item.get("kline_source_table"),
                "is_fallback": bool(item.get("kline_is_fallback")) if item.get("kline_is_fallback") is not None else None,
                "change_pct": kline_change_pct,
                "close": item.get("kline_close"),
            },
            "auction_confirmation": {
                "source_table": item.get("auction_source_table"),
                "is_fallback": bool(item.get("auction_is_fallback")) if item.get("auction_is_fallback") is not None else None,
                "confirmation": item.get("auction_confirmation"),
                "auction_strength": item.get("auction_strength"),
            },
            "market_regime_filter": {
                "regime": regime.get("regime"),
                "regime_score": regime.get("regime_score"),
                "suggested_position_pct": suggested_position,
                "acute_drop_risk_score": acute_drop_risk,
                "regime_penalty": regime_penalty,
            },
            "sample_stats": {
                "candidate_pool": _sample_stats(con, "v_limit_pool", trade_date),
                "stock_kline": _sample_stats(
                    con,
                    "v_kline_daily",
                    trade_date,
                    extra_where="AND stock_code = ?",
                    extra_params=[item.get("stock_code")],
                ),
            },
        }
        is_actionable = _candidate_gate(item, allow_fallback=False, regime_penalty=regime_penalty)
        candidate_status = "actionable_candidate" if is_actionable else "research_only"
        evidence["candidate_status"] = candidate_status
        evidence["is_actionable"] = is_actionable
        con.execute(
            """
            INSERT INTO stock_candidate_score
            (trade_date, stock_code, stock_name, score, source, sector_code, evidence_json,
             is_actionable, candidate_status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                trade_date,
                item.get("stock_code"),
                item.get("stock_name"),
                score,
                "limit_pool",
                item.get("sector_code"),
                json.dumps(evidence, ensure_ascii=False, default=str),
                is_actionable,
                candidate_status,
            ],
        )
        count += 1
    return count


def _generate_alerts_enriched(
    con: duckdb.DuckDBPyConnection,
    trade_date: str,
    regime: dict,
    sector_count: int,
) -> int:
    alerts = []
    if int(regime.get("suggested_position_pct") or 0) <= 15:
        alerts.append(("P0", "market_regime", f"Market regime is {regime['regime']}; reduce exposure."))
    if int(regime.get("regime_score") or 0) >= 85:
        alerts.append(("P1", "market_regime", "Market is overheated; lock profit and avoid fresh chase entries."))
    if sector_count == 0:
        alerts.append(("P1", "data_quality", "No sector score rows generated for this date."))
    if not _relation_has_rows(con, "auction_bidding_anomaly"):
        alerts.append(("P2", "data_gap", "Auction anomaly table is empty or missing; opening auction signals are incomplete."))
    if _relation_has_rows(con, "v_sector_capital"):
        fallback_count = con.execute(
            "SELECT count(*) FROM v_sector_capital WHERE trade_date = ? AND is_fallback = true",
            [trade_date],
        ).fetchone()[0]
        if fallback_count:
            alerts.append(("P2", "data_gap", f"Sector capital flow is fallback for {fallback_count} sectors."))
    if _relation_has_rows(con, "v_index_state"):
        index_fallback = con.execute(
            "SELECT count(*) FROM v_index_state WHERE trade_date = ? AND is_fallback = true",
            [trade_date],
        ).fetchone()[0]
        if index_fallback:
            alerts.append(("P2", "data_gap", "Index state uses market breadth fallback; index tables are empty or missing."))
    risk_row = con.execute(
        "SELECT acute_drop_risk_score FROM v_market_state_inputs WHERE trade_date = ?",
        [trade_date],
    ).fetchone()
    if risk_row and risk_row[0] is not None and risk_row[0] >= 45:
        alerts.append(("P1", "risk", f"Acute drop risk score is {risk_row[0]:.1f}; tighten intraday risk limits."))

    for severity, category, message in alerts:
        con.execute(
            """
            INSERT INTO alert_events (trade_date, severity, category, message, evidence_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            [trade_date, severity, category, message, json.dumps(regime["evidence"], ensure_ascii=False, default=str)],
        )
    return len(alerts)


def generate_signals(
    db_path: str | Path,
    trade_date: str | None = None,
    *,
    require_ready: bool = True,
    readiness_stage: str = "close",
) -> dict:
    build_normalized_views(db_path)
    try:
        return _generate_signals_once(
            db_path,
            trade_date,
            require_ready=require_ready,
            readiness_stage=readiness_stage,
        )
    except duckdb.Error as exc:
        # A known DuckDB failure mode is a stale/corrupt index catalog entry
        # reporting that fewer rows were deleted than expected.  Rebuild only
        # the small signal indexes and retry once.  The retry is bounded and the
        # generation itself remains atomic, so a second failure cannot publish
        # a partial same-day snapshot.
        message = str(exc)
        if not (
            "Failed to delete all rows from index" in message
            or isinstance(exc, duckdb.ConstraintException)
        ):
            raise
        ensure_unique_indexes(db_path)
        return _generate_signals_once(
            db_path,
            trade_date,
            require_ready=require_ready,
            readiness_stage=readiness_stage,
        )


def _generate_signals_once(
    db_path: str | Path,
    trade_date: str | None,
    *,
    require_ready: bool,
    readiness_stage: str,
) -> dict:
    con = duckdb.connect(str(db_path))
    in_transaction = False
    indexes_dropped = False
    try:
        _ensure_signal_tables(con)
        selected_date = trade_date or _latest_trade_date(con)
        if not selected_date:
            raise ValueError("No trade_date available from v_market_daily")
        readiness = assess_trade_date_readiness(con, selected_date, readiness_stage)
        if require_ready and not readiness.get(
            "data_certified_ready",
            readiness.get("source_ready", readiness["ready"]),
        ):
            missing = ", ".join(readiness["missing_groups"])
            raise ValueError(
                f"Trade date {selected_date} is not actionable for {readiness_stage}; "
                f"missing or stale groups: {missing}"
            )
        _drop_signal_indexes(con)
        indexes_dropped = True
        # A signal date is one replaceable snapshot.  Do not expose a mixture
        # of old and new rows if any sector/candidate calculation fails.
        con.execute("BEGIN TRANSACTION")
        in_transaction = True
        _clear_signal_date(con, selected_date)
        regime = _generate_regime(con, selected_date)
        sector_count = _generate_sectors(con, selected_date)
        candidate_count = _generate_candidates_enriched(con, selected_date)
        if candidate_count == 0:
            candidate_count = _generate_candidates(con, selected_date)
        # Stage signals are generated separately at their real decision cutoffs.
        # Keeping this context pass stage-free prevents post-close data from
        # rewriting premarket, auction, or intraday evidence.
        stage_candidate_count = 0
        alert_count = _generate_alerts_enriched(con, selected_date, regime, sector_count)
        con.execute("COMMIT")
        in_transaction = False
        _create_signal_indexes(con)
        indexes_dropped = False
        return {
            "trade_date": selected_date,
            "regime": regime["regime"],
            "suggested_position_pct": regime["suggested_position_pct"],
            "sector_count": sector_count,
            "candidate_count": candidate_count,
            "stage_candidate_count": stage_candidate_count,
            "alert_count": alert_count,
            "readiness": readiness,
        }
    except Exception:
        if in_transaction:
            try:
                con.execute("ROLLBACK")
            except Exception:
                pass
        if indexes_dropped:
            try:
                _create_signal_indexes(con)
            except Exception:
                # Preserve the original generation failure.  The next
                # integrity pass will make a missing-index condition visible.
                pass
        raise
    finally:
        con.close()
