"""L2 data collectors (11 endpoints)."""
from datetime import datetime, timezone
import math
from zoneinfo import ZoneInfo

from trade_system.data_store import KPLClient, DuckDBStore, logger


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _intraday_main_net(point: dict) -> float:
    return _to_float(
        point.get(
            "main_net_inflow",
            point.get("main_fund_net", point.get("主力净额", 0)),
        )
    )


def _extract_index_rows(data, date: str) -> list[tuple[str, str, str, float, float]]:
    if not data:
        return []
    if isinstance(data, dict):
        indices = data.get("indexes")
        if indices is None:
            indices = data.get("data", data.get("indices", []))
    elif isinstance(data, list):
        indices = data
    else:
        return []

    rows = []
    for idx in indices or []:
        if not isinstance(idx, dict):
            continue
        code = str(idx.get("index_code") or idx.get("stock_id") or idx.get("code") or "")
        if not code:
            continue
        rows.append(
            (
                date,
                code,
                idx.get("index_name") or idx.get("name") or "",
                _to_float(idx.get("price", idx.get("value", idx.get("价格", 0)))),
                _to_float(idx.get("change_pct", idx.get("涨跌幅", 0))),
            )
        )
    return rows


def _items(data, *keys: str) -> list:
    if not data:
        return []
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys + ("data", "items", "list"):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if any(key in data for key in ("time", "price", "volume")):
            return [data]
    return []


def _field(item: dict, *names, default=None):
    for name in names:
        if item.get(name) is not None:
            return item.get(name)
    return default


def _compact_date(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())[:8]


def _matches_trade_date(item: dict, date: str) -> bool:
    value = _field(item, "date", "trade_date", "day", "dt", "datetime", default=None)
    if value is None:
        return True
    compact = _compact_date(value)
    return len(compact) < 8 or compact == _compact_date(date)


def _response_matches_trade_date(data, date: str) -> bool:
    return not isinstance(data, dict) or _matches_trade_date(data, date)


def _tick_source_date(item, *, allow_epoch=True) -> str | None:
    """Use a returned date/epoch, never the requested date or receipt clock."""
    if not isinstance(item, dict):
        return None
    value = _field(item, "date", "trade_date", "day", "dt", "datetime")
    if value is not None:
        compact = _compact_date(value)
        if len(compact) != 8:
            return None
        try:
            return datetime.strptime(compact, "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            return None
    stamp = item.get("timestamp")
    if allow_epoch and stamp is not None and str(stamp).isdigit() and len(str(stamp)) in (10, 13):
        try:
            seconds = int(stamp) / (1000 if len(str(stamp)) == 13 else 1)
            return datetime.fromtimestamp(seconds, timezone.utc).astimezone(
                ZoneInfo("Asia/Shanghai")
            ).strftime("%Y%m%d")
        except (ValueError, OverflowError, OSError):
            return None
    return None


def _tick_matches_security(item, code: str) -> bool:
    if not isinstance(item, dict):
        return True
    value = _field(item, "stock_code", "stock_id", "code")
    if value is None:
        return True
    identity = str(value).upper().split(".")[0]
    if identity.startswith(("SH", "SZ", "BJ")):
        identity = identity[2:]
    return identity == str(code)


def _tick_time(value) -> str | None:
    for pattern in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value), pattern).strftime("%H:%M:%S")
        except ValueError:
            continue
    return None


