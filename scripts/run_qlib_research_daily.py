"""Run the isolated daily QLib research refresh.

This task is deliberately separate from the close publication chain. It
refreshes features and shadow predictions after close data is certified, while
keeping QLib research-only and fail-closed when the source date is not ready.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.pipeline_runtime import PipelineAlreadyRunning, PipelineLock


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return value.decode("utf-8", errors="backslashreplace")


def _subprocess_env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["KPL_RUNTIME_SCHEMA_READY"] = "1"
    return env


def _source_max_date(db_path: str | Path) -> str | None:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        row = con.execute(
            "SELECT max(CAST(date AS DATE)) FROM tushare_daily "
            "WHERE close IS NOT NULL AND close > 0"
        ).fetchone()
        return str(row[0]) if row and row[0] else None
    finally:
        con.close()


def _prediction_refresh_dates(
    db_path: str | Path,
    trade_date: str,
    *,
    max_catch_up_dates: int = 20,
) -> tuple[list[str], str | None]:
    """Return bounded source dates missing from the active model's predictions.

    A daily scheduler can miss several sessions while the source backfill is
    recovering.  Running only the newest date leaves the shadow evaluation
    window stranded at the last old prediction.  Catch-up is intentionally
    bounded and only uses dates that the QLib feature export can see; it never
    changes the model promotion or execution gates.
    """
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        try:
            model = con.execute(
                """
                SELECT model_id
                FROM qlib_model_registry
                WHERE status='champion' OR status IN ('challenger','shadow')
                ORDER BY CASE status WHEN 'champion' THEN 0 WHEN 'challenger' THEN 1 ELSE 2 END,
                         train_end DESC NULLS LAST, model_id
                LIMIT 1
                """
            ).fetchone()
        except duckdb.Error:
            return [], None
        if not model:
            return [], None
        model_id = str(model[0])
        latest_prediction = con.execute(
            "SELECT max(CAST(trade_date AS DATE)) FROM qlib_prediction WHERE model_id=?",
            [model_id],
        ).fetchone()
        last_date = latest_prediction[0] if latest_prediction and latest_prediction[0] else None
        if last_date and str(last_date) >= str(trade_date)[:10]:
            return [str(trade_date)[:10]], str(last_date)
        lower_bound = str(last_date) if last_date else "1900-01-01"
        rows = con.execute(
            """
            WITH coverage AS (
                SELECT CAST(date AS DATE) AS trade_date,
                       count(DISTINCT stock_code) AS instruments
                FROM tushare_daily
                WHERE close IS NOT NULL AND close > 0
                GROUP BY date
            ), limits AS (
                SELECT CASE WHEN coalesce(max(instruments), 0) >= 1000
                            THEN 1000 ELSE 1 END AS min_instruments
                FROM coverage
            )
            SELECT CAST(c.trade_date AS VARCHAR)
            FROM coverage c, limits l
            WHERE c.trade_date > CAST(? AS DATE)
              AND c.trade_date <= CAST(? AS DATE)
              AND c.instruments >= l.min_instruments
            ORDER BY c.trade_date
            """,
            [lower_bound, str(trade_date)[:10]],
        ).fetchall()
        dates = [str(row[0])[:10] for row in rows]
        if not dates:
            dates = [str(trade_date)[:10]]
        elif len(dates) > max(1, int(max_catch_up_dates)):
            dates = dates[-max(1, int(max_catch_up_dates)):]
        return dates, str(last_date) if last_date else None
    finally:
        con.close()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    os.replace(temp, path)


def _render_after_refresh(db: Path, trade_date: str, reports: Path,
                          as_of: str | None = None) -> dict[str, Any]:
    command = [
        sys.executable, "scripts/run_integrated_daily.py", "--db", str(db),
        "--trade-date", trade_date, "--phase", "close", "--render-only",
        # Rendering the self-contained review page can temporarily require
        # several times its final size.  Keep it out of the parent process so
        # the memory used by feature export/inference is released before HTML
        # generation and artifact audit begin.
        "--subprocess-reports",
        "--reports-dir", str(reports),
        "--run-id", f"qlib_render_{trade_date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}",
    ]
    if as_of:
        command.extend(["--as-of", as_of])
    completed = subprocess.run(
        command, cwd=str(ROOT), env=_subprocess_env(),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
    )
    return {
        "name": "render_daily_review",
        "return_code": completed.returncode,
        "stdout_tail": _decode(completed.stdout)[-2000:],
        "stderr_tail": _decode(completed.stderr)[-2000:],
    }


def _usage_summary(db: Path, trade_date: str) -> dict[str, Any]:
    """Return the operator-visible QLib contribution for the requested date."""
    con = duckdb.connect(str(db), read_only=True)
    try:
        def _count(sql: str, params: list[Any]) -> int:
            try:
                return int(con.execute(sql, params).fetchone()[0] or 0)
            except Exception:
                return 0

        prediction_rows = _count(
            "SELECT count(*) FROM qlib_prediction WHERE trade_date=?", [trade_date]
        )
        candidate_rows = _count(
            "SELECT count(*) FROM qlib_candidate_pool WHERE trade_date=?", [trade_date]
        )
        overlap_rows = _count(
            """
            SELECT count(*)
            FROM qlib_candidate_pool p
            JOIN v_limit_pool v ON v.trade_date=CAST(p.trade_date AS DATE)
                               AND v.stock_code=p.stock_code
            WHERE p.trade_date=?
            """,
            [trade_date],
        )
        evaluation = con.execute(
            """
            SELECT e.model_id, e.sample_end, e.sample_count, e.ic, e.rank_ic,
                   e.top_bottom_spread
            FROM qlib_shadow_evaluation e
            WHERE e.model_id = (
                SELECT model_id
                FROM qlib_prediction
                WHERE trade_date=?
                ORDER BY model_id
                LIMIT 1
            )
            ORDER BY e.updated_at DESC NULLS LAST, e.sample_end DESC NULLS LAST
            LIMIT 1
            """,
            [trade_date],
        ).fetchone()
        return {
            "trade_date": trade_date,
            "prediction_rows": prediction_rows,
            "candidate_pool_rows": candidate_rows,
            "candidate_limit_up_overlap": overlap_rows,
            "signal_impact": "disabled",
            "evaluation": {
                "model_id": evaluation[0],
                "sample_end": str(evaluation[1])[:10] if evaluation[1] else None,
                "sample_count": evaluation[2],
                "ic": evaluation[3],
                "rank_ic": evaluation[4],
                "top_bottom_spread": evaluation[5],
            } if evaluation else None,
        }
    finally:
        con.close()


def run(db_path: str | Path, trade_date: str, reports_dir: str | Path,
        *, as_of: str | None = None) -> dict[str, Any]:
    db = Path(db_path).resolve()
    reports = Path(reports_dir).resolve()
    run_id = f"qlib_research_{trade_date.replace('-', '')}_{datetime.now().strftime('%H%M%S')}"
    out = reports / "qlib_research_latest.json"
    feature_file = reports / "qlib_features_2026_exec.parquet"
    steps: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "run_id": run_id,
        "trade_date": trade_date,
        "status": "running",
        "signal_impact": "disabled",
        "model_status": "shadow",
        "steps": steps,
    }

    try:
        with PipelineLock(db, run_id):
            source_date = _source_max_date(db)
            result["source_date"] = source_date
            if not source_date or source_date < trade_date:
                result.update(
                    status="blocked_source_not_current",
                    reason=f"tushare_daily_max={source_date or 'missing'} < trade_date={trade_date}",
                )
                _atomic_json(out, result)
                return result

            prediction_dates, last_prediction_date = _prediction_refresh_dates(db, trade_date)
            result["last_prediction_date_before_refresh"] = last_prediction_date
            result["prediction_refresh_dates"] = prediction_dates
            commands = [
                (
                    "build_flow_features",
                    [sys.executable, "scripts/build_flow_features.py", "--db", str(db),
                     "--end-date", trade_date, "--out", str(reports / "flow_features_latest.json")],
                ),
                (
                    "export_qlib_features",
                    [sys.executable, "scripts/export_qlib_features.py", "--db", str(db),
                     "--start-date", "2024-01-01", "--end-date", trade_date,
                     "--out", str(feature_file), "--format", "parquet", "--label-mode", "t1_exec"],
                ),
                (
                    "evaluate_qlib_shadow",
                    [sys.executable, "scripts/evaluate_qlib_shadow.py", "--db", str(db),
                     "--out", str(reports / "qlib_shadow_latest.md")],
                ),
            ]
            inference_commands = []
            for prediction_date in prediction_dates:
                step_name = (
                    "run_qlib_daily"
                    if prediction_date == trade_date
                    else f"run_qlib_daily_backfill_{prediction_date.replace('-', '')}"
                )
                inference_commands.append(
                    (
                        step_name,
                        [sys.executable, "scripts/run_qlib_daily.py", "--db", str(db),
                         "--features", str(feature_file), "--trade-date", prediction_date,
                         "--allow-shadow", "--out", str(reports / "qlib_daily_latest.json")],
                    )
                )
            commands[2:2] = inference_commands
            env = _subprocess_env()
            for name, command in commands:
                completed = subprocess.run(
                    command, cwd=str(ROOT), env=env,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
                )
                item = {
                    "name": name,
                    "return_code": completed.returncode,
                    "stdout_tail": _decode(completed.stdout)[-2000:],
                    "stderr_tail": _decode(completed.stderr)[-2000:],
                }
                steps.append(item)
                if completed.returncode != 0:
                    result.update(status="failed", reason=name)
                    _atomic_json(out, result)
                    return result

            from trade_system.ml.feature_artifacts import resolve_feature_path
            metadata = resolve_feature_path(feature_file).with_suffix(".metadata.json")
            if metadata.exists():
                try:
                    result["feature_metadata"] = json.loads(metadata.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    result["feature_metadata_error"] = "invalid_json"
            daily = reports / "qlib_daily_latest.json"
            if daily.exists():
                try:
                    result["prediction"] = json.loads(daily.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    result["prediction_error"] = "invalid_json"
            result["usage"] = _usage_summary(db, trade_date)
            result["status"] = "completed"
            result["completed_at"] = datetime.now().isoformat(timespec="seconds")
    except PipelineAlreadyRunning as exc:
        result.update(status="deferred_pipeline_busy", reason=str(exc))
        _atomic_json(out, result)
        return result
    except Exception as exc:
        result.update(status="failed", reason=f"{type(exc).__name__}: {exc}")
    if result.get("status") == "completed":
        render_step = _render_after_refresh(db, trade_date, reports, as_of)
        result["steps"].append(render_step)
        if render_step["return_code"] != 0:
            result["status"] = "completed_with_warnings"
    _atomic_json(out, result)
    return result


def main() -> int:
    from trade_system.config import default_trade_date

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(ROOT / "kpl_data.duckdb"))
    parser.add_argument(
        "--trade-date", default="",
        help="Defaults to the latest open session resolved from the database calendar.",
    )
    parser.add_argument("--reports-dir", default=str(ROOT / "reports"))
    parser.add_argument("--as-of", default="", help="Review freshness clock for a historical rerun.")
    args = parser.parse_args()
    trade_date = args.trade_date or default_trade_date(args.db)
    result = run(args.db, trade_date, args.reports_dir, as_of=args.as_of or None)
    print(json.dumps(result, ensure_ascii=False, default=str))
    return 0 if result["status"] == "completed" else 3 if result["status"] == "deferred_pipeline_busy" else 2


if __name__ == "__main__":
    raise SystemExit(main())
