"""Shared quality contract for THS concept snapshots."""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from typing import Any


# The expected catalogue size is source-controlled per snapshot.  A global
# floor would silently accept a truncated catalogue after the source changed.
THS_MIN_CONCEPTS: int | None = None
THS_MEMBERSHIP_MAX_AGE_DAYS = 7


def qualified_stock_reference(con, *, provider='xiaodefa', now=None):
    """Read the collector's sealed L/D reference; never acquire or repair it."""
    now = now or datetime.now()
    if not _table_exists(con, 'multi_source_observation') or not _table_exists(con, 'tushare_stock_basic'):
        return None
    receipt = con.execute("SELECT observed_at,payload_json,payload_hash FROM multi_source_observation "
        "WHERE data_type='tushare_stock_basic_snapshot' AND status='qualified' AND provider=? "
        "ORDER BY observed_at DESC LIMIT 1", [provider]).fetchone()
    if not receipt or not 0 <= (now - receipt[0]).total_seconds() < 86400:
        return None
    if hashlib.sha256(receipt[1].encode()).hexdigest() != receipt[2]:
        return None
    payload = json.loads(receipt[1])
    rows = con.execute('SELECT ts_code,stock_code,stock_name,area,industry,market,list_date,delist_date '
                       'FROM tushare_stock_basic ORDER BY ts_code').fetchall()
    version = hashlib.sha256(json.dumps(rows, ensure_ascii=False, default=str,
                                       separators=(',', ':')).encode()).hexdigest()
    if payload.get('version') != version or payload.get('scope') != ['L', 'D']:
        return None
    membership = payload.get('listing_membership', {})
    if membership and membership.get('as_of') != now.date().isoformat():
        return None
    return dict(version=version, known_at=receipt[0].isoformat(), max_age_seconds=86400,
                membership_date=membership.get('as_of'), not_listed=membership.get('not_listed', []),
                membership_only=membership.get('membership_only', []),
                historical_listing_dates_complete=membership.get('historical_listing_dates_complete', True))


def _table_exists(con: Any, name: str) -> bool:
    return bool(con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='main' AND table_name=?",
        [name],
    ).fetchone()[0])


def dated_exchange_membership(con, exchange, trade_date, *, now):
    """Read a complete retained official inventory, with no identity guessing.

    Absence is evidence only for that exchange/session and complete capture.
    A corrupt/newer receipt must not silently fall back to an older inventory.
    """
    contracts = {
        'SZ': ('szse', 'https://www.szse.cn/api/report/ShowReport', 1000),
        'SH': ('sse', 'https://query.sse.com.cn/sseQuery/commonQuery.do', 1000),
        'BJ': ('bse', 'https://www.bse.cn/nqxxController/nqxxCnzq.do', 100),
    }
    provider, source, floor = contracts[exchange]
    if not _table_exists(con, 'multi_source_observation'):
        return None
    receipt = con.execute(
        "SELECT observed_at,payload_json,payload_hash,status FROM multi_source_observation "
        "WHERE data_type=? AND provider=? AND observed_at<=? "
        "ORDER BY observed_at DESC LIMIT 1",
        [provider+'_listing_membership', provider, now]).fetchone()
    if (not receipt or receipt[3] != 'qualified'
            or receipt[0].date().isoformat() != trade_date
            or hashlib.sha256(receipt[1].encode()).hexdigest() != receipt[2]):
        return None
    def digest(value):
        return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)
    try:
        payload = json.loads(receipt[1])
        listings = payload['listings']
        if (payload.get('as_of') != trade_date or payload.get('source') != source
                or not isinstance(listings, dict) or len(listings) < floor
                or type(payload.get('recordcount')) is not int or payload['recordcount'] != len(listings)
                or any(len(code) != 6 or not code.isascii() or not code.isdigit() for code in listings)):
            return None
        if exchange == 'SZ':
            complete = digest(payload.get('xlsx_sha256')) and digest(payload.get('metadata_sha256'))
        elif exchange == 'SH':
            parts = payload.get('receipts', [])
            complete = (len(parts) == 2 and {p.get('board') for p in parts} == {'1', '8'}
                        and all(type(p.get('rows')) is int and p['rows'] > 0
                                and digest(p.get('sha256')) for p in parts)
                        and sum(p['rows'] for p in parts) == len(listings))
        else:
            pages = payload.get('pages', [])
            complete = (payload.get('source_session') == trade_date and bool(pages)
                        and [p.get('page') for p in pages] == list(range(len(pages)))
                        and all(digest(p.get('sha256')) for p in pages)
                        and 20*(len(pages)-1) < len(listings) <= 20*len(pages))
        if not complete:
            return None
        return dict(exchange=exchange, as_of=trade_date, codes=set(listings),
                    receipt_sha256=receipt[2], received_at=receipt[0].isoformat())
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def qualified_membership_snapshot(con: Any, trade_date: str):
    """Select only a quality-gated snapshot at or before the observation date.

    Return its date and age separately so consumers can explain stale input.
    Raw history is deliberately not a fallback for missing qualified views.
    """
    if not _table_exists(con, "v_default_concept_stock_history"):
        return None, None
    row = con.execute(
        "SELECT max(CAST(trade_date AS DATE)) FROM v_default_concept_stock_history "
        "WHERE CAST(trade_date AS DATE)<=CAST(? AS DATE)", [trade_date],
    ).fetchone()
    snapshot = row[0] if row else None
    if snapshot is None:
        return None, None
    return snapshot, (date.fromisoformat(str(trade_date)[:10]) - snapshot).days