def _collect_tick_projection(store, table_name, direction_column, date, code, data):
    """Old tables can represent only one unambiguous observation per second.

    The complete receipt is the event archive.  Collision groups, undated
    observations and conflicting old rows stay there instead of overwriting
    curated history or inventing an order identifier for the old projection.
    """
    columns = ["date", "stock_code", "time", "price", "volume", direction_column]
    source_date = _tick_source_date(data, allow_epoch=False)
    requested_date = _compact_date(date)
    if not _tick_matches_security(data, code):
        return [], {"response_security_mismatch"}
    if isinstance(data, dict) and any(
        data.get(key) is not None for key in ("date", "trade_date", "day", "dt", "datetime")
    ) and source_date is None:
        return [], {"invalid_response_date"}
    if source_date is not None and source_date != requested_date:
        return [], {"response_date_mismatch"}
    groups = {}
    issues = set()
    for item in _items(data, "ticks", "orders", "history"):
        item_date = _tick_source_date(item)
        if isinstance(item, dict) and any(
            item.get(key) is not None for key in ("date", "trade_date", "day", "dt", "datetime", "timestamp")
        ) and item_date is None:
            issues.add("invalid_source_date")
            continue
        if (item_date or source_date) != requested_date:
            issues.add("source_date_unverified_or_mismatch")
            continue
        if not _tick_matches_security(item, code):
            issues.add("row_security_mismatch")
            continue
        if isinstance(item, dict):
            point_time = _field(item, "time", "t")
            price = _field(item, "price", "p")
            volume = _field(item, "volume", "v")
            direction = _field(item, direction_column, "direction", "order_type", "side", "type")
        elif isinstance(item, (list, tuple)) and len(item) >= 3:
            point_time, price, volume = item[:3]
            direction = item[3] if len(item) > 3 else None
        else:
            issues.add("invalid_row")
            continue
        point_time = _tick_time(point_time)
        # Count every observation before validating its projection. Two orders
        # with identical values still are not one event, and unknown duplicates
        # do not prove a safe deduplication rule.
        groups.setdefault(point_time, []).append(None)
        try:
            if isinstance(price, bool) or isinstance(volume, bool):
                raise ValueError("boolean numeric field")
            price, volume = float(price), float(volume)
            if not point_time or not math.isfinite(price) or price <= 0:
                raise ValueError("invalid price/time")
            if not math.isfinite(volume) or volume < 0 or not volume.is_integer():
                raise ValueError("invalid volume")
        except (TypeError, ValueError, OverflowError):
            issues.add("invalid_projection_fields")
            continue
        groups[point_time][-1] = (
            date, code, point_time, price, int(volume),
            str(direction) if direction is not None else None,
        )
    existing = {}
    if store.fetchall("SELECT count(*) FROM information_schema.tables WHERE table_name=?", [table_name])[0][0]:
        for row in store.fetchall(
            f"SELECT {','.join(columns)} FROM {table_name} WHERE date=CAST(? AS DATE) AND stock_code=?",
            [date, code],
        ):
            existing.setdefault(_tick_time(row[2]), []).append((date, code, *row[2:]))
    rows = []
    for point_time, candidates in groups.items():
        if len(candidates) != 1:
            issues.add("same_time_events_retained_in_raw")
            continue
        row = candidates[0]
        if row is None:
            continue
        previous = existing.get(point_time, [])
        if previous and (len(previous) != 1 or previous[0] != row):
            issues.add("existing_projection_conflict_retained_in_raw")
            continue
        rows.append(row)
    return rows, issues


