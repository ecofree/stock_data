"""HTTP client and DuckDB store for KPL data collection."""
import json
import time
import logging
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime

import duckdb

from config import (
    API_BASE, API_KEY, DB_PATH, REQUEST_TIMEOUT,
    REQUEST_DELAY, MAX_RETRIES, RETRY_DELAY, LOG_DIR, TODAY,
    RATE_LIMIT_ENDPOINTS, RATE_LIMIT_DELAY, MAX_EMPTY_RETRIES,
    DUCKDB_MEMORY_LIMIT, DUCKDB_THREADS, DUCKDB_TEMP_DIR,
)
from trade_system.host_limiter import shared_host_limiter
from trade_system.source_validation import validate_kpl

logger = logging.getLogger("kpl_collector")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(
            os.path.join(LOG_DIR, f"collect_{TODAY}.log"),
            encoding="utf-8",
        ),
    ],
)


def connect_duckdb(db_path=None, *, read_only=False):
    """Open DuckDB with bounded resources (WP2).

    DuckDB otherwise defaults to ~80% of system RAM, every core, and no spill
    directory.  With several concurrent collectors sharing the multi-GB database
    that oversubscribes memory and surfaces as "Out of Memory Error: Allocation
    failure".  Limits are env-tunable via config (KPL_DUCKDB_MEMORY_LIMIT /
    KPL_DUCKDB_THREADS / KPL_DUCKDB_TEMP_DIR).
    """
    path = db_path or DB_PATH
    try:
        os.makedirs(DUCKDB_TEMP_DIR, exist_ok=True)
    except Exception:
        pass
    config = {
        "memory_limit": DUCKDB_MEMORY_LIMIT,
        "threads": DUCKDB_THREADS,
        "temp_directory": DUCKDB_TEMP_DIR,
    }
    try:
        return duckdb.connect(path, read_only=read_only, config=config)
    except TypeError:
        # Older duckdb without the config kwarg: fall back to a bare connection.
        return duckdb.connect(path, read_only=read_only)


