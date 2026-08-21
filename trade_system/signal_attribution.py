"""Attribute stage-signal outcomes to market phases.

For every historical stage signal, compute the executable next-session
outcome (open->close) and the close-to-close reference outcome, then
aggregate win rate / average return per (stage, phase, horizon).  This is
the evidence layer that later lets operators prune weak signal layers or
condition them on the emotion cycle.
"""
from __future__ import annotations

import duckdb

KLINE_DEDUP_CTE = """
SELECT CAST(trade_date AS DATE) AS d, stock_code, open, close FROM (
    SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, open, close,
           row_number() OVER (
               PARTITION BY trade_date, stock_code
               ORDER BY is_fallback ASC, fetched_at DESC
           ) AS _rn
    FROM v_kline_daily
    WHERE ktype='D'
) WHERE _rn = 1
"""


def compute_stage_attribution(
    con: duckdb.DuckDBPyConnection,
    *,
    require_triggered: bool = True,
    require_data_complete: bool = False,
) -> list[dict]:
    """Return aggregated attribution rows and replace the stored table."""
    filters = ["s.stage IS NOT NULL"]
    if require_triggered:
        filters.append("s.signal_triggered = true")
    else:
        filters.append("s.is_actionable = true")
    if require_data_complete:
        filters.append("s.data_complete = true")

    rows = con.execute(
        f"""
        WITH k AS ({KLINE_DEDUP_CTE}),
        sessions AS (SELECT DISTINCT d FROM k),
        next_map AS (
            SELECT a.d AS cur, b.d AS nxt
            FROM (SELECT d, row_number() OVER (ORDER BY d) AS i FROM sessions) a
            JOIN (SELECT d, row_number() OVER (ORDER BY d) AS i FROM sessions) b
              ON b.i = a.i + 1
        ),
        sig AS (
            SELECT CAST(s.trade_date AS DATE) AS d, s.stage, s.stock_code,
                   s.score
            FROM stock_candidate_stage_signal s
            WHERE {' AND '.join(filters)}
        ),
        outcomes AS (
            SELECT sig.stage,
                   COALESCE(c.phase, 'unclassified') AS phase,
                   (k1.close - k1.open) / NULLIF(k1.open, 0) * 100.0 AS ret_oc,
                   (k1.close - k0.close) / NULLIF(k0.close, 0) * 100.0 AS ret_cc,
                   sig.stock_code
            FROM sig
            LEFT JOIN market_cycle_phase c ON c.trade_date = sig.d
            JOIN next_map nm ON nm.cur = sig.d
            JOIN k k0 ON k0.stock_code = sig.stock_code AND k0.d = sig.d
            JOIN k k1 ON k1.stock_code = sig.stock_code AND k1.d = nm.nxt
        )
        SELECT stage, phase,
               count(*) AS n_oc,
               avg(CASE WHEN ret_oc > 0 THEN 1.0 ELSE 0.0 END) AS wr_oc,
               avg(ret_oc) AS avg_oc,
               median(ret_oc) AS med_oc
        FROM outcomes
        GROUP BY stage, phase
        ORDER BY stage, phase
        """
    ).fetchall()

    oc_rows = [
        {
            "stage": r[0], "phase": r[1], "horizon": "oc_next",
            "n_signals": int(r[2]),
            "win_rate": round(float(r[3]), 4) if r[3] is not None else None,
            "avg_ret_pct": round(float(r[4]), 4) if r[4] is not None else None,
            "median_ret_pct": round(float(r[5]), 4) if r[5] is not None else None,
        }
        for r in rows
    ]
    cc_rows = con.execute(
        f"""
        WITH k AS ({KLINE_DEDUP_CTE}),
        sessions AS (SELECT DISTINCT d FROM k),
        next_map AS (
            SELECT a.d AS cur, b.d AS nxt
            FROM (SELECT d, row_number() OVER (ORDER BY d) AS i FROM sessions) a
            JOIN (SELECT d, row_number() OVER (ORDER BY d) AS i FROM sessions) b
              ON b.i = a.i + 1
        ),
        sig AS (
            SELECT CAST(s.trade_date AS DATE) AS d, s.stage, s.stock_code
            FROM stock_candidate_stage_signal s
            WHERE {' AND '.join(filters)}
        ),
        outcomes AS (
            SELECT sig.stage,
                   COALESCE(c.phase, 'unclassified') AS phase,
                   (k1.close - k0.close) / NULLIF(k0.close, 0) * 100.0 AS ret_cc
            FROM sig
            LEFT JOIN market_cycle_phase c ON c.trade_date = sig.d
            JOIN next_map nm ON nm.cur = sig.d
            JOIN k k0 ON k0.stock_code = sig.stock_code AND k0.d = sig.d
            JOIN k k1 ON k1.stock_code = sig.stock_code AND k1.d = nm.nxt
        )
        SELECT stage, phase, count(*),
               avg(CASE WHEN ret_cc > 0 THEN 1.0 ELSE 0.0 END),
               avg(ret_cc), median(ret_cc)
        FROM outcomes GROUP BY stage, phase ORDER BY stage, phase
        """
    ).fetchall()
    oc_rows += [
        {
            "stage": r[0], "phase": r[1], "horizon": "cc_next",
            "n_signals": int(r[2]),
            "win_rate": round(float(r[3]), 4) if r[3] is not None else None,
            "avg_ret_pct": round(float(r[4]), 4) if r[4] is not None else None,
            "median_ret_pct": round(float(r[5]), 4) if r[5] is not None else None,
        }
        for r in cc_rows
    ]

    as_of = con.execute(
        "SELECT max(CAST(trade_date AS DATE)) FROM stock_candidate_stage_signal"
    ).fetchone()[0]
    for row in oc_rows:
        row["as_of"] = str(as_of) if as_of else None

    con.execute("BEGIN TRANSACTION")
    try:
        con.execute("DELETE FROM signal_stage_attribution")
        for row in oc_rows:
            con.execute(
                """INSERT INTO signal_stage_attribution
                   (stage, phase, horizon, n_signals, win_rate, avg_ret_pct,
                    median_ret_pct, as_of)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                [row["stage"], row["phase"], row["horizon"], row["n_signals"],
                 row["win_rate"], row["avg_ret_pct"], row["median_ret_pct"],
                 row["as_of"]],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return oc_rows


# Advisory mapping only: the operator stays responsible for applying caps.
PHASE_POSITION_CAP_PCT = {
    "ice": 0,
    "retreat": 20,
    "divergence": 40,
    "recovery": 60,
    "ferment": 80,
    "climax": 100,
}


def latest_phase(con: duckdb.DuckDBPyConnection) -> dict | None:
    row = con.execute(
        """SELECT trade_date, phase, score, rationale FROM market_cycle_phase
           ORDER BY trade_date DESC LIMIT 1"""
    ).fetchone()
    if not row:
        return None
    return {
        "trade_date": str(row[0]), "phase": row[1],
        "score": float(row[2]) if row[2] is not None else None,
        "rationale": row[3],
        "advisory_position_cap_pct": PHASE_POSITION_CAP_PCT.get(row[1]),
    }
