"""Retry late close supplements and republish the self-contained review page.

The primary close run is intentionally bounded.  This follow-up runs after
the provider's publication window and retries only data sets that frequently
arrive late: KPL auction/LHB/index data and xiaodefa chip/margin data.  It
uses the same process lock as the main pipeline and never changes execution
gates or turns stale data into a successful batch.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.pipeline_runtime import PipelineAlreadyRunning, PipelineLock


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="backslashreplace")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    os.replace(temp, path)


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # The retry runner owns PipelineLock and migrations are applied before
    # its children start.  Keep the supplement from replaying bootstrap DDL.
    env["KPL_RUNTIME_SCHEMA_READY"] = "1"
    return env


def _commands(db: Path, trade_date: str, reports: Path) -> list[tuple[str, list[str]]]:
    py = sys.executable
    return [
        (
            "collect_lhb_daily",
            [py, "scripts/collect_lhb_daily.py", "--db", str(db), "--date", trade_date,
             "--out", str(reports / "lhb_collection_latest.md")],
        ),
        (
            "collect_auction_market_daily",
            [py, "scripts/collect_auction_market_daily.py", "--db", str(db), "--date", trade_date,
             "--out", str(reports / "auction_market_collection_latest.json")],
        ),
        (
            "collect_index_kline_daily",
            [py, "scripts/collect_index_kline_daily.py", "--db", str(db), "--date", trade_date,
             "--out", str(reports / "index_kline_collection_latest.md")],
        ),
        (
            "collect_xiaodefa_critical",
            [py, "-m", "trade_system.xiaodefa_source", "--db", str(db),
             "--trade-date", trade_date, "--start-date", trade_date,
             "--end-date", trade_date, "--kinds", "cyq,margin,margin_detail"],
        ),
    ]


def _render_after_retry(db: Path, trade_date: str, reports: Path) -> dict[str, Any]:
    command = [
        sys.executable, "scripts/run_integrated_daily.py", "--db", str(db),
        "--trade-date", trade_date, "--phase", "close", "--render-only",
        "--reports-dir", str(reports),
        "--run-id", f"supplemental_render_{trade_date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}",
    ]
    completed = subprocess.run(
        command, cwd=str(ROOT), env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return {
        "name": "render_daily_review",
        "return_code": completed.returncode,
        "stdout_tail": _decode(completed.stdout)[-2000:],
        "stderr_tail": _decode(completed.stderr)[-2000:],
    }


def run(db_path: str | Path, trade_date: str, reports_dir: str | Path) -> dict[str, Any]:
    db = Path(db_path).resolve()
    reports = Path(reports_dir).resolve()
    run_id = f"supplemental_retry_{trade_date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}"
    out = reports / "supplemental_retry_latest.json"
    result: dict[str, Any] = {
        "run_id": run_id,
        "trade_date": trade_date,
        "status": "running",
        "steps": [],
    }
    try:
        with PipelineLock(db, run_id):
            for name, command in _commands(db, trade_date, reports):
                completed = subprocess.run(
                    command, cwd=str(ROOT),
                    env=_subprocess_env(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                item = {
                    "name": name,
                    "return_code": completed.returncode,
                    "stdout_tail": _decode(completed.stdout)[-2000:],
                    "stderr_tail": _decode(completed.stderr)[-2000:],
                }
                result["steps"].append(item)
            # A multi-kind xiaodefa invocation exits non-zero when one
            # critical dataset is late, even if another dataset was written
            # successfully.  Republish in that case too, so the page does not
            # remain stale for the subset that did arrive.
            successful_collection = any(
                item["name"] != "render_daily_review"
                and (
                    item["return_code"] == 0
                    or "[OK ]" in str(item.get("stdout_tail") or "")
                )
                for item in result["steps"]
            )
            result["collection_status"] = (
                "completed" if all(item["return_code"] == 0 for item in result["steps"])
                else "degraded"
            )
        # Rendering needs a fresh lock of its own, so it is deliberately
        # outside the collection lock's context.
        if successful_collection:
            result["steps"].append(_render_after_retry(db, trade_date, reports))
        result["status"] = (
            "completed" if result["collection_status"] == "completed"
            and all(item["return_code"] == 0 for item in result["steps"])
            else "completed_degraded"
        )
        result["completed_at"] = datetime.now().isoformat(timespec="seconds")
    except PipelineAlreadyRunning as exc:
        result.update(status="deferred_pipeline_busy", reason=str(exc))
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    _atomic_json(out, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default=date.today().isoformat())
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    args = parser.parse_args()
    result = run(args.db, args.trade_date, args.reports_dir)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] == "completed" else 3 if result["status"] == "deferred_pipeline_busy" else 2


if __name__ == "__main__":
    raise SystemExit(main())