class KPLClient:
    """HTTP client with rate-limiting, retry, empty-response detection, and exponential backoff."""

    def __init__(
        self,
        *,
        request_timeout=None,
        max_attempts=None,
        total_budget_seconds=None,
    ):
        self.session = None
        self.request_timeout = float(
            REQUEST_TIMEOUT if request_timeout is None else request_timeout
        )
        self.max_attempts = max(1, int(MAX_RETRIES if max_attempts is None else max_attempts))
        self.total_budget_seconds = (
            None if total_budget_seconds is None else max(1.0, float(total_budget_seconds))
        )
        self._started_at = time.monotonic()
        self._circuit_open_reason = None
        self._last_request_time = 0
        self._empty_streaks = {}   # track consecutive empty responses per endpoint
        self._cooldown_until = {}  # endpoint -> timestamp when cooldown ends
        self.stats = {
            "success": 0,
            "error": 0,
            "empty": 0,
            "semantic_error": 0,
            "rate_limited": 0,
            "skipped": 0,
            "circuit_open": 0,
        }

    def _remaining_budget(self):
        if self.total_budget_seconds is None:
            return None
        return self.total_budget_seconds - (time.monotonic() - self._started_at)

    def _open_circuit(self, reason):
        if self._circuit_open_reason is None:
            self._circuit_open_reason = str(reason)
            self.stats["circuit_open"] += 1
            logger.warning(f"KPL circuit opened: {self._circuit_open_reason}")

    def _sleep_retry(self, seconds):
        """Sleep for a retry delay without exceeding the client budget."""
        delay = max(0.0, float(seconds))
        remaining = self._remaining_budget()
        if remaining is not None:
            if remaining <= 0:
                self._open_circuit("total_budget_exceeded")
                return False
            delay = min(delay, remaining)
        if delay:
            time.sleep(delay)
        return True

    def get(
        self,
        endpoint,
        params=None,
        *,
        critical=False,
        accept_source_date=False,
    ):
        """Fetch an endpoint with adaptive rate limiting.

        Args:
            endpoint: API path (e.g. '/sector/ranking')
            params: query parameters dict
            critical: if True, empty responses trigger longer backoff (for data-critical endpoints)
            accept_source_date: allow a dated response to be returned only for
                collectors that persist every row at its own source date.
        """
        if self._circuit_open_reason is not None:
            self.stats["skipped"] += 1
            return None
        remaining = self._remaining_budget()
        if remaining is not None and remaining <= 0:
            self._open_circuit("total_budget_exceeded")
            self.stats["skipped"] += 1
            return None

        url = f"{API_BASE}{endpoint}"
        if params:
            qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            if qs:
                url += f"?{qs}"

        # Check if endpoint is in cooldown
        now = time.time()
        if endpoint in self._cooldown_until and now < self._cooldown_until[endpoint]:
            wait = self._cooldown_until[endpoint] - now
            if wait > 0:
                logger.info(f"  [cooldown] {endpoint} cooling down for {wait:.0f}s...")
                # A collector should hand control to its fallback source
                # instead of blocking an entire trading phase on a cooled
                # endpoint.  The shared host limiter still protects the next
                # independent probe.
                self.stats["skipped"] += 1
                return None

        for attempt in range(self.max_attempts):
            now = time.time()
            elapsed = now - self._last_request_time

            # Base delay
            delay = REQUEST_DELAY
            # Extra delay for rate-limited endpoints
            if any(endpoint.startswith(ep) for ep in RATE_LIMIT_ENDPOINTS):
                delay = max(delay, RATE_LIMIT_DELAY)

            if elapsed < delay:
                time.sleep(delay - elapsed)

            # KPLClient is used by separate scripts from the resilient source
            # layer; share the same host lease so those scripts cannot burst
            # the KPL host concurrently.
            # Keep the global host lease for rate limiting, but isolate empty
            # endpoint backoff so one optional/unsupported route cannot pause
            # healthy KPL routes for the whole host.
            shared_host_limiter.acquire(f"kpl_endpoint:{endpoint}", 0.001)
            shared_host_limiter.acquire("kpl", delay)

            headers = {"accept": "application/json"}
            if API_KEY:
                headers["X-API-Key"] = API_KEY
            req = urllib.request.Request(url, headers=headers)
            try:
                self._last_request_time = time.time()
                remaining = self._remaining_budget()
                if remaining is not None and remaining <= 0:
                    self._open_circuit("total_budget_exceeded")
                    self.stats["skipped"] += 1
                    return None
                timeout = self.request_timeout
                if remaining is not None:
                    timeout = max(0.1, min(timeout, remaining))
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    semantic = validate_kpl(endpoint, params, data)
                    empty = self._is_empty_response(data)
                    if not semantic.ok or empty:
                        if not semantic.ok:
                            self.stats["semantic_error"] += 1
                            logger.warning(f"  [semantic] {endpoint}: {semantic.reason}")
                            # A dated endpoint returning a different trading
                            # date is a deterministic stale snapshot, not a
                            # transient empty response.  Retrying it only
                            # burns the intraday budget and can block the
                            # fallback market-context derivation.
                            semantic_reason = str(semantic.reason).lower()
                            source_date_mismatch = (
                                "date mismatch" in semantic_reason
                                or "missing requested rise-fall row" in semantic_reason
                            )
                            if source_date_mismatch:
                                if accept_source_date and endpoint == "/market/rise-fall":
                                    self.stats["success"] += 1
                                    logger.info(
                                        f"  [source-date] {endpoint} accepted for "
                                        "source-date persistence only"
                                    )
                                    return data
                                self.stats["empty"] += 1
                                return None
                        self.stats["empty"] += 1
                        self._empty_streaks[endpoint] = self._empty_streaks.get(endpoint, 0) + 1
                        streak = self._empty_streaks[endpoint]

                        if streak > MAX_EMPTY_RETRIES:
                            logger.info(
                                f"  [empty] {endpoint} returned empty {streak} times, "
                                f"giving up to trigger fallback"
                            )
                            # Do not leak an unverified payload to callers;
                            # this is what activates the provider fallback.
                            return None

                        backoff = min(10 * (2 ** streak), 60)
                        self._cooldown_until[endpoint] = time.time() + backoff
                        shared_host_limiter.cooldown(f"kpl_endpoint:{endpoint}", backoff)
                        self.stats["rate_limited"] += 1
                        logger.info(
                            f"  [empty] {endpoint} returned empty (streak={streak}), "
                            f"backoff {backoff}s"
                        )
                        if attempt < self.max_attempts - 1:
                            self._sleep_retry(backoff)
                        continue

                    self.stats["success"] += 1

                    # Got real data — reset streak
                    if endpoint in self._empty_streaks:
                        del self._empty_streaks[endpoint]
                    if endpoint in self._cooldown_until:
                        del self._cooldown_until[endpoint]
                    return data

            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8")[:200]
                except Exception:
                    pass
                if e.code == 422:
                    logger.debug(f"422 {endpoint}: {body}")
                    self.stats["error"] += 1
                    return None
                if e.code == 429:
                    # Rate limited — hard backoff
                    backoff = 30 * (attempt + 1)
                    logger.warning(f"429 {endpoint}: rate limited, backoff {backoff}s")
                    self.stats["rate_limited"] += 1
                    self._cooldown_until[endpoint] = time.time() + backoff
                    shared_host_limiter.cooldown("kpl", max(backoff, 60))
                    if attempt < self.max_attempts - 1:
                        self._sleep_retry(backoff)
                        continue
                if attempt < self.max_attempts - 1:
                    self._sleep_retry(RETRY_DELAY * (attempt + 1))
                    continue
                logger.warning(f"HTTP {e.code} {endpoint}: {body}")
                self.stats["error"] += 1
                return None
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", None)
                winerror = getattr(reason, "winerror", None)
                if winerror == 10013 or isinstance(reason, PermissionError):
                    self.stats["error"] += 1
                    self._open_circuit(f"network_permission_denied:{winerror or 'permission'}")
                    logger.warning(f"Error {endpoint}: {e}")
                    return None
                if attempt < self.max_attempts - 1:
                    self._sleep_retry(RETRY_DELAY * (attempt + 1))
                    continue
                logger.warning(f"Error {endpoint}: {e}")
                self.stats["error"] += 1
                return None
            except Exception as e:
                if attempt < self.max_attempts - 1:
                    self._sleep_retry(RETRY_DELAY * (attempt + 1))
                    continue
                logger.warning(f"Error {endpoint}: {e}")
                self.stats["error"] += 1
                return None

        return None

    @staticmethod
    def _is_empty_response(data):
        """Detect if an API response is effectively empty (rate-limited)."""
        if isinstance(data, dict):
            # Check common empty patterns
            for key in ("sectors", "stocks", "data", "list", "ladder"):
                val = data.get(key)
                if isinstance(val, (list, dict)) and len(val) > 0:
                    return False
            # sector/ranking specific: summary with all zeros + empty sectors
            if "sectors" in data and "summary" in data:
                summary = data["summary"]
                zeros = (
                    (summary.get("涨停数") or 0) == 0
                    and (summary.get("上涨家数") or 0) == 0
                    and (summary.get("下跌家数") or 0) == 0
                )
                if zeros:
                    return True
            return False
        if isinstance(data, list):
            return len(data) == 0
        return data is None

    def get_all_stock_codes(self):
        """Get all stock codes from sector plates -> stocks mapping."""
        data = self.get("/sector/plates")
        if not data or "plates" not in data:
            return []
        codes = set()
        for plate in data["plates"]:
            plate_code = plate.get("code", plate.get("plate_code", ""))
            if plate_code:
                stocks = self.get("/sector/stocks", {"code": plate_code})
                if stocks and isinstance(stocks, dict):
                    for s in stocks.get("stocks", stocks.get("data", [])):
                        if isinstance(s, dict):
                            c = s.get("code", s.get("stock_code", s.get("股票代码", "")))
                            if c:
                                codes.add(c)
                        elif isinstance(s, list) and len(s) > 0:
                            codes.add(str(s[0]))
        return sorted(codes)


