"""Market sentiment and emotion data collectors."""
import json
from datetime import datetime
from base import KPLClient, DuckDBStore, logger

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


def _self_heal_daily_summary_bloat(store) -> None:
    """One-time purge of the frozen oversized daily_summary.raw_json blob (WP0).

    The old nesting bug grew a single raw_json value to multi-gigabyte scale.  A
    value that large cannot be allocated in memory, so ANY query that materializes
    it -- a ``SELECT raw_json``, a ``length(raw_json)`` filter, or an UPDATE that
    reads the old value -- fails with "Out of Memory Error: Allocation failure".
    We must therefore never read it.  Dropping and re-adding the column is a
    columnar metadata operation that discards the column's data segments without
    materializing the giant value (validated ~instant regardless of blob size), so
    it purges the blob safely.  Runs once per database (guarded by a marker row);
    future runs write a bounded raw_json (see collect_market_rise_fall).
    """
    try:
        store.conn.execute(
            "CREATE TABLE IF NOT EXISTS _bloat_cleanup_done ("
            "id INTEGER PRIMARY KEY, done_at TIMESTAMP)"
        )
        if store.conn.execute("SELECT 1 FROM _bloat_cleanup_done WHERE id=1").fetchone():
            return
        store.conn.execute("ALTER TABLE daily_summary DROP COLUMN raw_json")
        store.conn.execute("ALTER TABLE daily_summary ADD COLUMN raw_json VARCHAR")
        store.conn.execute("INSERT INTO _bloat_cleanup_done VALUES (1, current_timestamp)")
        logger.warning(
            "daily_summary bloat self-heal: dropped and recreated raw_json column "
            "to purge the frozen oversized blob (audit history reset; scalar data intact)"
        )
    except Exception as exc:
        logger.warning(f"daily_summary bloat self-heal skipped: {exc}")
        # Best effort: guarantee raw_json exists so later inserts cannot fail.
        try:
            store.conn.execute(
                "ALTER TABLE daily_summary ADD COLUMN IF NOT EXISTS raw_json VARCHAR"
            )
        except Exception:
            pass


def collect_market_mood(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/market/mood")
    if not data:
        return 0
    try:
        row = (
            date,
            data.get("上涨家数"),
            data.get("下跌家数"),
            data.get("涨停家数"),
            data.get("跌停家数"),
            data.get("全市场流通量"),
            data.get("前日流通量"),
            data.get("涨跌比"),
            str(data.get("市场颜色", "")),
        )
        row = (*row, "real")
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
        for item in raw_data:
            if isinstance(item, list) and len(item) >= 7:
                row_date = item[6]
                if str(row_date)[:10] == str(date)[:10]:
                    current_found = True
                rows.append((
                    row_date,
                    item[0],  # limit_up_count
                    item[1],  # limit_down_count
                    item[2],  # broken_count
                    item[3],  # real_limit_up_count
                    item[4],  # broken_rate
                    item[5],  # real_limit_down_count
                    json.dumps(item, ensure_ascii=False)
                ))
        
        if rows:
            historical_dates = sorted({str(row[0])[:10] for row in rows})
            placeholders = ",".join("?" for _ in historical_dates)
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
            try:
                for row in rows:
                    source_date = str(row[0])[:10]
                    # Select only scalar columns: DuckDB is columnar, so NOT
                    # selecting raw_json guarantees the (once multi-gigabyte) audit
                    # blob is never materialized -- this is what prevents the OOM.
                    existing = store.conn.execute(
                        "SELECT rise_count,fall_count,consecutive_count "
                        "FROM daily_summary WHERE CAST(date AS VARCHAR)=?",
                        [source_date],
                    ).fetchone()
                    mood = store.conn.execute(
                        "SELECT rise_count,fall_count FROM market_mood "
                        "WHERE CAST(date AS VARCHAR)=? ORDER BY fetched_at DESC LIMIT 1",
                        [source_date],
                    ).fetchone()
                    rise_count = mood[0] if mood else existing[0] if existing else None
                    fall_count = mood[1] if mood else existing[1] if existing else None
                    consecutive_count = existing[2] if existing else None
                    source_kind = (
                        "real"
                        if source_date == str(date)[:10] and mood
                        else "real_cross_source"
                        if existing or mood
                        else "real_partial"
                    )
                    # Preserve only the prior scalar snapshot (bounded).  Never read
                    # or re-embed the previous raw_json blob (the old nesting bomb).
                    preserved = (
                        {
                            "rise_count": existing[0],
                            "fall_count": existing[1],
                            "consecutive_count": existing[2],
                        }
                        if existing
                        else None
                    )
                    audit_payload = {
                        "source": "kpl_market_rise_fall",
                        "requested_date": str(date)[:10],
                        "source_date": source_date,
                        "kpl_raw": json.loads(row[7]),
                        "preserved_daily_summary": preserved,
                    }
                    store.conn.execute(
                        "INSERT INTO daily_summary(date,limit_up_count,limit_down_count,rise_count,fall_count,consecutive_count,raw_json,fetched_at,source_kind) "
                        "VALUES (?,?,?,?,?,?,?,current_timestamp,?) "
                        "ON CONFLICT(date) DO UPDATE SET limit_up_count=excluded.limit_up_count,limit_down_count=excluded.limit_down_count,"
                        "rise_count=coalesce(excluded.rise_count,daily_summary.rise_count),fall_count=coalesce(excluded.fall_count,daily_summary.fall_count),"
                        "consecutive_count=coalesce(excluded.consecutive_count,daily_summary.consecutive_count),"
                        "raw_json=excluded.raw_json,fetched_at=excluded.fetched_at,source_kind=excluded.source_kind",
                        [
                            source_date, row[1], row[2], rise_count, fall_count,
                            consecutive_count,
                            json.dumps(audit_payload, ensure_ascii=False),
                            source_kind,
                        ],
                    )
            except Exception as exc:
                logger.warning(f"daily_summary fallback from market/rise-fall failed: {exc}")
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
                        f"{stale_count} consecutive runs; market state is relying on the "
                        "Eastmoney-derived fallback. Check the KPL upstream feed."
                    )
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
        item_date = str(item.get("date") or date)[:10]
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
        store.conn.execute("DELETE FROM market_limit_up_down_summary WHERE date=?", [date])
        total += store.insert_rows("market_limit_up_down_summary", summary_rows,
            ["date", "limit_up_count", "limit_down_count", "actual_limit_up_count",
             "actual_limit_down_count", "blown_limit_up_rate", "raw_json"])
    if rows:
        n = store.insert_rows("market_limit_up_down", rows,
            ["date", "stock_code", "stock_name", "change_pct", "turnover",
             "market_cap", "is_limit_up", "is_limit_down", "is_blown"])
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
    # WP0 self-heal: trim any frozen oversized raw_json left by the old nesting bug
    # BEFORE the per-row reads below, so this tick does not re-read a multi-MB blob.
    _self_heal_daily_summary_bloat(store)
    results["market_mood"] = collect_market_mood(client, store, date)
    results["market_rise_fall"] = collect_market_rise_fall(client, store, date)
    results["market_limit_up_down"] = collect_market_limit_up_down(client, store, date)
    results["emotion_money_date"] = collect_emotion_money_date(client, store, date)
    results["emotion_money_detail"] = collect_emotion_money_detail(client, store, date)
    return results
