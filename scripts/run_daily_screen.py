"""Run the full daily next-day screening pipeline.

Stages: gather evidence -> rule scoring (phase-aware) -> persist ->
optional DeepSeek narrative review for the Top N -> markdown report.

Research-only output; it does not touch the operator plan.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.db_utils import fetch_dicts as _fetch_dicts  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402
from trade_system.stock_screener import (  # noqa: E402
    build_llm_prompt,
    parse_llm_annotations,
    score_candidates,
)

logger = get_logger("daily_screen")


def gather(con: duckdb.DuckDBPyConnection, trade_date: str) -> tuple[list[dict], str]:
    """Candidate rows + current phase."""
    pool = _fetch_dicts(
        con,
        """
        SELECT stock_code, stock_name, limit_up_time, continue_day_cnt AS board,
               limit_up_reason, close, pct_chg, seal_money, max_seal_money
        FROM official_limit_pool
        WHERE trade_date = ?
        """,
        [trade_date],
    )
    phase_row = con.execute(
        """SELECT phase FROM market_cycle_phase
           WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1""",
        [trade_date],
    ).fetchone()
    phase = str(phase_row[0]) if phase_row else "divergence"

    qlib_day = con.execute(
        """SELECT max(trade_date) FROM qlib_prediction WHERE model_id IN (
               SELECT model_id FROM qlib_model_registry)"""
    ).fetchone()[0]
    qlib_map = {}
    if qlib_day:
        for r in con.execute(
            """SELECT symbol, score FROM qlib_prediction WHERE trade_date = ?""",
            [qlib_day],
        ).fetchall():
            qlib_map[str(r[0])] = float(r[1])
    flow_map = {
        r["stock_code"]: r["pct"]
        for r in _fetch_dicts(
            con,
            """
            SELECT stock_code,
                   percent_rank() OVER (ORDER BY main_net_5d DESC) AS pct
            FROM (
                SELECT stock_code, avg(main_net_5d) AS main_net_5d
                FROM v_flow_feature_daily
                WHERE trade_date <= ? AND trade_date >= ?
                GROUP BY stock_code
            )
            """,
            [trade_date, trade_date],
        ) or []
    } if _table_exists(con, "v_flow_feature_daily") else {}

    concept_heat_rows = _fetch_dicts(
        con,
        """
        SELECT h.concept_name, count(*) AS zt_cnt
        FROM ths_concept_stock_history h
        WHERE CAST(h.trade_date AS DATE) = ?
        GROUP BY 1 ORDER BY zt_cnt DESC LIMIT 15
        """,
        [trade_date],
    ) if _table_exists(con, "ths_concept_stock_history") else []
    heat_names = {r["concept_name"] for r in concept_heat_rows}

    rows = []
    for p in pool:
        code = str(p["stock_code"])
        reasons = [s.strip() for s in (p.get("limit_up_reason") or "").split("+")]
        heat = max(
            (con.execute(
                "SELECT count(*) FROM official_limit_pool WHERE trade_date=? "
                "AND (limit_up_reason LIKE ?)",
                [trade_date, f"%{s}%"]).fetchone()[0]
             for s in reasons[:3]), default=0)
        rows.append({
            **p,
            "board": p.get("continue_day_cnt"),
            "qlib_score": qlib_map.get(code),
            "flow_rank_pct": flow_map.get(code),
            "concept_heat": (heat / max(1, len(pool))) if heat else None,
            "_hot_concept": sorted(heat_names & set(reasons)) or None,
        })
    return rows, phase


def _table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema='main' AND table_name=?", [name]).fetchone()[0])


def persist(con, trade_date: str, phase: str, picks: list[dict]) -> None:
    """Upsert picks.  A --skip-llm rerun must NOT wipe earlier narratives:
    llm_* columns fall back to the existing row via COALESCE."""
    con.execute("BEGIN TRANSACTION")
    try:
        for p in picks:
            con.execute(
                """
                INSERT INTO daily_stock_picks
                    (trade_date, stock_code, stock_name, rank, total_score,
                     board, limit_up_reason, factor_json, llm_bull_case,
                     llm_risk, llm_watch_condition, phase)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (trade_date, stock_code) DO UPDATE SET
                    stock_name=excluded.stock_name,
                    rank=excluded.rank,
                    total_score=excluded.total_score,
                    board=excluded.board,
                    limit_up_reason=excluded.limit_up_reason,
                    factor_json=excluded.factor_json,
                    phase=excluded.phase,
                    created_at=now(),
                    llm_bull_case=COALESCE(excluded.llm_bull_case,
                                           daily_stock_picks.llm_bull_case),
                    llm_risk=COALESCE(excluded.llm_risk,
                                      daily_stock_picks.llm_risk),
                    llm_watch_condition=COALESCE(excluded.llm_watch_condition,
                                                 daily_stock_picks.llm_watch_condition)
                """,
                [trade_date, p["stock_code"], p.get("stock_name"), p["rank"],
                 p["total_score"], p.get("board"), p.get("limit_up_reason"),
                 json.dumps(p.get("factor_json"), ensure_ascii=False),
                 p.get("llm_bull_case"), p.get("llm_risk"),
                 p.get("llm_watch_condition"), phase],
            )
        # Drop candidates that disappeared from today's universe entirely.
        codes = [p["stock_code"] for p in picks]
        if codes:
            marks = ", ".join("?" for _ in codes)
            con.execute(
                f"""DELETE FROM daily_stock_picks WHERE trade_date=?
                    AND stock_code NOT IN ({marks})""",
                [trade_date, *codes],
            )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise


def annotate_with_llm(client, phase: str, picks: list[dict],
                      top_n: int) -> dict[str, dict]:
    prompt = build_llm_prompt(phase, picks[:top_n])
    response = client.chat(prompt)
    return parse_llm_annotations(response)


def render_markdown(trade_date: str, phase: str, picks: list[dict]) -> str:
    lines = [
        f"# 次日候选筛选 · {trade_date}",
        "",
        f"市场相位：**{phase}** · 候选 {len(picks)} 只 · research-only，非投资建议",
        "",
        "| 排名 | 代码 | 名称 | 板 | 总分 | 涨停原因 | 做多逻辑 | 风险 | 明日观察 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for p in picks[:20]:
        lines.append(
            f"| {p['rank']} | {p['stock_code']} | {_esc(p.get('stock_name'))} "
            f"| {p.get('board') or '—'} | {p['total_score']} "
            f"| {_esc((p.get('limit_up_reason') or '')[:24])} "
            f"| {_esc(p.get('llm_bull_case') or '—')} "
            f"| {_esc(p.get('llm_risk') or '—')} "
            f"| {_esc(p.get('llm_watch_condition') or '—')} |"
        )
    lines += ["", "### 因子明细（Top10）", "", "```json"]
    for p in picks[:10]:
        lines.append(f"{p['rank']:>2}. {p['stock_code']} {p.get('factor_json')}")
    lines.append("```")
    return "\n".join(lines) + "\n"


from trade_system.review_extras import _esc as _esc  # re-export for report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default=str(date.today()))
    parser.add_argument("--top-llm", type=int, default=10)
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "reports" / "daily_picks_latest.md"))
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db)
    try:
        rows, phase = gather(con, args.trade_date)
        if not rows:
            print(f"{args.trade_date}: 官方涨停池无数据（非交易日或未回补）")
            return 1
        picks = score_candidates(rows, phase)

        if not args.skip_llm:
            try:
                from trade_system.deepseek_client import DeepSeekReviewClient
                client = DeepSeekReviewClient()
                if client.configured:
                    ann = annotate_with_llm(client, phase, picks, args.top_llm)
                    logger.info("llm annotations: %d/%d", len(ann), min(len(picks), args.top_llm))
                    for p in picks:
                        a = ann.get(str(p["stock_code"]))
                        if a:
                            p.update({f"llm_{k}": v for k, v in a.items()})
                    print(f"llm annotated: {len(ann)}")
                else:
                    print("llm skipped: DEEPSEEK_API_KEY 未配置")
            except Exception as exc:
                logger.warning("llm review failed (degraded to no-narrative): %r", exc)
                print(f"llm skipped: {exc!r}")

        persist(con, args.trade_date, phase, picks)
        Path(args.out).write_text(
            render_markdown(args.trade_date, phase, picks), encoding="utf-8")
        top = ", ".join(
            f"{p['stock_code']}({p['total_score']})" for p in picks[:8])
        print(f"picks={len(picks)}\ntop: {top}\nreport: {args.out}")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
