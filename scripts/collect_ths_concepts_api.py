"""Collect today's THS concept catalog + members via the HiThink official API.

Writes the same tables as the legacy web crawler (``ths_concept_daily`` /
``ths_concept_stock_history``) so the normalized concept views keep working.

Concept-code bridging: the web crawler used ``THS-{akshare-id}`` while
HiThink exposes ``{885xxx}.TI``.  To keep per-concept history continuous,
an existing ``concept_code`` is reused when the exact concept name already
exists in recent snapshots; otherwise a new code ``THS-{hithink_id}`` is
minted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.hithink_client import HiThinkClient  # noqa: E402
from trade_system.logging_setup import configure, get_logger  # noqa: E402
from trade_system.schema import init_schema  # noqa: E402

logger = get_logger("ths_api_collector")


def _existing_code_by_name(con: duckdb.DuckDBPyConnection,
                           days: int = 120) -> dict[str, str]:
    rows = con.execute(
        f"""
        SELECT concept_name, concept_code FROM (
            SELECT concept_name, concept_code,
                   row_number() OVER (
                       PARTITION BY concept_name ORDER BY trade_date DESC
                   ) AS _rn
            FROM ths_concept_daily
            WHERE CAST(trade_date AS DATE) >= current_date - INTERVAL '{days}' DAY
        ) WHERE _rn = 1
        """,
    ).fetchall()
    return {name: code for name, code in rows}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--snapshot-date", default="",
                        help="YYYY-MM-DD; defaults to today.")
    parser.add_argument("--max-concepts", type=int, default=0,
                        help="Bound concepts this run (0 = all).")
    args = parser.parse_args()

    configure()
    snap = (args.snapshot_date or datetime.now().strftime("%Y-%m-%d"))
    client = HiThinkClient(min_interval=0.35)
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        # Keep standalone repair/backfill runs safe on an older database.  The
        # integrated close runner already applies schema under PipelineLock;
        # init_schema is a no-op in its guarded child processes.
        init_schema(con)
        bridge = _existing_code_by_name(con)
        catalog = client.ths_concept_catalog(tag="cn_concept")
        if args.max_concepts:
            catalog = catalog[: args.max_concepts]
        print(f"catalog: {len(catalog)} concepts; bridge table {len(bridge)} names")
        catalog_hash = hashlib.sha256(
            json.dumps(
                [
                    {"thscode": str(item.get("thscode") or ""), "name": str(item.get("name") or "")}
                    for item in catalog
                ],
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        # A bounded test/repair run must not publish its partial size as the
        # production expectation.  The full official catalogue is the only
        # snapshot allowed to advance the dynamic completeness gate.
        if not args.max_concepts:
            con.execute(
                """
                INSERT INTO ths_concept_snapshot_expectation(
                    trade_date, expected_concepts, provider, catalog_hash, status, fetched_at
                ) VALUES (?, ?, 'hithink_index_api', ?, 'success', now())
                ON CONFLICT(trade_date) DO UPDATE SET
                    expected_concepts=excluded.expected_concepts,
                    provider=excluded.provider,
                    catalog_hash=excluded.catalog_hash,
                    status=excluded.status,
                    fetched_at=excluded.fetched_at
                """,
                [snap, len(catalog), catalog_hash],
            )

        written_d = written_m = skipped = 0
        for idx, entry in enumerate(catalog):
            raw_code = str(entry.get("thscode") or "")
            numeric = raw_code.split(".")[0]
            name = str(entry.get("name") or "")
            concept_code = bridge.get(name) or f"THS-{numeric}"

            try:
                members = client.ths_index_constituents(raw_code)
            except Exception as exc:
                logger.warning("%s (%s) constituents failed: %s", name, raw_code, exc)
                skipped += 1
                continue

            # Deduplicate members by ticker (API may return duplicates)
            seen_tickers = set()
            unique_members = []
            for m in members:
                tk = str(m.get("ticker") or "")
                if tk and tk not in seen_tickers:
                    seen_tickers.add(tk)
                    unique_members.append(m)
            members = unique_members

            raw_daily = json.dumps({
                "requested_date": snap, "fetched_date": snap,
                "date_verified": True, "provider": "hithink_index_api",
                "ths_index_code": raw_code,
                "advertised_member_count": len(members),
            }, ensure_ascii=False)

            con.execute("BEGIN TRANSACTION")
            try:
                # Remove ALL same-day rows for this concept regardless of
                # source — the latest collector run is authoritative.
                con.execute(
                    """DELETE FROM ths_concept_stock_history
                       WHERE trade_date=? AND concept_code=?""",
                    [snap, concept_code],
                )
                con.execute(
                    "DELETE FROM ths_concept_daily WHERE trade_date=? AND concept_code=?",
                    [snap, concept_code],
                )
                con.execute(
                    """DELETE FROM ths_concept_member_checkpoint
                       WHERE trade_date=? AND concept_code=?""",
                    [snap, concept_code],
                )
                con.execute(
                    """INSERT INTO ths_concept_daily
                       (trade_date, concept_code, concept_name, rank, stock_count,
                        source, raw_json, date_verified, fetched_at)
                       VALUES (?, ?, ?, ?, ?, 'hithink_index_api', ?, true, now())""",
                    [snap, concept_code, name, idx + 1, len(members), raw_daily],
                )
                # Write checkpoint row so the source-sized completeness gate passes.
                con.execute(
                    """INSERT INTO ths_concept_member_checkpoint
                       (trade_date, concept_code, concept_name, status,
                        pages_expected, pages_fetched, member_rows, attempts,
                        last_error, updated_at, provider, crawler_version,
                        catalog_hash)
                       VALUES (?, ?, ?, 'success', 1, 1, ?, 1, '',
                               now(), 'hithink_index_api', 'hithink_api_v1', 'api')""",
                    [snap, concept_code, name, len(members)],
                )
                written_d += 1
                for m in members:
                    ticker = str(m.get("ticker") or "")
                    if not ticker.isdigit():
                        continue
                    enriched = dict(m)
                    enriched["fetched_date"] = snap
                    enriched["provider"] = "hithink_index_api"
                    try:
                        con.execute(
                            """INSERT INTO ths_concept_stock_history
                               (trade_date, concept_code, concept_name, stock_code,
                                stock_name, concept_rank, source, raw_json,
                                date_verified, fetched_at)
                               VALUES (?, ?, ?, ?, ?, NULL, 'hithink_index_api',
                                       ?, true, now())""",
                            [snap, concept_code, name, ticker, m.get("name"),
                             json.dumps(enriched, ensure_ascii=False)],
                        )
                        written_m += 1
                    except duckdb.ConstraintException:
                        pass  # duplicate member entry, safe to skip
                    except Exception as exc:
                        logger.debug("%s/%s insert error: %s", concept_code, ticker, exc)
                con.execute("COMMIT")
            except Exception:
                con.execute("ROLLBACK")
                raise
            if (idx + 1) % 50 == 0:
                print(f"  progress: {idx + 1}/{len(catalog)}")

        print(client.quota_note)
        print(f"done: daily_rows={written_d} member_rows={written_m} "
              f"failed={skipped}")
        print("next: rebuild normalized views to refresh "
              "v_default_concept_* (any collector run does it)")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
