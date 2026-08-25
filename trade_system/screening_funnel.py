"""Six-level screening funnel (L0-L5) per institutional methodology.

Each stage is a named filter; stocks must pass ALL previous stages.
Stages can be enabled/disabled via config for gradual rollout.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FunnelStage:
    level: str          # L0..L5
    name: str
    input_count: int = 0
    output_count: int = 0
    removed: list[dict] = field(default_factory=list)


def _has_table(con, name: str) -> bool:
    return bool(con.execute(
        f"SELECT count(*) FROM information_schema.tables WHERE table_name='{name}'"
    ).fetchone()[0])


def run_funnel(con, trade_date: str) -> dict:
    """Execute multi-stage funnel on official_limit_pool universe."""
    stages: list[FunnelStage] = []

    # --- L0: Universe from today's limit pool ---
    universe = [dict(zip(
        ["stock_code", "stock_name", "board", "limit_up_reason", "is_st"],
        r)) for r in con.execute(
        """SELECT stock_code, max(stock_name), max(continue_day_cnt),
                  max(limit_up_reason), max(is_st)
           FROM official_limit_pool WHERE trade_date=?
           GROUP BY stock_code""", [trade_date]).fetchall()]
    stages.append(FunnelStage("L0", "全A基础池(当日涨停)", len(universe), len(universe)))

    # --- L1: Hard exclusion filter ---
    l1_pass = []
    removed_l1 = []
    for s in universe:
        if s.get("is_st"):
            removed_l1.append({"code": s["stock_code"], "reason": "ST"})
            continue
        if con.execute(
            """SELECT count(*) FROM redline_results
               WHERE stock_code=? AND trade_date=? AND triggered=true""",
            [s["stock_code"], trade_date]
        ).fetchone()[0] > 0 if _has_table(con, "redline_results") else False:
            removed_l1.append({"code": s["stock_code"], "reason": "财务红线"})
            continue
        l1_pass.append(s)
    stages.append(FunnelStage("L1", "硬性排雷(ST+红线)", len(universe), len(l1_pass),
                              removed_l1))

    # --- L2: Factor score threshold ---
    l2_pass = []
    for s in l1_pass:
        row = con.execute(
            """SELECT total_score FROM daily_stock_picks
               WHERE trade_date=? AND stock_code=?""",
            [trade_date, s["stock_code"]]).fetchone()
        if row and row[0] >= 35:
            s["total_score"] = row[0]
            l2_pass.append(s)
    stages.append(FunnelStage("L2", "多因子评分≥35", len(l1_pass), len(l2_pass)))

    # --- L3: Phase-fit check ---
    phase_row = con.execute(
        """SELECT phase FROM market_cycle_phase
           WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1""",
        [trade_date]).fetchone()
    phase = phase_row[0] if phase_row else "divergence"
    bad_phases = {"retreat", "ice"}
    l3_pass = []
    for s in l2_pass:
        board = s.get("board") or 1
        if phase in bad_phases and board >= 3:
            continue
        l3_pass.append(s)
    stages.append(FunnelStage("L3", "相位适配过滤", len(l2_pass), len(l3_pass)))

    # --- L4: Top N by score (buy decision pool) ---
    l3_pass.sort(key=lambda s: -(s.get("total_score") or 0))
    l4_pass = l3_pass[:20]
    stages.append(FunnelStage("L4", "买入决策池Top20", len(l3_pass), len(l4_pass)))

    # --- L5: Active portfolio target (top 10) ---
    l5_final = l4_pass[:10]
    stages.append(FunnelStage("L5", "核心组合Top10", len(l4_pass), len(l5_final)))

    return {
        "trade_date": trade_date,
        "phase": phase,
        "stages": [{"level": s.level, "name": s.name,
                     "input": s.input_count, "output": s.output_count,
                     "removed": len(s.removed)} for s in stages],
        "final_picks": [
            {"code": s["stock_code"], "name": s.get("stock_name"),
             "score": s.get("total_score"), "board": s.get("board")}
            for s in l5_final
        ],
    }
