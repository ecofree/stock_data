r"""Bounded QLib environment check.

This script deliberately performs imports only: it never downloads market data,
initialises a remote MLflow service, or mutates the project database.  Run it
with the optional interpreter (``.venv-qlib\Scripts\python.exe``) before
starting a shadow training job.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.metadata as metadata
import json
from pathlib import Path
import sys
from typing import Any


REQUIRED = {
    "pyqlib": ("qlib", "qlib.contrib.model.gbdt", "qlib.contrib.data.handler"),
    "lightgbm": ("lightgbm",),
    "cvxpy": ("cvxpy",),
    "gym": ("gym",),
    "mlflow": ("mlflow",),
    "ruamel.yaml": ("ruamel.yaml",),
    "filelock": ("filelock",),
    "loguru": ("loguru",),
    "pymongo": ("pymongo",),
}


def _version(dist: str) -> str | None:
    try:
        return metadata.version(dist)
    except metadata.PackageNotFoundError:
        return None


def check_environment() -> dict[str, Any]:
    packages: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    errors: list[dict[str, str]] = []
    for dist, modules in REQUIRED.items():
        item: dict[str, Any] = {"version": _version(dist), "modules": {}}
        if item["version"] is None:
            missing.append(dist)
        for module in modules:
            try:
                imported = importlib.import_module(module)
                item["modules"][module] = {
                    "ok": True,
                    "version": getattr(imported, "__version__", None),
                }
            except Exception as exc:  # import diagnostics are part of the report
                item["modules"][module] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                errors.append({"module": module, "error": item["modules"][module]["error"]})
        packages[dist] = item

    # Import the exact project integration used by train_qlib_shadow.py.
    integration = {"ok": False}
    try:
        from qlib.contrib.model.gbdt import LGBModel

        model = LGBModel(num_boost_round=1, learning_rate=0.05, verbosity=-1)
        integration = {"ok": True, "model": type(model).__name__}
    except Exception as exc:
        integration = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        errors.append({"module": "project_qlib_integration", "error": integration["error"]})

    return {
        "status": "ok" if not missing and not errors else "error",
        "python": sys.executable,
        "python_version": sys.version.split()[0],
        "packages": packages,
        "missing_distributions": missing,
        "import_errors": errors,
        "integration": integration,
        "note": "Import-only smoke check; no network, database, or MLflow server was touched.",
    }


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "# QLib 可选环境检查",
        "",
        f"- 状态：`{result['status']}`",
        f"- Python：`{result['python']}` (`{result['python_version']}`)",
        "- 范围：仅导入与 LGBModel 构造 smoke test，不访问网络、不写数据库、不启动 MLflow 服务。",
        "",
        "## 依赖",
        "",
        "| 分发包 | 版本 | 导入状态 |",
        "|---|---:|---|",
    ]
    for dist, item in result["packages"].items():
        import_state = "ok" if all(v.get("ok") for v in item["modules"].values()) else "error"
        lines.append(f"| `{dist}` | `{item['version'] or 'missing'}` | `{import_state}` |")
    lines += ["", "## 集成", "", f"`{json.dumps(result['integration'], ensure_ascii=False)}`", ""]
    if result["missing_distributions"]:
        lines += ["## 缺失分发包", "", ", ".join(f"`{x}`" for x in result["missing_distributions"]), ""]
    if result["import_errors"]:
        lines += ["## 导入错误", "", "```json", json.dumps(result["import_errors"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(lines)


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Check the isolated optional QLib runtime.")
    parser.add_argument("--json-out", default=str(root / "reports" / "qlib_env_latest.json"))
    parser.add_argument("--report", default=str(root / "reports" / "qlib_env_latest.md"))
    args = parser.parse_args()
    result = check_environment()
    Path(args.json_out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    Path(args.report).write_text(render_report(result), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