class DuckDBStore:
    """DuckDB store with dynamic table creation."""

    def __init__(self, db_path=None):
        self.db_path = db_path or DB_PATH
        self.conn = connect_duckdb(self.db_path)
        self._init_meta()

    def _init_meta(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS _collect_log (
                table_name VARCHAR,
                endpoint VARCHAR,
                rows_inserted INTEGER,
                status VARCHAR,
                fetched_at TIMESTAMP DEFAULT current_timestamp
            )
        """)

    def execute(self, sql, params=None):
        if params:
            self.conn.execute(sql, params)
        else:
            self.conn.execute(sql)

    def fetchall(self, sql, params=None):
        if params:
            return self.conn.execute(sql, params).fetchall()
        return self.conn.execute(sql).fetchall()

    def insert_rows(self, table_name, rows, columns, replace_on=None):
        """Insert rows into a table, creating it if needed."""
        if not rows:
            return 0

        self._ensure_table(table_name, columns)

        placeholders = ", ".join(["?"] * len(columns))
        col_str = ", ".join(columns)
        sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({placeholders})"
        replace_indexes = []
        if replace_on:
            replace_indexes = [columns.index(col) for col in replace_on]
            where = " AND ".join(f"{col} = ?" for col in replace_on)
            delete_sql = f"DELETE FROM {table_name} WHERE {where}"

        inserted = 0
        for i, row in enumerate(rows):
            try:
                if replace_indexes:
                    self.conn.execute(delete_sql, [row[idx] for idx in replace_indexes])
                self.conn.execute(sql, list(row))
                inserted += 1
            except Exception as e:
                if i == 0:
                    # Log first error loudly so we can see the schema mismatch
                    logger.warning(f"Insert error {table_name}[0] ({len(row)} cols vs {len(columns)} schema): {e}")
                    logger.warning(f"  row: {list(row)[:6]}...")
                    logger.warning(f"  schema: {columns}")
                else:
                    logger.debug(f"Insert error {table_name}[{i}]: {e}")
                # On first error, try to auto-migrate: add missing columns
                if i == 0 and ("does not exist" in str(e) or "no column" in str(e).lower()):
                    logger.info(f"  Attempting auto-migration for {table_name}...")
                    # Fall back to JSON-style storage
                    self._ensure_json_table(table_name, columns)
        return inserted

    def insert_json(self, table_name, records, extra_cols=None):
        """Insert list of dicts, storing as JSON with optional extra columns."""
        if not records:
            return 0

        cols = ["fetched_at"]
        if extra_cols:
            cols = extra_cols + cols
        cols.append("raw_json")

        self._ensure_json_table(table_name, cols)

        placeholders = ", ".join(["?"] * len(cols))
        col_str = ", ".join(cols)
        sql = f"INSERT INTO {table_name} ({col_str}) VALUES ({placeholders})"

        now = datetime.now().isoformat()
        count = 0
        for rec in records:
            vals = []
            if extra_cols:
                for c in extra_cols:
                    vals.append(rec.get(c, rec.get(c.replace("_", ""), None)))
            vals.append(now)
            vals.append(json.dumps(rec, ensure_ascii=False))
            try:
                self.conn.execute(sql, vals)
                count += 1
            except Exception as e:
                logger.debug(f"Insert error {table_name}: {e}")

        return count

    def insert_raw(self, endpoint, data):
        """Store raw JSON response in catch-all table."""
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_api_data (
                endpoint VARCHAR,
                fetched_at TIMESTAMP,
                raw_json VARCHAR
            )
        """)
        self.conn.execute(
            "INSERT INTO raw_api_data (endpoint, fetched_at, raw_json) VALUES (?, ?, ?)",
            [endpoint, datetime.now().isoformat(), json.dumps(data, ensure_ascii=False)]
        )

    def log_collect(self, table_name, endpoint, rows, status="ok"):
        self.conn.execute(
            "INSERT INTO _collect_log (table_name, endpoint, rows_inserted, status) VALUES (?, ?, ?, ?)",
            [table_name, endpoint, rows, status]
        )

    def _ensure_table(self, name, columns):
        try:
            self.conn.execute(f"SELECT 1 FROM {name} LIMIT 0")
        except Exception:
            col_defs = ", ".join(f"{c} VARCHAR" for c in columns)
            self.conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({col_defs})")

    def _ensure_json_table(self, name, columns):
        try:
            self.conn.execute(f"SELECT 1 FROM {name} LIMIT 0")
        except Exception:
            col_defs = ", ".join(f"{c} VARCHAR" for c in columns)
            self.conn.execute(f"CREATE TABLE IF NOT EXISTS {name} ({col_defs})")

    def close(self):
        self.conn.close()
