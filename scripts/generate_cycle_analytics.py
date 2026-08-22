"""Build the emotion-cycle phase table, premium and promotion matrices.

Read-only over normalized views; writes three derived tables (migration 0002)
and a markdown summary.  Safe to re-run: rows are replaced per date.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.cycle import PHASE_CN, DayMetrics, KLINE_DEDUP_CTE, \
    classify_phase, compute_premium, compute_promotion  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402

logger = get_logger("cycle_analytics")


def _trading_days(con) -> list[str]:
    rows = con.execute(
        """
        SELECT d FROM (
            SELECT DISTINCT CAST(trade_date AS DATE) AS d FROM v_limit_pool
            UNION
            SELECT DISTINCT trade_date AS d FROM derived_limit_up_daily
        ) ORDER BY d
        """
    ).fetchall()
    return [str(r[0]) for r in rows]


def build(con, dates: list[str], min_sample: int = 3) -> dict:
    stats = {"phase": 0, "premium": 0, "promotion": 0}
    # Materialize the deduplicated kline panel once for the whole loop;
    # re-running the dedup CTE per day does not finish within minutes.
    con.execute(
        f"CREATE OR REPLACE TEMP TABLE _kline_dedup AS {KLINE_DEDUP_CTE}"
    )
    kline_src = "_kline_dedup"
    for day in dates:
        mood = con.execute(
            """SELECT limit_up_count, limit_down_count FROM market_mood
               WHERE date = ? ORDER BY source_kind DESC LIMIT 1""",
            [day],
        ).fetchone()
        blown_row = con.execute(
            "SELECT blown_limit_up_rate FROM market_limit_up_down_summary WHERE date = ?",
            [day],
        ).fetchone()
        max_board = con.execute(
            "SELECT max(board_level) FROM v_limit_pool WHERE CAST(trade_date AS DATE) = ?",
            [day],
        ).fetchone()[0]

        prem_rows = compute_premium(con, day, kline_src=kline_src)
        promo_rows = compute_promotion(con, day, kline_src=kline_src)

        overall_prem = next(
            (r["avg_pct"] for r in prem_rows if r["board_bucket"] == "_all"), None
        )
        first_board_promo = next(
            (r["rate"] for r in promo_rows if r["from_board"] == 1), None
        )

        # Days with neither mood data nor measurable cohort outcomes carry no
        # signal; writing a placeholder phase would pollute the time series.
        if mood is None and overall_prem is None:
            logger.debug("skip %s: no mood and no premium evidence", day)
            continue

        metrics = DayMetrics(
            trade_date=day,
            limit_up_count=mood[0] if mood else None,
            limit_down_count=mood[1] if mood else None,
            blown_rate=float(blown_row[0]) if blown_row and blown_row[0] is not None else None,
            max_board=int(max_board) if max_board is not None else None,
            premium_pct=overall_prem,
            promotion_rate=first_board_promo,
        )
        phase, score, rationale = classify_phase(metrics)

        # Replace-per-date writes keep reruns idempotent.
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(
                "DELETE FROM market_cycle_phase WHERE trade_date = ?", [day]
            )
            con.execute(
                """INSERT INTO market_cycle_phase
                   (trade_date, phase, limit_up_count, limit_down_count, blown_rate,
                    max_board, premium_pct, promotion_rate, score, rationale)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                [day, phase, metrics.limit_up_count, metrics.limit_down_count,
                 metrics.blown_rate, metrics.max_board, metrics.premium_pct,
                 metrics.promotion_rate, score, rationale],
            )
            con.execute(
                "DELETE FROM limit_premium_matrix WHERE prev_trade_date = ?", [day]
            )
            for r in prem_rows:
                if r["sample_size"] < min_sample and r["board_bucket"] != "_all":
                    continue
                con.execute(
                    """INSERT INTO limit_premium_matrix
                       (prev_trade_date, board_bucket, sample_size, avg_pct,
                        median_pct, win_rate) VALUES (?, ?, ?, ?, ?, ?)""",
                    [r["prev_trade_date"], r["board_bucket"], r["sample_size"],
                     r["avg_pct"], r["median_pct"], r["win_rate"]],
                )
                stats["premium"] += 1
            con.execute(
                "DELETE FROM promotion_rate_matrix WHERE trade_date = ?", [day]
            )
            for r in promo_rows:
                con.execute(
                    """INSERT INTO promotion_rate_matrix
                       (trade_date, from_board, candidates, promoted, rate)
                       VALUES (?, ?, ?, ?, ?)""",
                    [r["trade_date"], r["from_board"], r["candidates"],
                     r["promoted"], r["rate"]],
                )
                stats["promotion"] += 1
            con.execute("COMMIT")
            stats["phase"] += 1
        except Exception:
            con.execute("ROLLBACK")
            raise
    return stats


def render_report(con, out_path: Path) -> None:
    recent = con.execute(
        """SELECT trade_date, phase, limit_up_count, max_board, premium_pct,
                  promotion_rate, score, rationale
           FROM market_cycle_phase ORDER BY trade_date DESC LIMIT 10"""
    ).fetchall()
    lines = [
        "# Emotion cycle phases",
        "",
        f"Generated {Path(out_path).name}; rule-based v1 classifier.",
        "",
        "| date | phase | 涨停 | 最高板 | 昨涨停溢价% | 首板晋级率 | 温度 | 依据 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for row in reversed(recent):
        d, phase, lu, board, prem, promo, score, why = row
        lines.append(
            f"| {d} | {PHASE_CN.get(phase, phase)} ({phase}) | {lu} | {board} "
            f"| {prem if prem is not None else '—'} "
            f"| {f'{promo:.0%}' if promo is not None else '—'} "
            f"| {score} | {why} |"
        )
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "cycle_phase_latest.md"))
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db)
    try:
        dates = _trading_days(con)
        stats = build(con, dates)
        logger.info("cycle analytics built: %s", stats)
        render_report(con, Path(args.out))
        print(f"cycle analytics: {stats}")
        print(f"report: {args.out}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
