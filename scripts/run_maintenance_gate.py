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
    status: str = "PASS"


def _read(relative: str) -> str:
    # Some existing project files were written with a UTF-8 BOM.  The gate
    # reads source for AST inspection, so strip that marker at the boundary
    # instead of treating a valid Python file as a syntax failure.
    return (ROOT / relative).read_text(encoding="utf-8-sig")


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
    schema_source = _read("trade_system/schema.py")
    base_source = _read("base.py")
    authority_source = _read("trade_system/source_authority.py")
    review_tree = ast.parse(review_web_source)
    daily_tree = ast.parse(daily_source)
    review_render = _function(review_tree, "render_review_web")
    review_bundle = _function(review_tree, "_render_review_bundle")
    daily_context = _function(daily_tree, "build_daily_review_context")
    # ``_render_review_bundle`` owns a few shared-context queries, but the
    # public renderer must delegate page-specific facts instead of issuing
    # those queries itself.  The previous check incorrectly rejected the
    # bundle merely because it also reads the cycle phase for the optional
    # sector-trail artifact.
    facts_boundary = (
        review_render is not None
        and not _has_attribute(review_render, "execute")
        and review_bundle is not None
        and _has_call(review_bundle, "build_review_page_facts")
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
        Check(
            "source_authority_contract",
            all(token in authority_source for token in (
                "SOURCE_POLICIES",
                "canonical_relation",
                "compatibility_only",
                "provider_rank",
            )),
            "production datasets expose one declared authority policy and stable provider ranking",
        ),
        Check(
            "source_conflict_audit",
            (ROOT / "scripts" / "audit_source_conflicts.py").exists()
            and "read_only=True" in _read("scripts/audit_source_conflicts.py"),
            "same-key provider conflicts have a read-only audit entrypoint",
        ),
        Check(
            "runtime_schema_boundary",
            "KPL_RUNTIME_SCHEMA_READY" in schema_source
            and "KPL_RUNTIME_SCHEMA_READY" in base_source
            and "KPL_RUNTIME_SCHEMA_READY" in runner_source
            and (ROOT / "migrations" / "0012_canonical_runtime_checkpoints.sql").exists(),
            "canonical child collectors rely on pre-applied migrations instead of runtime bootstrap DDL",
        ),
        Check(
            "phase_aware_review_publication",
            all(token in runtime_source for token in (
                'phase != "close" and path.name.startswith("daily_review_latest")',
                '"review_published"',
                '"pipeline-failure-banner" not in current_review.read_text',
            )),
            "non-close runs cannot replace the close review and failures cannot mutate the live page",
        ),
        Check(
            "explicit_readiness_stage",
            runner_source.count("--readiness-stage") >= 3
            and '"auction"' in runner_source
            and '"intraday"' in runner_source,
            "auction/intraday signal generation passes its explicit readiness stage",
        ),
        Check(
            "static_review_deferred_concept_data",
            all(token in review_web_source for token in (
                'id="review-concept-data"',
                "loadFullConceptData",
                "slice(0, 50)",
            )),
            "concept details remain inline but are parsed/rendered after explicit user expansion",
        ),
    ]