def canonical_ths_snapshot(
    con: Any,
    as_of: str,
    *,
    exact_date: str | None = None,
    minimum_concepts: int | None = THS_MIN_CONCEPTS,
) -> dict[str, Any] | None:
    """Return the newest globally complete, same-date THS snapshot.

    A successful transport/checkpoint is not enough.  Every concept and member
    row must be date-verified, all rows must share one fetched date, every
    member checkpoint must succeed, and the catalogue must equal the expected
    count recorded for that snapshot.  This deliberately returns no fallback snapshot: callers
    must show an explicit unavailable/degraded state instead of mixing dates.
    """
    required = (
        "ths_concept_daily",
        "ths_concept_stock_history",
        "ths_concept_member_checkpoint",
        "ths_concept_snapshot_expectation",
    )
    if any(not _table_exists(con, name) for name in required):
        return None
    params: list[Any] = [as_of]
    date_filter = ""
    if exact_date:
        date_filter = " AND c.trade_date=CAST(? AS DATE)"
        params.append(exact_date)
    minimum_filter = " AND c.concept_count>=?" if minimum_concepts is not None else ""
    if minimum_concepts is not None:
        params.append(int(minimum_concepts))
    row = con.execute(
        f"""
        WITH concepts AS (
            SELECT c.trade_date,
                   count(DISTINCT c.concept_code) AS concept_count,
                   count(*) AS concept_rows,
                   sum(CASE WHEN coalesce(c.date_verified,false) THEN 1 ELSE 0 END) AS verified_concepts,
                   count(DISTINCT CASE WHEN json_valid(c.raw_json)
                       THEN json_extract_string(c.raw_json,'$.fetched_date') END) AS fetched_dates,
                   sum(CASE WHEN coalesce(c.stock_count,0)=0 THEN 1 ELSE 0 END) AS zero_concepts
            FROM ths_concept_daily c
            WHERE c.trade_date<=CAST(? AS DATE){date_filter}
            GROUP BY c.trade_date
        ), members AS (
            SELECT h.trade_date,
                   count(*) AS member_rows,
                   sum(CASE WHEN coalesce(h.date_verified,false) THEN 1 ELSE 0 END) AS verified_members,
                   count(DISTINCT CASE WHEN json_valid(h.raw_json)
                       THEN json_extract_string(h.raw_json,'$.fetched_date') END) AS fetched_dates
            FROM ths_concept_stock_history h
            GROUP BY h.trade_date
        ), checkpoints AS (
            SELECT trade_date,
                   count(DISTINCT concept_code) AS checkpoint_total,
                   sum(CASE WHEN status='success' THEN 1 ELSE 0 END) AS checkpoint_success
            FROM ths_concept_member_checkpoint
            GROUP BY trade_date
        ), expected AS (
            SELECT trade_date, expected_concepts
            FROM ths_concept_snapshot_expectation
            WHERE status IN ('success', 'bootstrap_observed')
        )
        SELECT CAST(c.trade_date AS VARCHAR), c.concept_count, m.member_rows,
               c.fetched_dates, m.fetched_dates, c.verified_concepts,
               m.verified_members, c.zero_concepts, cp.checkpoint_total,
               cp.checkpoint_success, e.expected_concepts
        FROM concepts c
        JOIN members m ON m.trade_date=c.trade_date
        JOIN checkpoints cp ON cp.trade_date=c.trade_date
        JOIN expected e ON e.trade_date=c.trade_date
        WHERE c.concept_count=e.expected_concepts{minimum_filter}
          AND c.fetched_dates=1 AND m.fetched_dates=1
          AND c.verified_concepts=c.concept_rows
          AND m.verified_members=m.member_rows
          AND c.zero_concepts=0
          AND cp.checkpoint_total=c.concept_count
          AND cp.checkpoint_success=c.concept_count
          AND m.member_rows>c.concept_count
        ORDER BY c.trade_date DESC
        LIMIT 1
        """,
        params,
    ).fetchone()
    if not row:
        return None
    snapshot_date = str(row[0])[:10]
    return {
        "snapshot_date": snapshot_date,
        "concepts": int(row[1] or 0),
        "members": int(row[2] or 0),
        "fetched_dates": int(row[3] or 0),
        "member_fetched_dates": int(row[4] or 0),
        "verified_concepts": int(row[5] or 0),
        "verified_members": int(row[6] or 0),
        "zero_concepts": int(row[7] or 0),
        "checkpoint_total": int(row[8] or 0),
        "checkpoint_success": int(row[9] or 0),
        "age_days": (date.fromisoformat(as_of) - date.fromisoformat(snapshot_date)).days,
    }
