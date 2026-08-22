"""Emotion-cycle phase engine and limit-up premium analytics.

All functions are read-only over the normalized views; derived rows land in
the ``market_cycle_phase`` / ``limit_premium_matrix`` /
``promotion_rate_matrix`` tables (migration 0002).

The v1 classifier is deliberately rule-based and explainable: every output
row carries its ``rationale``.  Constants below are tuning knobs, not laws.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import duckdb

# ---------------------------------------------------------------- constants
ICE_LIMIT_UP = 30
ICE_LIMIT_UP_SOFT = 45
ICE_PREMIUM_PCT = -2.0
CLIMAX_PREMIUM_PCT = 3.0
CLIMAX_MIN_BOARD = 5
CLIMAX_MAX_BLOWN_RATE = 25.0
RETREAT_PREMIUM_PCT = -1.5
RETREAT_BLOWN_RATE = 35.0
RETREAT_MAX_PROMOTION = 0.20
FERMENT_PREMIUM_PCT = 1.0
FERMENT_MIN_BOARD = 4

PHASES = ("ice", "recovery", "ferment", "climax", "divergence", "retreat")
PHASE_CN = {
    "ice": "冰点",
    "recovery": "回暖",
    "ferment": "发酵",
    "climax": "高潮",
    "divergence": "分歧",
    "retreat": "退潮",
}

KLINE_DEDUP_CTE = """
SELECT trade_date, stock_code, change_pct FROM (
    SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, change_pct,
           row_number() OVER (
               PARTITION BY trade_date, stock_code
               ORDER BY is_fallback ASC, fetched_at DESC
           ) AS _rn
    FROM v_kline_daily
    WHERE ktype = 'D' AND change_pct IS NOT NULL
) WHERE _rn = 1
"""


def _kline_relation(kline_src: str | None) -> str:
    """CTE body for the deduplicated kline; a bare table name gets wrapped."""
    src = kline_src or KLINE_DEDUP_CTE
    if " " not in src.strip():
        return f"SELECT trade_date, stock_code, change_pct FROM {src}"
    return src


@dataclass
class DayMetrics:
    trade_date: str
    limit_up_count: int | None = None
    limit_down_count: int | None = None
    blown_rate: float | None = None
    max_board: int | None = None
    premium_pct: float | None = None
    promotion_rate: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)


def classify_phase(m: DayMetrics) -> tuple[str, float, str]:
    """Return (phase, score 0..100, rationale) using documented v1 rules."""
    lu = m.limit_up_count
    prem = m.premium_pct
    promo = m.promotion_rate
    board = m.max_board or 0
    blown = m.blown_rate

    reasons: list[str] = []
    if lu is not None and lu <= ICE_LIMIT_UP:
        return ("ice", _score(m), f"limit_up={lu}<= {ICE_LIMIT_UP}")
    if lu is not None and lu <= ICE_LIMIT_UP_SOFT and prem is not None \
            and prem <= ICE_PREMIUM_PCT:
        return ("ice", _score(m),
                f"limit_up={lu}<={ICE_LIMIT_UP_SOFT} and premium={prem:.2f}%")

    if prem is not None:
        reasons.append(f"premium={prem:.2f}%")
    if prem is not None and prem >= CLIMAX_PREMIUM_PCT and board >= CLIMAX_MIN_BOARD \
            and (blown is None or blown <= CLIMAX_MAX_BLOWN_RATE):
        return ("climax", _score(m),
                f"premium>={CLIMAX_PREMIUM_PCT}% & max_board={board}"
                + (f" & blown={blown:.0f}%" if blown is not None else ""))

    if prem is not None and prem <= RETREAT_PREMIUM_PCT:
        return ("retreat", _score(m), f"premium<={RETREAT_PREMIUM_PCT}%")
    if blown is not None and blown > RETREAT_BLOWN_RATE \
            and (promo is not None and promo < RETREAT_MAX_PROMOTION):
        return ("retreat", _score(m),
                f"blown={blown:.0f}%>{RETREAT_BLOWN_RATE}% & promo<{RETREAT_MAX_PROMOTION:.0%}")

    if prem is not None and prem >= FERMENT_PREMIUM_PCT and board >= FERMENT_MIN_BOARD:
        return ("ferment", _score(m),
                f"premium>={FERMENT_PREMIUM_PCT}% & max_board={board}")

    if prem is not None and prem >= 0:
        return ("recovery", _score(m), "premium>=0 baseline")

    if prem is not None:
        return ("divergence", _score(m), "mixed signals")
    if lu is not None:
        return ("divergence", _score(m), f"kline premium unavailable; limit_up={lu}")
    return ("divergence", 50.0, "insufficient data")


def _score(m: DayMetrics) -> float:
    """Heuristic temperature 0..100 for charting; missing inputs default neutral."""
    prem = m.premium_pct if m.premium_pct is not None else 0.0
    promo = m.promotion_rate if m.promotion_rate is not None else 0.25
    board = m.max_board if m.max_board is not None else 3
    blown = m.blown_rate if m.blown_rate is not None else 20.0
    raw = 50.0 + prem * 10.0 + (promo - 0.25) * 100.0 * 0.3 + (board - 3) * 5.0 \
        - (blown - 20.0) * 0.5
    return round(max(0.0, min(100.0, raw)), 1)


# ------------------------------------------------------------ aggregations
def limit_pool_by_day(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict]:
    return [
        {"stock_code": r[0], "board_level": int(r[1])}
        for r in con.execute(
            """
            SELECT stock_code, max(board_level)
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) = ?
            GROUP BY stock_code
            """,
            [trade_date],
        ).fetchall()
    ]


def next_session(con: duckdb.DuckDBPyConnection, trade_date: str,
                 sessions: list[str] | None = None) -> str | None:
    if sessions:
        later = [d for d in sessions if d > trade_date]
        return later[0] if later else None
    rows = con.execute(
        """
        SELECT min(CAST(trade_date AS DATE)) FROM v_kline_daily
        WHERE ktype='D' AND CAST(trade_date AS DATE) > ?
        """,
        [trade_date],
    ).fetchone()
    return str(rows[0]) if rows and rows[0] else None


def _pool_with_fallback(con: duckdb.DuckDBPyConnection, trade_date: str,
                        table_exists_fn) -> str:
    """SQL fragment: real limit pool for the day, derived pool filling gaps.

    Returns a ``pool(stock_code, board)`` select body; callers wrap it in a
    CTE.  The derived table only contributes codes the real pool lacks.
    """
    has_derived = table_exists_fn(con, "derived_limit_up_daily")
    if not has_derived:
        return f"""
            SELECT stock_code, max(board_level) AS board
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) = {trade_date}
            GROUP BY stock_code
        """
    return f"""
        WITH real_pool AS (
            SELECT stock_code, max(board_level) AS board
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) = {trade_date}
            GROUP BY stock_code
        ),
        derived_only AS (
            SELECT d.stock_code, d.board_level AS board
            FROM derived_limit_up_daily d
            WHERE d.trade_date = {trade_date}
              AND d.stock_code NOT IN (SELECT stock_code FROM real_pool)
        )
        SELECT stock_code, board FROM real_pool
        UNION ALL
        SELECT stock_code, board FROM derived_only
    """


def compute_premium(con: duckdb.DuckDBPyConnection, prev_trade_date: str, *,
                    kline_src: str | None = None,
                    sessions: list[str] | None = None) -> list[dict]:
    """Yesterday's limit-up cohort vs today's change_pct, bucketed by height."""
    from trade_system.quality import table_exists as _te

    nxt = next_session(con, prev_trade_date, sessions)
    if not nxt:
        return []
    pool_body = _pool_with_fallback_quoted(con, prev_trade_date, _te)
    rows = con.execute(
        f"""
        WITH pool AS ({pool_body}),
        kline AS ({_kline_relation(kline_src)})
        SELECT
            CASE WHEN board >= 4 THEN '4+' ELSE CAST(board AS VARCHAR) END AS bucket,
            count(*) AS n,
            avg(k.change_pct) AS avg_pct,
            median(k.change_pct) AS med_pct,
            avg(CASE WHEN k.change_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate
        FROM pool p JOIN kline k ON k.stock_code = p.stock_code
                   AND k.trade_date = DATE '{nxt}'
        GROUP BY 1
        """,
    ).fetchall()
    out = []
    for bucket, n, avg_pct, med_pct, win in rows:
        out.append({
            "prev_trade_date": prev_trade_date,
            "board_bucket": bucket,
            "sample_size": int(n),
            "avg_pct": round(float(avg_pct), 4) if avg_pct is not None else None,
            "median_pct": round(float(med_pct), 4) if med_pct is not None else None,
            "win_rate": round(float(win), 4) if win is not None else None,
        })
    # overall row (all boards pooled)
    overall = con.execute(
        f"""
        WITH pool AS ({pool_body}),
        kline AS ({_kline_relation(kline_src)})
        SELECT count(*), avg(k.change_pct), median(k.change_pct),
               avg(CASE WHEN k.change_pct > 0 THEN 1.0 ELSE 0.0 END)
        FROM pool p JOIN kline k ON k.stock_code = p.stock_code
                   AND k.trade_date = DATE '{nxt}'
        """,
    ).fetchone()
    if overall and overall[0]:
        n, avg_pct, med_pct, win = overall
        out.append({
            "prev_trade_date": prev_trade_date,
            "board_bucket": "_all",
            "sample_size": int(n),
            "avg_pct": round(float(avg_pct), 4) if avg_pct is not None else None,
            "median_pct": round(float(med_pct), 4) if med_pct is not None else None,
            "win_rate": round(float(win), 4) if win is not None else None,
        })
    return out


def _pool_with_fallback_quoted(con: duckdb.DuckDBPyConnection, trade_date: str,
                               table_exists_fn) -> str:
    """Like :func:`_pool_with_fallback` but with the date inlined as a literal."""
    has_derived = table_exists_fn(con, "derived_limit_up_daily")
    lit = f"DATE '{trade_date}'"
    if not has_derived:
        return f"""
            SELECT stock_code, max(board_level) AS board
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) = {lit}
            GROUP BY stock_code
        """
    return f"""
        WITH real_pool AS (
            SELECT stock_code, max(board_level) AS board
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) = {lit}
            GROUP BY stock_code
        ),
        derived_only AS (
            SELECT d.stock_code, d.board_level AS board
            FROM derived_limit_up_daily d
            WHERE d.trade_date = {lit}
              AND d.stock_code NOT IN (SELECT stock_code FROM real_pool)
        )
        SELECT stock_code, board FROM real_pool
        UNION ALL
        SELECT stock_code, board FROM derived_only
    """


def compute_promotion(con: duckdb.DuckDBPyConnection, trade_date: str, *,
                      kline_src: str | None = None,
                      sessions: list[str] | None = None) -> list[dict]:
    """Of stocks at board b yesterday, how many reached b+1 today."""
    from trade_system.quality import table_exists as _te

    nxt = next_session(con, trade_date, sessions)
    if not nxt:
        return []
    y_pool = _pool_with_fallback_quoted(con, trade_date, _te)
    t_pool = _pool_with_fallback_quoted(con, nxt, _te)
    rows = con.execute(
        f"""
        WITH y AS ({y_pool}),
        t AS ({t_pool})
        SELECT y.board, count(*) AS candidates,
               count(t.stock_code) AS promoted
        FROM y LEFT JOIN t ON t.stock_code = y.stock_code
        GROUP BY y.board ORDER BY y.board
        """
    ).fetchall()
    return [
        {
            "trade_date": trade_date,
            "from_board": int(board),
            "candidates": int(cand),
            "promoted": int(prom),
            "rate": round(prom / cand, 4) if cand else None,
        }
        for board, cand, prom in rows
    ]
