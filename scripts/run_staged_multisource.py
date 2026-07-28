"""CLI for the ordered multi-source scheduler."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.staged_multisource import STAGE_ORDER, StageScheduler  # noqa: E402


def _split(value: str) -> list[str]:
    return [item.strip() for item in (value or "").split(",") if item.strip()]


def _write_report(path: str | Path, result: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# 分阶段多源数据采集",
        "",
        f"- run_id: `{result.get('run_id')}`",
        f"- stages: `{','.join(result.get('stages', []))}`",
        f"- planned: {result.get('planned', 0)}",
        f"- success/stale/failed/skipped: {result.get('completed', 0)}/{result.get('stale', 0)}/{result.get('failed', 0)}/{result.get('skipped', 0)}",
        f"- budget_exhausted: `{str(bool(result.get('budget_exhausted'))).lower()}`",
        f"- elapsed_seconds: {result.get('elapsed_seconds', 0)}",
        "",
        "|阶段|任务|数据类型|代码|状态|provider|写入行数|错误|",
        "|---|---|---|---|---|---|---:|---|",
    ]
    for item in result.get("tasks", []):
        task = item.get("task")
        if "stage" in item and not hasattr(task, "stage"):
            stage = item.get("stage", "")
            name = item.get("task", "")
            data_type = item.get("data_type", "")
            asset_code = item.get("asset_code", "")
        elif isinstance(task, dict):
            stage = task.get("stage", "")
            name = task.get("task", "")
            data_type = task.get("data_type", "")
            asset_code = task.get("asset_code", "")
        else:
            stage = task.stage
            name = task.name
            data_type = task.data_type
            asset_code = task.asset_code
        lines.append(
            f"|{stage}|{name}|{data_type}|{asset_code or '-'}|"
            f"{item.get('status')}|{item.get('provider') or '-'}|{item.get('rows_written', 0)}|"
            f"{str(item.get('error') or '').replace('|', '/') }|"
        )
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run market data collection in ordered, resumable stages.")
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--stage", choices=STAGE_ORDER, action="append", help="Stage to run; repeatable.")
    parser.add_argument("--all-stages", action="store_true", help="Run all stages in premarket-to-after_close order.")
    parser.add_argument("--date", default=date.today().isoformat(), help="Trade date, ISO or YYYYMMDD.")
    parser.add_argument("--start", default="", help="K-line/history start date; defaults to --date.")
    parser.add_argument("--end", default="", help="K-line/history end date; defaults to --date.")
    parser.add_argument("--stock-codes", default="")
    parser.add_argument("--index-codes", default="SH000001,SZ399001,SZ399006")
    parser.add_argument("--max-stocks", type=int, default=20)
    parser.add_argument("--max-sectors", type=int, default=200)
    parser.add_argument("--budget-seconds", type=float, default=120.0)
    parser.add_argument("--force", action="store_true", help="Ignore checkpoints and refresh providers (bypass source cache).")
    parser.add_argument("--dry-run", action="store_true", help="Print/write the plan without network requests.")
    parser.add_argument("--report", default="reports/multisource_staged_latest.md")
    args = parser.parse_args()

    if args.all_stages:
        stages = list(STAGE_ORDER)
    elif args.stage:
        stages = args.stage
    else:
        stages = ["close"]

    with StageScheduler(
        args.db,
        args.date,
        _split(args.stock_codes),
        _split(args.index_codes),
        start=args.start or args.date,
        end=args.end or args.date,
        max_stocks=args.max_stocks,
        max_sectors=args.max_sectors,
        budget_seconds=args.budget_seconds,
    ) as scheduler:
        result = scheduler.run(stages, force=args.force, dry_run=args.dry_run)
    _write_report(args.report, result)
    print(
        f"staged collection: run_id={result['run_id']} stages={','.join(result['stages'])} "
        f"planned={result['planned']} success={result.get('completed', 0)} "
        f"stale={result.get('stale', 0)} failed={result.get('failed', 0)} "
        f"skipped={result.get('skipped', 0)} report={args.report}"
    )
    return 0 if result.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
