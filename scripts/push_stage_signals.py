"""Compose and push the day's triggered stage signals to configured channels.

Called with ``--notify`` from ``generate_stage_signals.py`` (or standalone);
delivery is a no-op unless a channel is configured via ``KPL_NOTIFY_*`` env,
and push failures never affect signal generation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.db_utils import fetch_dicts as _fetch_dicts  # noqa: E402
from trade_system.i18n_labels import STAGE_CN  # noqa: E402
from trade_system.logging_setup import configure  # noqa: E402
from trade_system.notify import send_text  # noqa: E402


def collect_triggered(con, trade_date: str, stage: str | None = None,
                      limit: int = 12) -> list[dict]:
    sql = """
        SELECT stage, stock_code, stock_name, score, reference_price
        FROM stock_candidate_stage_signal
        WHERE CAST(trade_date AS VARCHAR) = ?
          AND signal_triggered = true
    """
    params: list = [trade_date]
    if stage:
        sql += " AND stage = ?"
        params.append(stage)
    sql += " ORDER BY score DESC NULLS LAST LIMIT ?"
    params.append(limit)
    return _fetch_dicts(con, sql, params)


def compose(trade_date: str, rows: list[dict]) -> str:
    by_stage: dict[str, list[dict]] = {}
    for r in rows:
        by_stage.setdefault(str(r["stage"]), []).append(r)
    lines = [f"触发信号 {len(rows)} 条："]
    for stage, items in by_stage.items():
        label = STAGE_CN.get(stage, stage)
        tops = "、".join(
            f"{it.get('stock_name') or it.get('stock_code')}"
            f"({it.get('score') or 0:.0f}分)"
            for it in items[:5]
        )
        more = f" 等{len(items)}只" if len(items) > 5 else ""
        lines.append(f"- {label}: {tops}{more}")
    lines.append("（仅供研究，非订单；须通过风控与人工复核）")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", "--date", dest="trade_date",
                        default="")
    parser.add_argument("--stage", default=None)
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db, read_only=True)
    try:
        rows = collect_triggered(con, args.trade_date, args.stage)
    finally:
        con.close()
    print(f"triggered={len(rows)}")
    if not rows:
        return 0
    status = send_text(f"[stock_data] {args.trade_date} 信号提醒",
                       compose(args.trade_date, rows))
    for channel, result in status.items():
        print(f"{channel}={result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
