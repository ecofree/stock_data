"""Audit sector mapping, candidate actionability, and human-outcome loop."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import duckdb


def _table(con, name: str) -> bool:
    return bool(con.execute(
        "select count(*) from information_schema.tables where table_name=?", [name]
    ).fetchone()[0])


def audit(db: str, trade_date: str, out: str) -> dict:
    con = duckdb.connect(db, read_only=True)
    rotation_columns = {
        str(row[0]) for row in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name='sector_rotation_score'"
        ).fetchall()
    }
    concept_filter = (
        "(taxonomy IN ('ths_concept','ths_concept_derived') "
        "OR (coalesce(taxonomy,'unknown')='unknown' AND sector_code LIKE 'THS-%'))"
        if "taxonomy" in rotation_columns else "sector_code LIKE 'THS-%'"
    )
    industry_filter = (
        "(taxonomy = 'em_industry' OR (coalesce(taxonomy,'unknown')='unknown' "
        "AND sector_code NOT LIKE 'THS-%'))"
        if "taxonomy" in rotation_columns else "sector_code NOT LIKE 'THS-%'"
    )
    fallback_concept_filter = (
        "(s.sector_type IN ('ths_concept','ths_concept_derived') "
        "OR (coalesce(s.sector_type,'unknown')='unknown' AND s.sector_code LIKE 'THS-%'))"
    )
    fallback_industry_filter = (
        "(s.sector_type = 'em_industry' "
        "OR (coalesce(s.sector_type,'unknown')='unknown' AND s.sector_code NOT LIKE 'THS-%'))"
    )
    if _table(con, "v_default_concept_daily"):
        fallback_concept_filter += (
            " AND EXISTS (SELECT 1 FROM v_default_concept_daily d "
            "WHERE d.trade_date=CAST(s.trade_date AS DATE) AND d.concept_code LIKE 'THS-%' "
            "GROUP BY d.trade_date HAVING count(DISTINCT d.concept_code)>0)"
        )
    rotation_sector = con.execute(
        """
        SELECT count(*), count(DISTINCT sector_code),
               sum(CASE WHEN coalesce(sector_name, '') = '' THEN 1 ELSE 0 END)
        FROM sector_rotation_score WHERE trade_date = ?
        """, [trade_date]
    ).fetchone()
    sector_relation = "sector_rotation_score"
    if not int(rotation_sector[0] or 0) and _table(con, "v_sector_capital"):
        sector = con.execute(
            """
            SELECT count(*), count(DISTINCT sector_code),
                   sum(CASE WHEN coalesce(sector_name, '') = '' THEN 1 ELSE 0 END)
            FROM v_sector_capital WHERE trade_date = ?
            """, [trade_date]
        ).fetchone()
        sector_relation = "v_sector_capital"
    else:
        sector = rotation_sector
    taxonomy_counts = {
        "concept": con.execute(
            f"SELECT count(DISTINCT sector_code) FROM sector_rotation_score "
            f"WHERE trade_date=? AND {concept_filter}", [trade_date]
        ).fetchone()[0] if sector_relation == "sector_rotation_score" else con.execute(
            f"SELECT count(DISTINCT s.sector_code) FROM v_sector_capital s "
            f"WHERE trade_date=? AND {fallback_concept_filter}", [trade_date]
        ).fetchone()[0] if sector_relation == "v_sector_capital" else 0,
        "industry": con.execute(
            f"SELECT count(DISTINCT sector_code) FROM sector_rotation_score "
            f"WHERE trade_date=? AND {industry_filter}", [trade_date]
        ).fetchone()[0] if sector_relation == "sector_rotation_score" else con.execute(
            f"SELECT count(DISTINCT s.sector_code) FROM v_sector_capital s "
            f"WHERE trade_date=? AND {fallback_industry_filter}", [trade_date]
        ).fetchone()[0] if sector_relation == "v_sector_capital" else 0,
    }
    stage = con.execute(
        """
        SELECT count(*), sum(CASE WHEN coalesce(is_actionable, false) THEN 1 ELSE 0 END),
               sum(CASE WHEN decision = 'blocked_data_quality' THEN 1 ELSE 0 END)
        FROM stock_candidate_stage_signal WHERE trade_date = ?
        """, [trade_date]
    ).fetchone() if _table(con, "stock_candidate_stage_signal") else (0, 0, 0)
    outcomes = con.execute(
        "SELECT count(*) FROM operator_trade_outcome WHERE trade_date = ?", [trade_date]
    ).fetchone()[0] if _table(con, "operator_trade_outcome") else 0
    top_concepts = []
    top_industries = []
    if sector_relation == "sector_rotation_score":
        top_concepts = con.execute(
            f"SELECT sector_code, sector_name, score FROM sector_rotation_score "
            f"WHERE trade_date=? AND {concept_filter} ORDER BY score DESC LIMIT 10", [trade_date]
        ).fetchall()
        top_industries = con.execute(
            f"SELECT sector_code, sector_name, score FROM sector_rotation_score "
            f"WHERE trade_date=? AND {industry_filter} ORDER BY score DESC LIMIT 10", [trade_date]
        ).fetchall()
        top = top_concepts
    elif sector_relation == "v_sector_capital":
        top_concepts = con.execute(
            f"SELECT s.sector_code, s.sector_name, s.main_net_inflow AS score FROM v_sector_capital s "
            f"WHERE trade_date=? AND {fallback_concept_filter} "
            "ORDER BY main_net_inflow DESC NULLS LAST LIMIT 10", [trade_date]
        ).fetchall()
        top_industries = con.execute(
            f"SELECT s.sector_code, s.sector_name, s.main_net_inflow AS score FROM v_sector_capital s "
            f"WHERE trade_date=? AND {fallback_industry_filter} "
            "ORDER BY main_net_inflow DESC NULLS LAST LIMIT 10", [trade_date]
        ).fetchall()
        top = top_concepts
    else:
        top = con.execute(
            "SELECT sector_code, sector_name, main_net AS score FROM multi_source_sector_flow "
            "WHERE source_date = CAST(? AS DATE) ORDER BY main_net DESC NULLS LAST LIMIT 10", [trade_date]
        ).fetchall()
    con.close()
    result = {
        "trade_date": trade_date,
        "sector_rows": int(sector[0] or 0),
        "sector_codes": int(sector[1] or 0),
        "sector_blank_names": int(sector[2] or 0),
        "sector_relation": sector_relation,
        "taxonomy_counts": taxonomy_counts,
        "stage_rows": int(stage[0] or 0),
        "stage_actionable": int(stage[1] or 0),
        "stage_blocked": int(stage[2] or 0),
        "operator_outcomes": int(outcomes),
        "top_sectors": [dict(code=r[0], name=r[1], score=r[2]) for r in top],
        "top_concepts": [dict(code=r[0], name=r[1], score=r[2]) for r in top_concepts],
        "top_industries": [dict(code=r[0], name=r[1], score=r[2]) for r in top_industries],
    }
    lines = [
        "# P3 候选池与板块映射审计", "", f"- 交易日：`{trade_date}`", "",
        "| 指标 | 数值 |", "|---|---:|",
        f"| 板块 rotation 行数 | {result['sector_rows']} |",
        f"| 板块代码数 | {result['sector_codes']} |",
        f"| 板块空名称数 | {result['sector_blank_names']} |",
        f"| 阶段候选行数 | {result['stage_rows']} |",
        f"| 数据可用候选数（非执行授权） | {result['stage_actionable']} |",
        f"| 数据阻断数 | {result['stage_blocked']} |",
        f"| 人工交易结果数 | {result['operator_outcomes']} |", "",
        "## 前十概念（同花顺口径）", "", "| 代码 | 名称 | 分数 |", "|---|---|---:|",
    ]
    lines.extend(f"| {r['code']} | {r['name']} | {r['score']:.2f} |" for r in result["top_concepts"])
    lines.extend(["", "## 前十行业（独立口径）", "", "| 代码 | 名称 | 分数 |", "|---|---|---:|"])
    lines.extend(f"| {r['code']} | {r['name']} | {r['score']:.2f} |" for r in result["top_industries"])
    lines.extend([
        "", "## 使用边界", "",
        "候选池只有在阶段窗口、同日数据截止时间、个股证据和风险门槛同时满足时才标记为可执行；历史补采数据晚于交易日的记录保持 blocked，避免回填数据伪装成盘中信号。",
        "人工结果必须由操作者填写模板后导入，不自动生成成交或收益。",
        "", "```json", json.dumps(result, ensure_ascii=False, indent=2, default=str), "```",
    ])
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True)
    parser.add_argument("--out", default="reports/p3_candidate_audit_latest.md")
    args = parser.parse_args()
    result = audit(args.db, args.date, args.out)
    print(f"report={args.out} sectors={result['sector_codes']} actionable={result['stage_actionable']} outcomes={result['operator_outcomes']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
