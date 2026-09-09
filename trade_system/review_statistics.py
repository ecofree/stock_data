"""Daily review statistics for staged operator signals."""

from __future__ import annotations

from pathlib import Path

import duckdb

from trade_system.backtest import run_stage_candidate_backtest
from trade_system.quality import table_exists
from trade_system.db_utils import fetch_dicts as _fetch_dicts



def _verdict(stats: dict, min_return_samples: int) -> str:
    samples = int(stats.get("return_sample_count") or 0)
    if samples < min_return_samples:
        return "insufficient_sample"
    hit_rate = stats.get("hit_rate")
    avg_return = stats.get("avg_forward_return_pct")
    # 仅描述样本内统计，不证明策略能力；判读规则见页面 Interpretation Rules。
    if hit_rate is not None and avg_return is not None and hit_rate >= 50 and avg_return > 0:
        return "positive_review_sample"
    if avg_return is not None and avg_return < 0:
        return "negative_review_sample"
    return "mixed_sample"


def _regime_stage_counts(db_path: str | Path) -> dict[str, dict[str, int]]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        if not table_exists(con, "stock_candidate_stage_signal") or not table_exists(con, "market_regime_snapshot"):
            return {}
        # 与回测口径对齐：只计 is_actionable=true（列存在时），展示≠回测的混入在此切断。
        actionable_filter = ""
        try:
            cols = {row[1] for row in con.execute("PRAGMA table_info('stock_candidate_stage_signal')").fetchall()}
            if "is_actionable" in cols:
                actionable_filter = "AND coalesce(s.is_actionable, false) = true"
        except Exception:
            actionable_filter = ""
        rows = _fetch_dicts(
            con,
            f"""
            SELECT coalesce(m.regime, 'unknown') AS regime, s.stage, count(*) AS count
            FROM stock_candidate_stage_signal s
            LEFT JOIN market_regime_snapshot m ON s.trade_date = m.trade_date
            WHERE 1 = 1 {actionable_filter}
            GROUP BY 1, 2
            ORDER BY 1, 2
            """,
        )
    finally:
        con.close()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        result.setdefault(row["regime"], {})[row["stage"]] = int(row["count"] or 0)
    return result


def build_daily_review_statistics(db_path: str | Path, min_return_samples: int = 50) -> dict:
    # Align with the formal stage/operator/web backtests, which all use enforce_t1=True.
    # The default (False) would score same-day buy/sell returns that are not executable
    # under A-share T+1, making the daily review caliber inconsistent with them.
    backtest = run_stage_candidate_backtest(db_path, enforce_t1=True)
    stage_statistics = {}
    for stage, stats in backtest.get("stage_stats", {}).items():
        item = dict(stats)
        item["verdict"] = _verdict(item, min_return_samples)
        item["min_return_samples"] = min_return_samples
        stage_statistics[stage] = item
    return {
        "sample_count": backtest.get("sample_count", 0),
        "stage_statistics": stage_statistics,
        "regime_stage_counts": _regime_stage_counts(db_path),
    }


def render_daily_review_statistics(stats: dict) -> str:
    lines = [
        "# Daily Review Statistics",
        "",
        f"- Signal samples: `{stats.get('sample_count', 0)}`",
        "",
        "## Stage Statistics",
        "",
        "| Stage | Signals | Return Samples | Hit Rate | Avg Forward Return | Verdict |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for stage, item in sorted(stats.get("stage_statistics", {}).items()):
        hit = "" if item.get("hit_rate") is None else f"{float(item['hit_rate']):.2f}%"
        avg = "" if item.get("avg_forward_return_pct") is None else f"{float(item['avg_forward_return_pct']):.2f}%"
        lines.append(
            f"| {stage} | {int(item.get('sample_count') or 0)} | {int(item.get('return_sample_count') or 0)} | "
            f"{hit} | {avg} | `{item.get('verdict')}` |"
        )
    lines.extend(
        [
            "",
            "## Regime Group Counts",
            "",
            "| Regime | Stage | Signals |",
            "|---|---|---:|",
        ]
    )
    for regime, stages in sorted(stats.get("regime_stage_counts", {}).items()):
        for stage, count in sorted(stages.items()):
            lines.append(f"| {regime} | {stage} | {count} |")
    lines.extend(
        [
            "",
            "## Interpretation Rules",
            "",
            "- `insufficient_sample` means the stage does not yet have enough forward-return samples to support a trading conclusion.",
            "- `positive_review_sample` / `negative_review_sample` describe in-sample statistics only; they do not prove strategy capability.",
            "- Regime group counts cover actionable signals only (`is_actionable=true` when the column exists), aligned with the backtest caliber.",
            "",
        ]
    )
    return "\n".join(lines)
