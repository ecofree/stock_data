"""Batch-fetch PE/PB/PS/PCF valuations for limit-pool stocks via HiThink API.

One API call covers up to ~50 codes.  Results stored in ``stock_valuations``
for use as a screening factor (cheap stocks with momentum = higher score).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from trade_system.hithink_client import HiThinkClient  # noqa: E402
from trade_system.logging_setup import configure  # noqa: E402

UPSERT = """
INSERT INTO stock_valuations (thscode, stock_code, pe_ttm, pe_mrq, pb_mrq, ps_ttm, pcf_ttm)
VALUES (?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (thscode) DO UPDATE SET
    pe_ttm=excluded.pe_ttm, pe_mrq=excluded.pe_mrq,
    pb_mrq=excluded.pb_mrq, ps_ttm=excluded.ps_ttm,
    pcf_ttm=excluded.pcf_ttm, fetched_at=now()
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--trade-date", default=str(__import__("datetime").date.today()))
    args = parser.parse_args()

    configure()
    client = HiThinkClient(min_interval=0.5)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        # Get latest limit pool codes + thscodes from official_limit_pool
        codes = con.execute(
            """SELECT DISTINCT o.stock_code, o.stock_name FROM official_limit_pool o
               WHERE o.trade_date = (
                   SELECT max(trade_date) FROM official_limit_pool)"""
        ).fetchall()
        if not codes:
            print("no limit pool data")
            return 1

        # Build thscodes list: 000001.SZ format
        def to_ths(code):
            if code.startswith("6"):
                return f"{code}.SH"
            return f"{code}.SZ"

        ths_codes = [to_ths(c[0]) for c in codes]
        print(f"fetching valuations for {len(ths_codes)} stocks...")

        # Batch fetch (50 per call)
        all_items = []
        for i in range(0, len(ths_codes), 50):
            batch = ",".join(ths_codes[i:i+50])
            data = client._get("/api/a-share/valuations/snapshot", {"thscodes": batch})
            items = data.get("item") or []
            all_items.extend(items)
            print(f"  batch {i//50+1}: {len(items)} results")

        con.execute("BEGIN TRANSACTION")
        try:
            for it in all_items:
                ticker = str(it.get("ticker") or "")
                if not ticker:
                    continue
                con.execute(UPSERT, [
                    it.get("thscode"), ticker,
                    it.get("pe_ttm"), it.get("pe_mrq"),
                    it.get("pb_mrq"), it.get("ps_ttm"), it.get("pcf_ttm"),
                ])
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

        n = con.execute("SELECT count(*) FROM stock_valuations").fetchone()[0]
        print(f"done: {n} total valuations stored")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