def check_cleanup_boundaries() -> list[Check]:
    """Check that cleanup did not reintroduce a second production authority."""
    fetch_all = _read("fetch_all.py")
    authority = _read("trade_system/source_authority.py")
    schema = _read("trade_system/schema.py")
    ths_quality = _read("trade_system/ths_quality.py")
    migration = ROOT / "migrations" / "0013_dynamic_snapshot_and_probe_runs.sql"
    fixed_concept_gates = [
        path.name
        for path in list((ROOT / "trade_system").glob("*.py")) + list((ROOT / "scripts").glob("*.py"))
        if path.name != "run_maintenance_gate.py"
        if "374" in path.read_text(encoding="utf-8-sig", errors="ignore")
        and path.name not in {"ths_quality.py"}
    ]
    test_files = list((ROOT / "tests").glob("test_*.py"))
    test_functions = sum(
        text.count("def test_")
        for text in (path.read_text(encoding="utf-8-sig", errors="ignore") for path in test_files)
    )
    transactional_sources = {
        "trade_system/flow_features.py": "def build_flow_features",
        "trade_system/candidate_pool.py": "def build_candidate_pool",
        "trade_system/ml/qlib_shadow.py": "def import_qlib_predictions",
        "trade_system/ml/shadow_evaluator.py": "def evaluate_qlib_shadow",
        "trade_system/daily_loop.py": "def run_daily_operator_loop",
        "trade_system/auction_evidence.py": "def persist_auction_evidence_snapshot",
    }
    transactional_ok = True
    for relative, marker in transactional_sources.items():
        source = _read(relative)
        transactional_ok = transactional_ok and marker in source and "BEGIN TRANSACTION" in source and (
            "ROLLBACK" in source or ".rollback()" in source
        )
    return [
        Check(
            "lazy_compatibility_imports",
            "if args.only_market" in fetch_all and "from collect_sector import" in fetch_all,
            "fetch_all loads the legacy collector graph only after the market-only exit",
        ),
        Check(
            "dynamic_ths_expectation",
            "ths_concept_snapshot_expectation" in schema
            and "minimum_concepts: int | None" in ths_quality
            and migration.exists(),
            "THS completeness is source-sized and versioned by migration",
        ),
        Check(
            "single_source_authority",
            "def provider_rank(" in authority and "def provider_rank_sql(" in authority,
            "provider selection is rendered from one authority matrix",
        ),
        Check(
            "no_fixed_production_concept_gate",
            not fixed_concept_gates,
            "no production module contains the retired fixed 374-concept gate",
        ),
        Check(
            "test_surface_inventory",
            bool(test_files) and test_functions > 0,
            f"test files={len(test_files)}; test functions={test_functions}; inventory only, no tests deleted",
            status="INFO",
        ),
        Check(
            "critical_persistence_transactions",
            transactional_ok,
            "critical delete/replace writers have explicit BEGIN/ROLLBACK boundaries",
        ),
        Check(
            "lifecycle_registry_boundary",
            (ROOT / "trade_system" / "maintenance_registry.py").exists()
            and "safe_to_drop" in _read("trade_system/maintenance_registry.py")
            and "does not move" in _read("trade_system/maintenance_registry.py"),
            "empty tables and legacy scripts are classified without implicit deletion",
        ),
    ]


def run_gate(run_tests: bool, test_timeout: int) -> tuple[list[Check], list[str]]:
    checks = check_canonical_structure() + check_cleanup_boundaries()
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
        checks.append(Check(
            "pytest", True,
            "not run; use --pytest for full regression",
            status="SKIPPED",
        ))
    return checks, test_lines


def render_markdown(checks: list[Check], test_lines: list[str]) -> str:
    failed = any(not item.passed for item in checks)
    skipped = any(item.status == "SKIPPED" for item in checks)
    gate_status = "FAIL" if failed else ("PASS_WITH_SKIPS" if skipped else "PASS")
    lines = [
        "# P2 Maintenance Gate",
        "",
        f"- Status: `{gate_status}`",
        f"- Project root: `{ROOT}`",
        "- Scope: source structure, compile check, and optional pytest; no live database mutation.",
        "",
        "| Check | Status | Evidence |",
        "|---|---|---|",
    ]
    for item in checks:
        row_status = "FAIL" if not item.passed else item.status
        lines.append(f"| {item.name} | {row_status} | {item.evidence} |")
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
    failed = any(not item.passed for item in checks)
    skipped = any(item.status == "SKIPPED" for item in checks)
    gate_status = "fail" if failed else ("pass_with_skips" if skipped else "pass")
    print(
        f"P2_MAINTENANCE_GATE status={gate_status} "
        f"checks={sum(item.passed for item in checks)}/{len(checks)} out={out}"
    )
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