def collect_l2_stock_intraday(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    total = 0
    empty_streak = 0
    for code in stock_codes:
        # Endpoint already cooling: do not walk the rest of the universe.
        cooldown_until = getattr(client, "_cooldown_until", {}).get("/l2/stock-intraday")
        if cooldown_until and cooldown_until > __import__("time").time():
            logger.info(
                "L2 stock-intraday in cooldown after empty streak; "
                "stopping early (%s codes remaining)",
                max(0, len(stock_codes) - stock_codes.index(code)),
            )
            break
        data = client.get("/l2/stock-intraday", {"code": code, "date": date})
        if not data or not _response_matches_trade_date(data, date):
            empty_streak += 1
            # Two consecutive empties → let the caller fall back (trends2)
            # instead of burning the host budget on a dead route.
            if empty_streak >= 2:
                logger.info(
                    "L2 stock-intraday empty for %s consecutive codes; early stop",
                    empty_streak,
                )
                break
            continue
        empty_streak = 0
        intraday = data if isinstance(data, list) else data.get("data", data.get("intraday", []))
        rows = []
        for pt in intraday:
            if isinstance(pt, dict):
                if not _matches_trade_date(pt, date):
                    continue
                rows.append((
                    date, code,
                    str(pt.get("time", pt.get("t", ""))),
                    pt.get("price", pt.get("p", 0)),
                    pt.get("avg_price", pt.get("avg", 0)),
                    pt.get("volume", pt.get("v", 0)),
                    pt.get("turnover", pt.get("amount", 0)),
                    _intraday_main_net(pt),
                ))
            elif isinstance(pt, (list, tuple)) and len(pt) >= 4:
                rows.append((
                    date, code, str(pt[0]), pt[1], pt[2], pt[3],
                    pt[4] if len(pt) > 4 else 0,
                    pt[5] if len(pt) > 5 else 0,
                ))
        if rows:
            n = store.insert_rows("l2_stock_intraday", rows,
                ["date", "stock_code", "time", "price", "avg_price", "volume", "turnover", "main_fund_net"])
            total += n
        else:
            empty_streak += 1
            if empty_streak >= 2:
                break
    if total:
        store.log_collect("l2_stock_intraday", "/l2/stock-intraday", total, "ok")
    return total


def collect_l2_stock_bigorder(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    """API returns {stock_code, date, big_order_buy_total, big_order_sell_total, data: [{time, big_order_net, ...}]}."""
    total = 0
    for code in stock_codes:
        data = client.get("/l2/stock-bigorder", {"code": code, "date": date})
        if not data or not _response_matches_trade_date(data, date):
            continue
        # The API response is a dict with "data" as a list of time-series entries
        if isinstance(data, dict):
            orders = data.get("data", [])
            api_date = date
        elif isinstance(data, list):
            orders = data
            api_date = date
        else:
            continue
        rows = []
        for o in orders:
            if isinstance(o, dict):
                if not _matches_trade_date(o, date):
                    continue
                rows.append((
                    api_date, code,
                    str(o.get("time", o.get("t", ""))),
                    o.get("big_order_net", o.get("大单净额", 0)),
                    o.get("intraday_buy", o.get("大单买入", 0)),
                    o.get("intraday_sell", o.get("大单卖出", 0)),
                ))
        if rows:
            n = store.insert_rows("l2_stock_bigorder", rows,
                ["date", "stock_code", "time", "big_net_amount", "big_buy", "big_sell"])
            total += n
    if total:
        store.log_collect("l2_stock_bigorder", "/l2/stock-bigorder", total, "ok")
    return total


def collect_l2_sector_intraday(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:50]:
        data = client.get("/l2/sector-intraday", {"code": code, "date": date})
        if not data or not _response_matches_trade_date(data, date):
            continue
        intraday = data if isinstance(data, list) else data.get("data", data.get("intraday", []))
        rows = []
        for pt in intraday:
            if isinstance(pt, dict):
                if not _matches_trade_date(pt, date):
                    continue
                rows.append((
                    date, code,
                    str(pt.get("time", pt.get("t", ""))),
                    pt.get("price", pt.get("p", 0)),
                    pt.get("volume", pt.get("v", 0)),
                    pt.get("turnover", pt.get("amount", 0)),
                ))
        if rows:
            n = store.insert_rows("l2_sector_intraday", rows,
                ["date", "sector_code", "time", "price", "volume", "turnover"])
            total += n
    if total:
        store.log_collect("l2_sector_intraday", "/l2/sector-intraday", total, "ok")
    return total


def collect_l2_realtime_all_boards(client: KPLClient, store: DuckDBStore, date: str) -> int:
    from collectors.collect_ladder import _collect_realtime_boards
    return _collect_realtime_boards(client, store, date, grouped=True)


def collect_l2_realtime_index_list(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/l2/realtime/index-list")
    if not data:
        return 0
    rows = _extract_index_rows(data, date)
    if rows:
        n = store.insert_rows("l2_realtime_index_list", rows,
            ["date", "index_code", "index_name", "price", "change_pct"])
        store.log_collect("l2_realtime_index_list", "/l2/realtime/index-list", n, "ok")
        return n
    store.insert_raw("/l2/realtime/index-list", data)
    return 0


def collect_l2_realtime_index_trend(client: KPLClient, store: DuckDBStore, date: str) -> int:
    data = client.get("/l2/realtime/index-trend")
    rows = []
    for item in _items(data, "trend", "points"):
        if not isinstance(item, dict):
            continue
        rows.append(
            (
                date,
                str(_field(item, "index_code", "stock_id", "code", default="SH000001")),
                str(_field(item, "time", "t", "timestamp", default="")),
                _to_float(_field(item, "price", "value", "p", default=0)),
                int(_to_float(_field(item, "volume", "v", default=0))),
            )
        )
    if rows:
        n = store.insert_rows(
            "l2_realtime_index_trend",
            rows,
            ["date", "index_code", "time", "price", "volume"],
            replace_on=["date", "index_code", "time"],
        )
        store.log_collect("l2_realtime_index_trend", "/l2/realtime/index-trend", n, "ok")
        return n
    if data:
        store.insert_raw("/l2/realtime/index-trend", data)
    return 0


def collect_l2_sector_volume(client: KPLClient, store: DuckDBStore, date: str, sector_codes: list) -> int:
    total = 0
    for code in sector_codes[:30]:
        data = client.get("/l2/sector-volume", {"code": code})
        if not _response_matches_trade_date(data, date):
            continue
        rows = []
        for item in _items(data, "points", "volumes"):
            if not isinstance(item, dict):
                continue
            if not _matches_trade_date(item, date):
                continue
            rows.append(
                (
                    date,
                    code,
                    str(_field(item, "time", "t", "timestamp", default="")),
                    int(_to_float(_field(item, "volume", "v", default=0))),
                    int(_to_float(_field(item, "turnover", "amount", default=0))),
                )
            )
        if rows:
            total += store.insert_rows(
                "l2_sector_volume",
                rows,
                ["date", "sector_code", "time", "volume", "turnover"],
                replace_on=["date", "sector_code", "time"],
            )
    if total:
        store.log_collect("l2_sector_volume", "/l2/sector-volume", total, "ok")
    return total


def _collect_l2_tick_like(
    client: KPLClient,
    store: DuckDBStore,
    *,
    endpoint: str,
    table_name: str,
    date: str,
    stock_codes: list,
    direction_column: str,
) -> int:
    total = 0
    for code in stock_codes[:30]:
        params = {"code": code, "date": date}
        requested_at = datetime.now(timezone.utc).isoformat()
        observations = 0

        def retain_decoded_response(decoded, observation):
            nonlocal observations
            # The real client invokes this before semantic rejection. Preserve
            # IDs/flags/repeated rows without giving the payload return eligibility.
            # This is decoded evidence, not a byte-identical HTTP body.
            store.insert_raw(endpoint, {
                "schema": "kpl-l2-decoded-response-v1",
                "provider": "kpl",
                "endpoint": endpoint,
                "request_params": observation["request_params"],
                "requested_trade_date": date,
                "requested_at": observation["requested_at"],
                "response_observed_at": observation["response_observed_at"],
                "source_trade_date": _tick_source_date(decoded, allow_epoch=False),
                "arrival_time_basis": observation["arrival_time_basis"],
                "payload_kind": "complete_client_decoded_return_not_http_bytes",
                "response": decoded,
                "qualification": {
                    "original_producer_verified": False,
                    "definition_verified": False,
                    "pagination_complete": None,
                    "session_complete": None,
                    "same_definition_independent_funds_eligible": False,
                    "standard_table_role": "legacy_unambiguous_observation_projection",
                },
            })
            observations += 1

        if isinstance(client, KPLClient):
            data = client.get(endpoint, dict(params), decoded_observer=retain_decoded_response)
        else:
            # Existing simple FakeClients do not implement the client callback;
            # do not retry a get() after TypeError or introduce a second request.
            data = client.get(endpoint, dict(params))
        if data is None:
            if observations:
                store.log_collect(table_name, endpoint, 0, "semantic_response_rejected_raw_retained")
            continue
        if not observations:
            retain_decoded_response(data, {
                "request_params": dict(params),
                "requested_at": requested_at,
                "response_observed_at": datetime.now(timezone.utc).isoformat(),
                "arrival_time_basis": "immediate_observation_of_client_return_not_wire_receipt",
            })
        rows, issues = _collect_tick_projection(
            store, table_name, direction_column, date, code, data
        )
        written = 0
        try:
            if rows:
                written = store.insert_rows(
                    table_name,
                    rows,
                    ["date", "stock_code", "time", "price", "volume", direction_column],
                    replace_on=["date", "stock_code", "time"],
                )
        except Exception:
            store.log_collect(table_name, endpoint, 0, "projection_failed_raw_retained")
            raise
        total += written
        if written < len(rows):
            issues.add("projection_write_incomplete_raw_retained")
        status = "unqualified_definition_and_page_scope"
        if issues:
            status += ":" + ",".join(sorted(issues))
        store.log_collect(table_name, endpoint, written, status)
    return total


def collect_l2_tick_history(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    return _collect_l2_tick_like(
        client,
        store,
        endpoint="/l2/tick-history",
        table_name="l2_tick_history",
        date=date,
        stock_codes=stock_codes,
        direction_column="direction",
    )


def collect_l2_tick_orders(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    return _collect_l2_tick_like(
        client,
        store,
        endpoint="/l2/tick-orders",
        table_name="l2_tick_orders",
        date=date,
        stock_codes=stock_codes,
        direction_column="order_type",
    )


def collect_l2_tick_orders_all(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list) -> int:
    return _collect_l2_tick_like(
        client,
        store,
        endpoint="/l2/tick-orders-all",
        table_name="l2_tick_orders_all",
        date=date,
        stock_codes=stock_codes,
        direction_column="order_type",
    )


def collect_all_l2(client: KPLClient, store: DuckDBStore, date: str, stock_codes: list, sector_codes: list) -> dict:
    results = {}
    results["l2_realtime_all_boards"] = collect_l2_realtime_all_boards(client, store, date)
    results["l2_realtime_index_list"] = collect_l2_realtime_index_list(client, store, date)
    results["l2_realtime_index_trend"] = collect_l2_realtime_index_trend(client, store, date)
    # L2 per-stock data (limit to top 100 to avoid too many requests)
    active_stocks = stock_codes[:100]
    results["l2_stock_intraday"] = collect_l2_stock_intraday(client, store, date, active_stocks)
    results["l2_stock_bigorder"] = collect_l2_stock_bigorder(client, store, date, active_stocks)
    # Tick/order endpoints can be large; use a bounded operator sample.
    tick_stocks = stock_codes[:30]
    results["l2_tick_history"] = collect_l2_tick_history(client, store, date, tick_stocks)
    results["l2_tick_orders"] = collect_l2_tick_orders(client, store, date, tick_stocks)
    results["l2_tick_orders_all"] = collect_l2_tick_orders_all(client, store, date, tick_stocks)
    # L2 per-sector data
    results["l2_sector_intraday"] = collect_l2_sector_intraday(client, store, date, sector_codes)
    results["l2_sector_volume"] = collect_l2_sector_volume(client, store, date, sector_codes)
    return results
