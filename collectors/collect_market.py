"""Market sentiment and emotion data collectors."""
import json
import math
from decimal import Decimal
from datetime import datetime
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from trade_system.data_store import KPLClient, DuckDBStore, logger
from trade_system.market_contract import count_evidence, inherited_count_evidence, market_count, require_observed_counts, validate_requested_day

# After this many consecutive runs where KPL answers but returns no row for the
# requested trading date, escalate to an explicit alert (WP5).  At a 5-minute
# tick cadence ~6 runs is roughly half an hour of stale same-date market data.
KPL_STALE_ALERT_THRESHOLD = 6


def _track_kpl_stale(store, requested_date: str, got_same_date: bool) -> int:
    """Persist a per-date consecutive-stale counter; return the new count.

    A 'stale' run is one where KPL answered but returned no row for the requested
    trading date (it is still serving a previous session).  Escalating after a
    threshold lets operators distinguish an upstream that has not published
    today's data from a transient local failure.
    """
    try:
        store.conn.execute(
            "CREATE TABLE IF NOT EXISTS kpl_stale_tracker ("
            "date DATE PRIMARY KEY, consecutive_stale INTEGER, updated_at TIMESTAMP)"
        )
        # Pass the timestamp as a bound parameter and reference it via excluded.* in
        # the ON CONFLICT clause.  A literal ``current_timestamp`` there is parsed by
        # DuckDB as a *column name* ("does not have a column named current_timestamp"),
        # raising BinderException on every call -- which (silently) kept this table
        # empty and prevented the stale alert from ever firing.
        now = datetime.now()
        if got_same_date:
            store.conn.execute(
                "INSERT INTO kpl_stale_tracker(date,consecutive_stale,updated_at) "
                "VALUES (?,0,?) "
                "ON CONFLICT(date) DO UPDATE SET "
                "consecutive_stale=0, updated_at=excluded.updated_at",
                [requested_date, now],
            )
            return 0
        row = store.conn.execute(
            "SELECT consecutive_stale FROM kpl_stale_tracker WHERE date=?",
            [requested_date],
        ).fetchone()
        count = (int(row[0]) if row else 0) + 1
        store.conn.execute(
            "INSERT INTO kpl_stale_tracker(date,consecutive_stale,updated_at) "
            "VALUES (?,?,?) "
            "ON CONFLICT(date) DO UPDATE SET "
            "consecutive_stale=excluded.consecutive_stale, updated_at=excluded.updated_at",
            [requested_date, count, now],
        )
        return count
    except Exception as exc:
        # Surface failures (the original debug level hid the BinderException above).
        logger.warning(f"kpl_stale_tracker update failed: {exc}")
        return 0


