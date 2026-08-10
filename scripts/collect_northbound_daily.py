"""收盘后采集北向资金（当日分钟级累计净买入）。

北向数据只存在于 staged 调度器（open/midday 阶段的 northbound、after_close
的 northbound_hist），而生产调度（auction/intraday/close phase）从不调用
staged 运行，导致 multi_source_observation 的北向行停在 2026-07-14。本脚本
把当日北向接回 close phase。

注意：**每日历史净流入已永久断供**——港交所自 2024-08-19 起停止披露北向
每日成交净买额（东财/akshare 该日起全为 NaN，EM 数据中心对 2024-08-16 之后
的查询返回空），因此不再采集 northbound_hist（避免每晚写入 failed 行）。
当日分钟级序列（同花顺 hgt/sgt，亿元）仍可用，落入
multi_source_observation.payload_json。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH, TODAY
from trade_system.multi_source_store import MultiSourceStore
from trade_system.resilient_sources import get as resilient_get


def collect_northbound_daily(db_path: str | Path, trade_date: str) -> dict:
    store = MultiSourceStore(str(db_path), fetcher=resilient_get)
    result = {
        "trade_date": trade_date,
        "intraday_points": 0,
        "provider": "",
        "status": "running",
    }
    try:
        data, meta = store.fetch("northbound")
        payload_ok = (
            isinstance(data, dict)
            and isinstance(data.get("time"), list)
            and isinstance(data.get("hgt"), list)
            and isinstance(data.get("sgt"), list)
            and len(data["time"]) > 0
            and len(data["time"]) == len(data["hgt"]) == len(data["sgt"])
        )
        if not payload_ok:
            result["provider"] = f"{meta.get('source') or 'unknown'}/schema_error"
            result["status"] = "schema_error"
            return result
        r = store.store("northbound", None, data, meta, trade_date=trade_date)
        result["intraday_points"] = len(data["time"])
        result["provider"] = f"{r.get('provider')}/{r.get('status')}"
        result["status"] = (
            "success"
            if result["intraday_points"] > 0
            and r.get("status") not in {"failed", "error"}
            else "error"
        )
    except Exception as exc:
        result["provider"] = f"error:{str(exc)[:120]}"
        result["status"] = "error"
    finally:
        store.close()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect northbound capital intraday net buys.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument("--out", default="reports/northbound_collection_latest.md")
    args = parser.parse_args()
    result = collect_northbound_daily(args.db, args.date)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        "# Northbound Collection\n\n"
        f"- trade_date: `{result['trade_date']}`\n"
        f"- intraday points: `{result['intraday_points']}`\n"
        f"- provider: `{result['provider']}`\n"
        "- note: daily history discontinued by HKEX since 2024-08-19 (not collected)\n",
        encoding="utf-8")
    print(" ".join(f"{k}={v}" for k, v in result.items()), f"report={out}")
    return 0 if result["status"] == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
