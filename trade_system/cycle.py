"""Emotion-cycle phase engine and limit-up premium analytics.

All functions are read-only over the normalized views; derived rows land in
the ``market_cycle_phase`` / ``limit_premium_matrix`` /
``promotion_rate_matrix`` tables (migration 0002).

The v1 classifier is deliberately rule-based and explainable: every output
row carries its ``rationale``.  Constants below are tuning knobs, not laws.
"""
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import duckdb

_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "phase_thresholds.json"


def _load_thresholds() -> dict:
    """Load tunable thresholds from config/phase_thresholds.json (fallback: defaults)."""
    defaults = {
        "ice_limit_up": 30, "ice_limit_up_soft": 45,
        "ice_premium_pct": -2.0,
        "climax_premium_pct": 3.0, "climax_min_board": 5,
        "climax_max_blown_rate": 25.0,
        "retreat_premium_pct": -1.5, "retreat_blown_rate": 35.0,
        "retreat_max_promotion": 0.20,
        "ferment_premium_pct": 1.0, "ferment_min_board": 4,
    }
    try:
        overrides = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        defaults.update({k: v for k, v in overrides.items() if not k.startswith("_")})
    except Exception:
        pass
    return defaults


_T = _load_thresholds()

# ---------------------------------------------------------------- constants
# Thresholds are loaded from config/phase_thresholds.json (see _T above).
ICE_LIMIT_UP = _T["ice_limit_up"]
ICE_LIMIT_UP_SOFT = _T["ice_limit_up_soft"]
ICE_PREMIUM_PCT = _T["ice_premium_pct"]
CLIMAX_PREMIUM_PCT = _T["climax_premium_pct"]
CLIMAX_MIN_BOARD = _T["climax_min_board"]
CLIMAX_MAX_BLOWN_RATE = _T["climax_max_blown_rate"]
RETREAT_PREMIUM_PCT = _T["retreat_premium_pct"]
RETREAT_BLOWN_RATE = _T["retreat_blown_rate"]
RETREAT_MAX_PROMOTION = _T["retreat_max_promotion"]
FERMENT_PREMIUM_PCT = _T["ferment_premium_pct"]
FERMENT_MIN_BOARD = _T["ferment_min_board"]

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
    WHERE ktype = 'D' AND change_pct IS NOT NULL AND isfinite(change_pct)
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
    """Use the exchange calendar; missing bars never move the target session."""
    from trade_system.trading_calendar import open_session_dates
    day = date.fromisoformat(trade_date).isoformat()
    candidates = sessions if sessions is not None else open_session_dates(con, day, "9999-12-31")
    later = sorted(d for d in candidates if d > day)
    if not later:
        return None
    try:
        verified = open_session_dates(con, day, later[0], strict=True)
    except ValueError:
        return None
    return later[0] if verified == [day, later[0]] else None


def _pool_sql(trade_date: str) -> str:
    """Observed pool only; historical threshold estimates are not pool facts."""
    day = date.fromisoformat(trade_date).isoformat()
    return f"""
        SELECT stock_code, max(board_level) AS board
        FROM v_limit_pool WHERE CAST(trade_date AS DATE) = DATE '{day}'
        GROUP BY stock_code
        HAVING max(board_level) >= 1
    """


def compute_premium(con: duckdb.DuckDBPyConnection, prev_trade_date: str, *,
                    kline_src: str | None = None,
                    sessions: list[str] | None = None) -> list[dict]:
    """Exact next-session outcomes; incomplete cohorts keep unknown statistics."""
    nxt = next_session(con, prev_trade_date, sessions)
    if not nxt:
        return []
    rows = con.execute(
        f"""
        WITH pool AS ({_pool_sql(prev_trade_date)}),
        kline AS ({_kline_relation(kline_src)}),
        cohort AS (
            SELECT CASE WHEN board >= 4 THEN '4+' ELSE CAST(board AS VARCHAR) END AS bucket,
                   k.change_pct
            FROM pool p LEFT JOIN kline k ON k.stock_code = p.stock_code
                       AND k.trade_date = DATE '{nxt}'
        )
        SELECT CASE WHEN grouping(bucket)=1 THEN '_all' ELSE bucket END,
               count(*), count(change_pct), avg(change_pct), median(change_pct),
               avg(CASE WHEN change_pct IS NULL THEN NULL
                        WHEN change_pct > 0 THEN 1.0 ELSE 0.0 END)
        FROM cohort GROUP BY GROUPING SETS ((bucket), ())
        """
    ).fetchall()
    return [{"prev_trade_date": prev_trade_date, "board_bucket": bucket,
             "sample_size": int(n), "observed_count": int(observed),
             "missing_count": int(n-observed),
             "avg_pct": round(float(avg_pct), 4) if observed == n else None,
             "median_pct": round(float(med_pct), 4) if observed == n else None,
             "win_rate": round(float(win), 4) if observed == n else None}
            for bucket, n, observed, avg_pct, med_pct, win in rows if n]


def compute_promotion(con: duckdb.DuckDBPyConnection, trade_date: str, *,
                      kline_src: str | None = None,
                      sessions: list[str] | None = None) -> list[dict]:
    """Of stocks at board b yesterday, how many reached b+1 today."""
    nxt = next_session(con, trade_date, sessions)
    if not nxt:
        return []
    if not limit_pool_by_day(con, nxt):
        return []  # Empty without coverage proof is unknown, not zero promotion.
    y_pool = _pool_sql(trade_date)
    t_pool = _pool_sql(nxt)
    rows = con.execute(
        f"""
        WITH y AS ({y_pool}),
        t AS ({t_pool})
        SELECT y.board, count(*) AS candidates,
               count(t.stock_code) AS promoted
        FROM y LEFT JOIN t ON t.stock_code = y.stock_code AND t.board = y.board + 1
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