def _require_daily_summary_schema(store) -> None:
    """Validate the schema before collection without changing database state.

    A historical bloat workaround used ``DROP COLUMN``/``ADD COLUMN`` in the
    collection path.  That made a normal market tick destructive and erased the
    raw audit payload.  Schema repair now belongs to an explicit migration; the
    production collector only checks that the required column exists and fails
    clearly when an old database needs migration.
    """
    columns = {
        str(row[1])
        for row in store.conn.execute("PRAGMA table_info('daily_summary')").fetchall()
    }
    required = {"date", "raw_json", "source_kind", "count_provenance_json"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(
            "daily_summary schema is incomplete; run the explicit schema migration "
            f"before collection (missing: {', '.join(missing)})"
        )


def collect_market_mood(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/market/mood")
    if not data:
        return 0
    try:
        validate_requested_day(data, date)
        require_observed_counts(data)
        row = (
            date,
            market_count(data.get("上涨家数")),
            market_count(data.get("下跌家数")),
            data.get("涨停家数"),
            data.get("跌停家数"),
            data.get("全市场流通量"),
            data.get("前日流通量"),
            data.get("涨跌比"),
            str(data.get("市场颜色", "")),
        )
        row = (*row, "real")
        with store.transaction():
            store.conn.execute("DELETE FROM market_mood WHERE date=?", [date])
            n = store.insert_rows("market_mood", [row],
                ["date", "rise_count", "fall_count", "limit_up_count", "limit_down_count",
                 "total_float", "prev_float", "rise_fall_ratio", "market_color", "source_kind"])
        store.log_collect("market_mood", "/market/mood", n, "ok")
        return n
    except Exception as e:
        logger.error(f"market_mood parse error: {e}")
        store.insert_raw("/market/mood", data)
        store.log_collect("market_mood", "/market/mood", 0, f"error: {e}")
        return 0


def collect_market_rise_fall(client: KPLClient, store: DuckDBStore, date: str) -> int:
    """Collect market rise/fall statistics."""
    if isinstance(client, KPLClient):
        data = client.get(
            "/market/rise-fall",
            {"date": date},
            accept_source_date=True,
        )
    else:
        data = client.get("/market/rise-fall", {"date": date})
    if not data:
        return 0
    
    try:
        # API returns dict with raw_data as list of daily stats
        if not isinstance(data, dict) or "raw_data" not in data:
            logger.warning(f"Unexpected response structure for market/rise-fall")
            return 0
        
        raw_data = data.get("raw_data", [])
        if not isinstance(raw_data, list) or len(raw_data) == 0:
            return 0
        
        # Each item in raw_data: [limit_up, limit_down, broken, real_limit_up, broken_rate, real_limit_down, date]
        rows = []
        current_found = False
        seen = set()
        for item in raw_data:
            if not isinstance(item, list) or len(item) < 7:
                raise ValueError('incomplete market rise/fall row')
            row_date = datetime.strptime(str(item[6])[:10], '%Y-%m-%d').date().isoformat()
            if row_date in seen:
                raise ValueError('duplicate market rise/fall source date')
            seen.add(row_date)
            counts = []
            for index in (0, 1, 2, 3, 5):
                number = Decimal(str(item[index]))
                if not number.is_finite() or number != number.to_integral_value() or number < 0:
                    raise ValueError('market counts must be finite nonnegative integers')
                counts.append(int(number))
            rate = float(item[4])
            if not math.isfinite(rate):
                raise ValueError('market rate must be finite')
            current_found = current_found or row_date == str(date)[:10]
            rows.append((row_date, *counts[:4], rate, counts[4], json.dumps(item, ensure_ascii=False)))

        if rows:
            historical_dates = sorted({str(row[0])[:10] for row in rows})
            placeholders = ",".join("?" for _ in historical_dates)
            with store.transaction():
                store.conn.execute(
                    f"DELETE FROM market_rise_fall WHERE CAST(date AS VARCHAR) IN ({placeholders})",
                    historical_dates,
                )
                store.conn.executemany("""
                    INSERT INTO market_rise_fall
                    (date, limit_up_count, limit_down_count, broken_limit_up_count,
                     blown_limit_up_count, blown_limit_up_rate, raw_field_5, raw_json, source_kind)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'real')
                """, rows)
                # Persist every response at its own source date.  Before the open,
                # KPL legitimately returns the previous completed session; losing
                # that real row left auction readiness with only a derived
                # fallback.  It still must not be relabelled as the requested day.
                for row in rows:
                    source_date = str(row[0])[:10]
                    # Select only scalar columns: DuckDB is columnar, so NOT
                    # selecting raw_json guarantees the (once multi-gigabyte) audit
                    # blob is never materialized -- this is what prevents the OOM.
                    received = datetime.now()
                    existing = store.conn.execute(
                        "SELECT rise_count,fall_count,consecutive_count,source_kind,fetched_at,count_provenance_json "
                        "FROM daily_summary WHERE CAST(date AS VARCHAR)=?",
                        [source_date],
                    ).fetchone()
                    mood = store.conn.execute(
                        "SELECT rise_count,fall_count,source_kind,fetched_at FROM market_mood "
                        "WHERE CAST(date AS VARCHAR)=? ORDER BY fetched_at DESC LIMIT 1",
                        [source_date],
                    ).fetchone()
                    evidence = {}
                    for field, value in zip(('limit_up_count', 'limit_down_count'), row[1:3]):
                        evidence[field] = count_evidence(value, source='kpl_market_rise_fall',
                            source_kind='real', source_date=source_date,
                            fetched_at=received, received_at=received)
                    for index, field in enumerate(('rise_count','fall_count','consecutive_count')):
                        # The latest mood response is authoritative for breadth even
                        # when partial/fallback. Never borrow an earlier good row
                        # to disguise an explicit bad observation as a success.
                        use_mood = index < 2 and mood is not None
                        if use_mood:
                            evidence[field] = count_evidence(mood[index], source='market_mood',
                                source_kind=mood[2], source_date=source_date,
                                fetched_at=mood[3], received_at=received)
                        else:
                            evidence[field] = inherited_count_evidence(existing[index] if existing else None,
                                field, provenance=existing[5] if existing else None,
                                source_kind=existing[3] if existing else None, source_date=source_date,
                                fetched_at=existing[4] if existing else None, received_at=received)
                    qualified = all(item['qualified'] for item in evidence.values())
                    source_kind = 'real' if qualified else 'real_partial'
                    # Every required constituent must be within a reader's TTL.
                    # min(receipt clocks) is equivalent to checking them all; a
                    # future/unknown clock already made the aggregate partial.
                    clocks = [datetime.fromisoformat(item['fetched_at']) for item in evidence.values()
                              if item['fetched_at'] is not None]
                    freshness_at = min(clocks) if clocks else None
                    audit_payload = {
                        'source': 'kpl_market_rise_fall', 'requested_date': str(date)[:10],
                        'source_date': source_date, 'kpl_raw': json.loads(row[7]),
                        'received_at': received.isoformat(timespec='microseconds'),
                        'count_evidence': evidence,
                    }
                    store.conn.execute(
                        "INSERT INTO daily_summary(date,limit_up_count,limit_down_count,rise_count,fall_count,consecutive_count,raw_json,fetched_at,source_kind,count_provenance_json) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(date) DO UPDATE SET limit_up_count=excluded.limit_up_count,limit_down_count=excluded.limit_down_count,"
                        "rise_count=excluded.rise_count,fall_count=excluded.fall_count,"
                        "consecutive_count=excluded.consecutive_count,"
                        "raw_json=excluded.raw_json,fetched_at=excluded.fetched_at,source_kind=excluded.source_kind,"
                        "count_provenance_json=excluded.count_provenance_json",
                        [source_date, *[item['value'] for item in evidence.values()],
                         json.dumps(audit_payload, ensure_ascii=False), freshness_at, source_kind,
                         json.dumps(evidence, ensure_ascii=False)],
                    )
            stale_count = _track_kpl_stale(store, str(date)[:10], current_found)
            if not current_found:
                logger.warning(
                    f"market/rise-fall returned no row for requested date {date}; "
                    "stored real source-date rows for previous-session context"
                )
                if stale_count >= KPL_STALE_ALERT_THRESHOLD:
                    logger.warning(
                        f"KPL_STALE_ALERT date={date} consecutive_stale={stale_count}: "
                        "KPL has returned no same-date market/rise-fall row for "
                        f"{stale_count} consecutive runs; current market context is "
                        "unavailable unless an independent source passes its own checks."
                    )
                    # Surface the alert in alert_events so the dashboard,
                    # daily review and trading terminal show it, not just the
                    # collector log (previously the only trace).
                    try:
                        store.conn.execute(
                            "CREATE TABLE IF NOT EXISTS alert_events ("
                            "trade_date VARCHAR, severity VARCHAR, category VARCHAR, "
                            "message VARCHAR, evidence_json VARCHAR, generated_at TIMESTAMP "
                            "DEFAULT current_timestamp)"
                        )
                        store.conn.execute(
                            "INSERT INTO alert_events (trade_date, severity, category, message) "
                            "VALUES (?, 'warning', 'kpl_stale', ?)",
                            [
                                str(date)[:10],
                                f"KPL market/rise-fall stale for {stale_count} consecutive runs; "
                                "current market context remains unverified",
                            ],
                        )
                    except Exception as exc:
                        logger.warning(f"alert_events insert failed: {exc}")
            logger.info(f"Inserted {len(rows)} rows into market_rise_fall")
            return len(rows)
    except Exception as e:
        logger.error(f"Error parsing market/rise-fall: {e}")
    
    return 0


def collect_market_limit_up_down(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/market/limit-up-down")
    if not data or not isinstance(data, list):
        return 0
    summary_rows = []
    rows = []
    for item in data:
        if not isinstance(item, dict):
            continue
        code = str(item.get("stock_code", item.get("code", "")) or "").strip()
        item_date = str(item.get("date") or item.get("日期") or '')[:10]
        if not item_date:
            continue
        # The current response is a dated aggregate.  Do not store it as a
        # stock row with an empty code.
        if not code and any(key in item for key in ("limit_up", "limit_down", "actual_limit_up", "actual_limit_down")):
            if item_date == str(date)[:10]:
                summary_rows.append((
                    item_date,
                    item.get("limit_up"),
                    item.get("limit_down"),
                    item.get("actual_limit_up"),
                    item.get("actual_limit_down"),
                    item.get("blown_limit_up_rate", item.get("blown_limit_up")),
                    json.dumps(item, ensure_ascii=False),
                ))
            continue
        if not code:
            continue
        rows.append((
            item_date,
            code,
            item.get("stock_name", item.get("name", "")),
            item.get("change_pct", item.get("涨跌幅", 0)),
            item.get("turnover", item.get("成交额", 0)),
            item.get("market_cap", item.get("总市值", 0)),
            bool(item.get("is_limit_up", False)),
            bool(item.get("is_limit_down", False)),
            bool(item.get("is_blown", item.get("broken", False))),
        ))
    total = 0
    if summary_rows:
        store.conn.execute("""
            CREATE TABLE IF NOT EXISTS market_limit_up_down_summary (
                date DATE PRIMARY KEY,
                limit_up_count INTEGER,
                limit_down_count INTEGER,
                actual_limit_up_count INTEGER,
                actual_limit_down_count INTEGER,
                blown_limit_up_rate DOUBLE,
                raw_json VARCHAR,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """)
        with store.transaction():
            store.conn.execute("DELETE FROM market_limit_up_down_summary WHERE date=?", [date])
            total += store.insert_rows("market_limit_up_down_summary", summary_rows,
                ["date", "limit_up_count", "limit_down_count", "actual_limit_up_count",
                 "actual_limit_down_count", "blown_limit_up_rate", "raw_json"],
                replace_on=["date"])
    if rows:
        n = store.insert_rows("market_limit_up_down", rows,
            ["date", "stock_code", "stock_name", "change_pct", "turnover",
             "market_cap", "is_limit_up", "is_limit_down", "is_blown"],
            replace_on=["date", "stock_code"])
        total += n
    if total:
        store.log_collect("market_limit_up_down", "/market/limit-up-down", total, "ok")
    return total


def collect_emotion_money_date(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/market/emotion-money-date")
    if not data:
        return 0
    try:
        zy_list = data.get("ZY", [])
        rows = []
        for item in zy_list:
            if isinstance(item, dict):
                rows.append((
                    item.get("Day", date),
                    item.get("CGL", 0),
                    item.get("YLL", 0),
                    item.get("CGL", 0),
                    json.dumps(item, ensure_ascii=False),
                ))
            elif isinstance(item, (list, tuple)) and len(item) >= 3:
                rows.append((str(item[0]), item[1], item[2], item[1], json.dumps(item)))
        if rows:
            n = store.insert_rows("market_emotion_money", rows,
                ["date", "cgl", "yll", "success_rate", "raw_json"])
            store.log_collect("market_emotion_money", "/market/emotion-money-date", n, "ok")
            return n
        store.insert_raw("/market/emotion-money-date", data)
        return 0
    except Exception as e:
        logger.error(f"emotion_money_date error: {e}")
        store.insert_raw("/market/emotion-money-date", data)
        return 0


def collect_emotion_money_detail(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/market/emotion-money-detail")
    if not data:
        return 0
    try:
        detail = data.get("Detail", data.get("detail", {}))
        rows = []
        if isinstance(detail, dict):
            for k, v in detail.items():
                rows.append((date, str(k), v if isinstance(v, (int, float)) else str(v),
                             json.dumps({k: v}, ensure_ascii=False)))
        if rows:
            n = store.insert_rows("market_emotion_detail", rows,
                ["date", "metric_name", "metric_value", "raw_json"])
            store.log_collect("market_emotion_detail", "/market/emotion-money-detail", n, "ok")
            return n
        store.insert_raw("/market/emotion-money-detail", data)
        return 0
    except Exception as e:
        logger.error(f"emotion_money_detail error: {e}")
        store.insert_raw("/market/emotion-money-detail", data)
        return 0


def collect_all_market(client: KPLClient, store: DuckDBStore, date: str) -> dict:
    results = {}
    _require_daily_summary_schema(store)
    results["market_mood"] = collect_market_mood(client, store, date)
    results["market_rise_fall"] = collect_market_rise_fall(client, store, date)
    results["market_limit_up_down"] = collect_market_limit_up_down(client, store, date)
    results["emotion_money_date"] = collect_emotion_money_date(client, store, date)
    results["emotion_money_detail"] = collect_emotion_money_detail(client, store, date)
    return results


def main():
    """Bounded market context only; other products use their existing phase tasks."""
    import argparse
    from trade_system.schema import init_schema
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    store = DuckDBStore(args.db)
    try:
        init_schema(store.conn)
        client = KPLClient(max_attempts=2, total_budget_seconds=45)
        result = collect_all_market(client, store, args.date)
        current = {table: store.fetchall(
            f'SELECT count(*) FROM {table} WHERE CAST(date AS VARCHAR)=?', [args.date])[0][0]
            for table in ('daily_summary', 'market_rise_fall')}
        dates = {table: str(store.fetchall(f'SELECT max(date) FROM {table}')[0][0]) for table in current}
        qualified = all(current.values())
        print(json.dumps({'rows': result, 'provider_stats': client.stats, 'qualified': qualified,
                          'requested_date': args.date, 'latest_source_dates': dates,
                          'missing_current': [table for table, count in current.items() if not count]}))
        return 0 if qualified and client.stats['success'] else 2
    finally:
        store.close()


if __name__ == '__main__':
    raise SystemExit(main())
