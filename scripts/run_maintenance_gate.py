"""Run the lightweight P2 maintenance gate for the stock-data project.

This gate is intentionally dependency-light.  It checks the production
boundaries that are easy to break during cleanup, compiles source files, and
can optionally run the full test suite.  It does not open or mutate the live
DuckDB database.
"""

from __future__ import annotations

import argparse
import ast
import compileall
from dataclasses import dataclass
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    passed: bool
    evidence: str


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _has_call(node: ast.AST, name: str) -> bool:
    return any(
        isinstance(item, ast.Call)
        and isinstance(item.func, ast.Name)
        and item.func.id == name
        for item in ast.walk(node)
    )


def _has_attribute(node: ast.AST, name: str) -> bool:
    return any(isinstance(item, ast.Attribute) and item.attr == name for item in ast.walk(node))


def check_canonical_structure() -> list[Check]:
    gate = _read("trade_system/gate_contract.py")
    review_web_source = _read("trade_system/review_web.py")
    daily_source = _read("trade_system/daily_review.py")
    runner_source = _read("scripts/run_integrated_daily.py")
    runtime_source = _read("trade_system/pipeline_runtime.py")
    review_tree = ast.parse(review_web_source)
    daily_tree = ast.parse(daily_source)
    review_render = _function(review_tree, "render_review_web")
    review_bundle = _function(review_tree, "_render_review_bundle")
    daily_context = _function(daily_tree, "build_daily_review_context")
    facts_boundary = any(
        _has_call(function, "build_review_page_facts")
        and not _has_attribute(function, "execute")
        for function in (review_render, review_bundle)
        if function is not None
    )
    return [
        Check(
            "operator_state_contract",
            all(token in gate for token in (
                "OPERATOR_STATE_VERSION",
                "data_certified_ready",
                "flow_certified_ready",
                "analysis_ready",
                "execution_ready",
            )),
            "gate_contract.py exposes the four explicit operator gates",
        ),
        Check(
            "review_facts_boundary",
            "from trade_system.review_facts import build_review_page_facts" in review_web_source
            and facts_boundary,
            "review rendering is presentation-only and delegates page facts",
        ),
        Check(
            "shared_review_connection",
            daily_context is not None
            and any(arg.arg == "con" for arg in daily_context.args.kwonlyargs),
            "build_daily_review_context accepts the caller-owned read-only connection",
        ),
        Check(
            "close_research_boundary",
            "include_research" in runner_source
            and "RESEARCH_CHAIN_STEPS" in runner_source
            and "if not research_enabled" in runner_source,
            "research chain is explicit opt-in for close execution",
        ),
        Check(
            "atomic_run_pointer",
            "pipeline_run_latest.json" in runtime_source
            and "LatestReportTransaction" in runtime_source,
            "latest publication has a same-run pointer",
        ),
    ]


def run_gate(run_tests: bool, test_timeout: int) -> tuple[list[Check], list[str]]:
    checks = check_canonical_structure()
    compiled = compileall.compile_dir(ROOT / "trade_system", quiet=1, force=False)
    compiled = compileall.compile_dir(ROOT / "scripts", quiet=1, force=False) and compiled
    checks.append(Check("python_compile", compiled, "trade_system/ and scripts/ compile cleanly"))
    test_lines: list[str] = []
    if run_tests:
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=test_timeout,
            check=False,
        )
        combined = (completed.stdout + "\n" + completed.stderr).strip()
        test_lines = combined.splitlines()[-12:]
        checks.append(Check("pytest", completed.returncode == 0, " ".join(test_lines[-2:])))
    else:
        checks.append(Check("pytest", True, "not run; use --pytest for full regression"))
    return checks, test_lines


def render_markdown(checks: list[Check], test_lines: list[str]) -> str:
    passed = all(item.passed for item in checks)
    lines = [
        "# P2 Maintenance Gate",
        "",
        f"- Status: `{'PASS' if passed else 'FAIL'}`",
        f"- Project root: `{ROOT}`",
        "- Scope: source structure, compile check, and optional pytest; no live database mutation.",
        "",
        "| Check | Status | Evidence |",
        "|---|---|---|",
    ]
    for item in checks:
        lines.append(f"| {item.name} | {'PASS' if item.passed else 'FAIL'} | {item.evidence} |")
    if test_lines:
        lines.extend(["", "## Test Tail", "", "```text", *test_lines, "```"])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the P2 maintenance gate.")
    parser.add_argument("--pytest", action="store_true", help="also run the full pytest suite")
    parser.add_argument("--test-timeout", type=int, default=300)
    parser.add_argument("--out", default="reports/p2_maintenance_gate_latest.md")
    args = parser.parse_args()

    try:
        checks, test_lines = run_gate(args.pytest, args.test_timeout)
    except subprocess.TimeoutExpired:
        checks = [Check("pytest", False, f"timed out after {args.test_timeout}s")]
        test_lines = []
    content = render_markdown(checks, test_lines)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(content, encoding="utf-8")
    passed = all(item.passed for item in checks)
    print(
        f"P2_MAINTENANCE_GATE status={'pass' if passed else 'fail'} "
        f"checks={sum(item.passed for item in checks)}/{len(checks)} out={out}"
    )
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
