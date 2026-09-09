"""Run a small production-path KPL connectivity and data canary.

This is deliberately read-only.  It proves the configured HTTPS transport,
API key, route, and a non-empty business payload without writing raw responses
or secrets to the report.  A failure is useful evidence and must not be
converted into a green status by falling back to an old local cache.
"""

from __future__ import annotations

import argparse
from datetime import date
import json
from pathlib import Path
import sys
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from base import KPLClient  # noqa: E402
from config import API_BASE, API_KEY, DB_PATH, TODAY  # noqa: E402
from trade_system.api_data_audit import latest_local_trade_date  # noqa: E402
from trade_system.http_transport import ssl_context_note  # noqa: E402


def _payload_count(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for key in ("data", "raw_data", "stocks", "sectors", "list", "items", "klines"):
            value = payload.get(key)
            if isinstance(value, (list, dict)):
                return len(value)
        return 1 if payload else 0
    return 1


def _host(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    return parsed.netloc or parsed.path.split("/", 1)[0]


def _infer_stock_code(db_path: str | Path) -> str:
    """Choose a locally known stock for the K-line canary."""
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            row = con.execute(
                """
                SELECT stock_code
                FROM kline
                WHERE stock_code IS NOT NULL AND trim(stock_code) <> ''
                GROUP BY stock_code
                ORDER BY count(*) DESC, stock_code
                LIMIT 1
                """
            ).fetchone()
        finally:
            con.close()
        if row and row[0]:
            return str(row[0]).strip()
    except Exception:
        pass
    return "000938"


def run_probe(db_path: str | Path, trade_date: str, stock_code: str) -> dict[str, Any]:
    client = KPLClient(request_timeout=12, max_attempts=1, total_budget_seconds=30)
    checks: list[dict[str, Any]] = []
    for name, endpoint, params in (
        ("daily", "/daily", {"date": trade_date}),
        ("kline", "/kline", {"code": stock_code, "ktype": "d", "count": "5"}),
        ("market_rise_fall", "/market/rise-fall", {"date": trade_date}),
        ("index_zhishu_kline", "/index/zhishu-kline", {"code": "SH000001", "ktype": "d", "index": "0"}),
    ):
        payload = client.get(endpoint, params, critical=True)
        checks.append(
            {
                "name": name,
                "endpoint": endpoint,
                "status": "data" if payload is not None and _payload_count(payload) > 0 else "unavailable",
                "item_count": _payload_count(payload),
            }
        )

    has_data = any(item["status"] == "data" for item in checks)
    all_data = all(item["status"] == "data" for item in checks)
    circuit_reason = getattr(client, "_circuit_open_reason", None)
    if circuit_reason == "tls_certificate_untrusted":
        status = "tls_certificate_untrusted"
    elif circuit_reason and str(circuit_reason).startswith("api_auth_rejected"):
        status = "api_auth_rejected"
    elif client.stats.get("auth_error") and not has_data:
        status = "api_auth_rejected"
    elif client.stats.get("route_error") and not has_data:
        status = "api_route_not_found"
    elif all_data:
        status = "reachable_with_data"
    elif has_data:
        status = "reachable_partial"
    else:
        status = "reachable_empty_or_unavailable"
    return {
        "status": status,
        "trade_date": trade_date,
        "stock_code": stock_code,
        "api_host": _host(API_BASE),
        "transport": ssl_context_note(),
        "api_key_configured": bool(str(API_KEY or "").strip()),
        "checks": checks,
        "stats": dict(client.stats),
        "circuit_reason": circuit_reason or "",
    }


def render_report(result: dict[str, Any]) -> str:
    lines = [
        "# KPL 连通性与数据 Canary",
        "",
        f"- status: `{result['status']}`",
        f"- api_host: `{result['api_host']}`",
        f"- trade_date: `{result['trade_date']}`",
        f"- stock_code: `{result['stock_code']}`",
        f"- transport: `{result['transport']}`",
        f"- api_key_configured: `{str(result['api_key_configured']).lower()}`",
        f"- circuit_reason: `{result['circuit_reason'] or '-'}`",
        "",
        "## 核心接口",
        "",
        "| check | endpoint | status | item_count |",
        "|---|---|---|---:|",
    ]
    for item in result["checks"]:
        lines.append(
            f"| {item['name']} | `{item['endpoint']}` | {item['status']} | {item['item_count']} |"
        )
    lines.extend(
        [
            "",
            "## 请求统计",
            "",
            "```json",
            json.dumps(result["stats"], ensure_ascii=False, sort_keys=True),
            "```",
            "",
            "说明：本报告只记录结构化状态和行数，不保存 API Key 或原始响应。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Check KPL verified HTTPS and business-data availability.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default="", help="Trading date; defaults to newest local snapshot.")
    parser.add_argument("--stock-code", default="", help="Stock code for K-line canary; defaults to a local core code.")
    parser.add_argument("--out", default=str(ROOT / "reports" / "kpl_connectivity_latest.md"))
    parser.add_argument("--json-out", default=str(ROOT / "reports" / "kpl_connectivity_latest.json"))
    args = parser.parse_args()

    selected_date = args.date or latest_local_trade_date(args.db, TODAY)
    result = run_probe(args.db, selected_date, args.stock_code or _infer_stock_code(args.db))
    report = render_report(result)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")
    json_out = Path(args.json_out)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"kpl_connectivity_status={result['status']}")
    print(f"kpl_connectivity_report={out}")
    print(f"kpl_connectivity_json={json_out}")
    return 0 if result["status"] == "reachable_with_data" else 2


if __name__ == "__main__":
    raise SystemExit(main())
