"""Project-owned persistence and orchestration for migrated market sources.

The sibling project supplied the parsers and provider plans; this module makes
them part of this repository's data contract.  It deliberately keeps raw JSON,
provider and stale/fresh state alongside normalized rows, so a provider outage
is visible and cannot silently masquerade as current data.
"""

from __future__ import annotations

from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any, Callable

import duckdb

from trade_system import resilient_sources
from trade_system.flow_contract import ensure_stock_flow_contract, normalize_stock_flow_row, normalize_sector_flow_row
from trade_system.source_authority import provider_rank_sql
from trade_system.units import _number


def _date(value: Any, fallback: str | None = None) -> str | None:
    raw = "".join(ch for ch in str(value or fallback or "") if ch.isdigit())
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) >= 8 else None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class MultiSourceStore:
    """Fetch through the migrated fallback graph and persist idempotently."""

    def __init__(self, db_path: str | Path, fetcher: Callable[..., tuple[Any, dict]] | None = None):
        self.db_path = str(db_path)
        from trade_system.db_utils import legacy_connect
        self.con = legacy_connect(self.db_path)
        self.fetcher = fetcher or resilient_sources.get
        self._ensure_tables()

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> "MultiSourceStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _ensure_tables(self) -> None:
        """Allow the adapter to run against a fresh/test DB without the full schema."""
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_observation(
              observed_at TIMESTAMP DEFAULT current_timestamp, source_date DATE,
              data_type VARCHAR, asset_type VARCHAR, asset_code VARCHAR,
              provider VARCHAR, status VARCHAR, latency_ms INTEGER,
              is_stale BOOLEAN DEFAULT FALSE, payload_json VARCHAR, payload_hash VARCHAR)
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_kline(
              source_date DATE, asset_type VARCHAR, asset_code VARCHAR, open DOUBLE,
              high DOUBLE, low DOUBLE, close DOUBLE, volume DOUBLE, amount DOUBLE,
              change_pct DOUBLE, provider VARCHAR, volume_unit VARCHAR, amount_unit VARCHAR,
              adjustment VARCHAR, fetched_at TIMESTAMP DEFAULT current_timestamp,
              is_stale BOOLEAN DEFAULT FALSE, raw_json VARCHAR)
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_stock_flow(
              source_date DATE, stock_code VARCHAR, main_net DOUBLE, super_net DOUBLE,
              large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE, close DOUBLE,
              change_pct DOUBLE, turnover DOUBLE, provider VARCHAR,
              fetched_at TIMESTAMP DEFAULT current_timestamp, is_stale BOOLEAN DEFAULT FALSE,
              raw_json VARCHAR)
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_sector_flow(
              source_date DATE, sector_code VARCHAR, sector_name VARCHAR, main_net DOUBLE,
              super_net DOUBLE, large_net DOUBLE, mid_net DOUBLE, small_net DOUBLE,
              change_pct DOUBLE, main_ratio DOUBLE, provider VARCHAR,
              sector_type VARCHAR, amount_unit VARCHAR,
              fetched_at TIMESTAMP DEFAULT current_timestamp, is_stale BOOLEAN DEFAULT FALSE,
              raw_json VARCHAR)
        """)
        # Existing project databases predate taxonomy/unit provenance.  Add
        # the columns in-place so old data remains readable while new rows can
        # never be ranked without declaring what the amount means.
        self.con.execute("ALTER TABLE multi_source_sector_flow ADD COLUMN IF NOT EXISTS sector_type VARCHAR")
        self.con.execute("ALTER TABLE multi_source_sector_flow ADD COLUMN IF NOT EXISTS amount_unit VARCHAR")
        self.con.execute("ALTER TABLE multi_source_kline ADD COLUMN IF NOT EXISTS volume_unit VARCHAR")
        self.con.execute("ALTER TABLE multi_source_kline ADD COLUMN IF NOT EXISTS amount_unit VARCHAR")
        self.con.execute("ALTER TABLE multi_source_kline ADD COLUMN IF NOT EXISTS adjustment VARCHAR")
        ensure_stock_flow_contract(self.con)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_quote(
              source_date DATE, asset_type VARCHAR, asset_code VARCHAR, name VARCHAR,
              price DOUBLE, change_pct DOUBLE, pe_ttm DOUBLE, pb DOUBLE, total_mv DOUBLE,
              circ_mv DOUBLE, provider VARCHAR, total_mv_unit VARCHAR, circ_mv_unit VARCHAR,
              fetched_at TIMESTAMP DEFAULT current_timestamp,
              is_stale BOOLEAN DEFAULT FALSE, raw_json VARCHAR)
        """)
        self.con.execute("ALTER TABLE multi_source_quote ADD COLUMN IF NOT EXISTS source_event_time TIMESTAMP")
        self.con.execute("ALTER TABLE multi_source_quote ADD COLUMN IF NOT EXISTS collected_at TIMESTAMP")
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_sync_status(
              run_id VARCHAR, run_started_at TIMESTAMP, run_finished_at TIMESTAMP,
              trade_date DATE, data_type VARCHAR, asset_scope VARCHAR, requested_count INTEGER,
              success_count INTEGER, stale_count INTEGER, failed_count INTEGER,
              providers_json VARCHAR, error_json VARCHAR)
        """)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_task_checkpoint(
              run_id VARCHAR,
              trade_date DATE,
              stage VARCHAR,
              task_name VARCHAR,
              data_type VARCHAR,
              asset_code VARCHAR,
              status VARCHAR,
              attempts INTEGER DEFAULT 0,
              rows_written INTEGER DEFAULT 0,
              provider VARCHAR,
              started_at TIMESTAMP,
              finished_at TIMESTAMP,
              last_error VARCHAR,
              updated_at TIMESTAMP DEFAULT current_timestamp,
              PRIMARY KEY(trade_date, stage, task_name, data_type, asset_code)
            )
        """)

    def fetch(self, data_type: str, code: str | None = None, **kwargs) -> tuple[Any, dict]:
        if (self.fetcher is resilient_sources.get and data_type in resilient_sources._BAR_TYPES
                and not kwargs.get("full_history") and "start" in kwargs and "end" in kwargs):
            from trade_system.trading_calendar import open_session_dates
            kwargs["expected_sessions"] = open_session_dates(self.con, kwargs["start"], kwargs["end"], strict=True)
        return self.fetcher(data_type, code, **kwargs)

    def store(self, data_type: str, code: str | None, data: Any, meta: dict, *, asset_type: str | None = None,
              trade_date: str | None = None, commit: bool = True) -> dict[str, Any]:
        if data_type == "limit_up_sentiment" or meta.get("derived"):
            raise ValueError("derived statistics are not provider receipts; store the original pools")
        if commit:
            self.con.execute("BEGIN TRANSACTION")
        try:
            if "receipts" in meta:
                # Materialize only the original provider receipts, not an assembled
                # window with invented timestamps or duplicated covered sessions.
                results = []
                for receipt in meta["receipts"]:
                    rows = receipt["data"]
                    event_rows = rows.get("rows", []) if isinstance(rows, dict) else rows
                    receipt_date = max((_date(row.get("date")) or "" for row in event_rows if isinstance(row, dict)), default="")
                    results.append(self.store(data_type, code, rows, receipt["meta"],
                        asset_type=asset_type, trade_date=receipt_date or trade_date, commit=False))
                status = meta.get("status", "failed")
                if any(r["status"] == "cache_unmaterialized" for r in results):
                    status = "cache_unmaterialized"
                if commit:
                    self.con.commit()
                return {"status": status, "provider": meta.get("source"),
                        "rows_written": sum(r["rows_written"] for r in results),
                        "receipt_reused": bool(results) and all(r.get("receipt_reused") for r in results)}
            status = str(meta.get("status") or "failed")
            stale = status == "stale"
            provider = str(meta.get("source") or "unknown")
            payload = data if data is not None else {"error": meta.get("error")}
            payload_hash = _hash(payload)
            rows_written = 0

            source_date = _date(trade_date or meta.get("trade_date"))
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict):
                        source_date = source_date or _date(row.get("date") or row.get("trade_date"))
                        break
            elif isinstance(data, dict):
                source_date = source_date or _date(data.get("date") or data.get("trade_date"))

            if status == "fresh":
                # A cache hit is not another receipt and must not refresh any
                # observation or canonical row's received/fetched timestamp.
                exists = self.con.execute(
                    "SELECT 1 FROM multi_source_observation WHERE data_type IN (?,?) "
                    "AND asset_code IS NOT DISTINCT FROM ? AND payload_hash=? AND provider=? "
                    "AND source_date IS NOT DISTINCT FROM ? "
                    "AND status IN ('live','refreshed','delayed') LIMIT 1",
                    [data_type, {"stock_flow": "fund_flow_120d", "fund_flow_120d": "stock_flow"}.get(data_type, data_type),
                     code, payload_hash, provider, source_date],
                ).fetchone()
                if exists and data_type in resilient_sources._BAR_TYPES:
                    kind = asset_type or {"index_kline": "index", "etf_kline": "etf", "cb_kline": "cb"}.get(data_type, "stock")
                    requested = set(meta.get("qualified_dates", []))
                    for row in data:
                        if requested and resilient_sources._norm_date(row.get("date", "")) not in requested:
                            continue
                        materialized = self.con.execute(
                            "SELECT 1 FROM multi_source_kline WHERE source_date=? AND asset_type=? "
                            "AND asset_code=? AND provider=? AND raw_json=? AND is_stale=false LIMIT 1",
                            [_date(row.get("date")), kind, code, provider, _json(row)]).fetchone()
                        if not materialized:
                            exists = False
                            break
                if commit:
                    self.con.commit()
                return {"status": "fresh" if exists else "cache_unmaterialized",
                        "provider": provider, "rows_written": 0, "stale": False,
                        "source_date": source_date, "payload_hash": payload_hash,
                        "receipt_reused": bool(exists)}

            received_at = datetime.fromtimestamp(meta["received_at"]) if isinstance(meta.get("received_at"), (float, int)) else datetime.now()
            self.con.execute(
                "INSERT INTO multi_source_observation "
                "(source_date,data_type,asset_type,asset_code,provider,status,latency_ms,is_stale,payload_json,payload_hash,observed_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                [source_date, data_type, asset_type or data_type, code, provider, status,
                 int(float(meta.get("latency", 0) or 0) * 1000), stale, _json(payload), payload_hash, received_at],
            )

        # Do not overwrite a live row with an expired cache result.  The stale
        # observation above is enough to make the degradation auditable.
            if data is not None and status in {'live', 'refreshed', 'fresh', 'delayed'}:
                if data_type in {"kline", "index_kline", "etf_kline", "cb_kline"}:
                    canonical = data
                    if "qualified_dates" in meta:
                        qualified = set(meta["qualified_dates"])
                        canonical = [row for row in data if resilient_sources._norm_date(row.get("date", "")) in qualified]
                    rows_written = self._store_klines(data_type, code, canonical, provider, asset_type, stale, received_at)
                elif data_type in {"stock_flow", "fund_flow_120d", "fund_flow"}:
                    rows_written = self._store_stock_flow(code, data, provider, stale, received_at)
                elif data_type == "sector_flow":
                    rows_written = self._store_sector_flow(data, provider, trade_date, stale, received_at)
                elif data_type in {"valuation", "index_spot", "etf_info", "cb_quote", "bid_ask"}:
                    rows_written = self._store_quote(data_type, code, data, provider, asset_type, stale, trade_date)

            if commit:
                self.con.commit()
            return {"status": status, "provider": provider, "rows_written": rows_written,
                    "stale": stale, "source_date": source_date, "payload_hash": payload_hash}
        except Exception:
            if commit:
                try:
                    self.con.rollback()
                except Exception:
                    pass
            raise

    def _store_klines(self, data_type, code, data, provider, asset_type, stale, received_at=None):
        rows = data if isinstance(data, list) else []
        kind = asset_type or ({"index_kline": "index", "etf_kline": "etf", "cb_kline": "cb"}.get(data_type, "stock"))
        count = 0
        for row in rows:
            if not isinstance(row, dict) or not row.get("date"):
                continue
            d = _date(row.get("date"))
            ac = str(code or row.get("code") or "")
            self.con.execute(
                "DELETE FROM multi_source_kline WHERE source_date=? AND asset_type=? AND asset_code=? AND provider=? AND adjustment=?",
                [d, kind, ac, provider, row.get("adjustment") or "unknown"],
            )
            self.con.execute(
                "INSERT INTO multi_source_kline(source_date,asset_type,asset_code,open,high,low,close,volume,amount,change_pct,provider,volume_unit,amount_unit,adjustment,is_stale,raw_json,fetched_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [d, kind, ac, _number(row.get("open")), _number(row.get("high")), _number(row.get("low")),
                 _number(row.get("close")), _number(row.get("volume")), _number(row.get("amount")),
                 _number(row.get("change_pct") or row.get("pct")), provider,
                 row.get("volume_unit") or "unknown", row.get("amount_unit") or "unknown",
                 row.get("adjustment") or "unknown", stale, _json(row), received_at or datetime.now()],
            )
            count += 1
        return count

    def _store_stock_flow(self, code, data, provider, stale, received_at=None):
        rows = data if isinstance(data, list) else []
        received_at = received_at or datetime.now()
        count = 0
        for row in rows:
            if not isinstance(row, dict) or not (row.get("date") or row.get("trade_date")):
                continue
            # Pre-open/blocked sector and quote endpoints sometimes return a
            # syntactically valid row with every capital-flow field missing.
            # Do not persist that placeholder as usable money-flow evidence.
            canonical = normalize_stock_flow_row(row, provider)
            flow_fields = ("main_net", "net_total", "super_net", "large_net", "mid_net", "small_net")
            if not any(canonical.get(field) is not None for field in flow_fields):
                continue
            d = _date(row.get("date") or row.get("trade_date"))
            stock = str(code or row.get("code") or "")
            columns = ("source_date", "stock_code", "provider", "main_net", "net_total", "super_net",
                       "large_net", "mid_net", "small_net", "close", "change_pct", "turnover",
                       "amount_unit", "flow_unit", "turnover_unit", "flow_definition", "source_api",
                       "origin_provider", "field_mapping_version", "is_stale", "raw_json", "fetched_at")
            values = [d, stock, provider, canonical["main_net"], canonical["net_total"],
                      canonical["super_net"], canonical["large_net"], canonical["mid_net"], canonical["small_net"],
                      _number(row.get("close")), _number(row.get("change_pct") if row.get("change_pct") is not None else row.get("pct")),
                      _number(row.get("turnover")), canonical["amount_unit"], canonical["flow_unit"], canonical["turnover_unit"],
                      canonical["flow_definition"], canonical["source_api"], canonical["origin_provider"],
                      canonical["field_mapping_version"], stale, _json(row), received_at]
            # One atomic statement works before and after the business-key index exists.
            # No delete/reinsert cycle; raw receipt and original receive time remain paired.
            names = ",".join(columns)
            self.con.execute(
                f"MERGE INTO multi_source_stock_flow AS target USING (VALUES ({','.join('?' for _ in columns)})) "
                f"AS source({names}) ON target.source_date=CAST(source.source_date AS DATE) "
                "AND target.stock_code=source.stock_code AND target.provider=source.provider "
                f"WHEN MATCHED THEN UPDATE SET {','.join(f'{c}=source.{c}' for c in columns[3:])} "
                f"WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({','.join('source.'+c for c in columns)})",
                values,
            )
            count += 1
        return count

    def _store_sector_flow(self, data, provider, trade_date, stale, received_at=None):
        rows = data if isinstance(data, list) else []
        count = 0
        d_default = _date(trade_date) or date.today().isoformat()
        for row in rows:
            if not isinstance(row, dict) or not row.get("sector_code"):
                continue
            canonical = normalize_sector_flow_row(row, provider)
            if provider == "existing_core" and row.get("sector_type") == "legacy_core":
                canonical["sector_type"] = "legacy_core"  # Preserve unqualified historical taxonomy.
            # A pre-open Eastmoney snapshot can contain sector names/prices
            # while all flow fields are ``-``.  Such rows are not a partial
            # flow measurement and must not make readiness look available.
            flow_fields = ("main_net", "super_net", "large_net", "mid_net", "small_net")
            if not any(canonical.get(field) is not None for field in flow_fields):
                continue
            d = _date(row.get("date") or row.get("trade_date")) or d_default
            code = str(row.get("sector_code"))
            conflicting = self.con.execute(
                "SELECT 1 FROM multi_source_sector_flow WHERE source_date=? AND sector_code=? AND provider=? "
                "AND sector_type IS NOT NULL AND sector_type<>'unknown' AND sector_type<>? LIMIT 1",
                [d, code, provider, canonical['sector_type']],
            ).fetchone()
            if conflicting:
                raise ValueError('sector source/date/code changed taxonomy; explicit migration required')
            columns = ("source_date", "sector_code", "provider", "sector_name", "main_net", "super_net",
                       "large_net", "mid_net", "small_net", "change_pct", "main_ratio", "sector_type",
                       "amount_unit", "is_stale", "raw_json", "fetched_at")
            values = [d, code, provider, row.get("sector_name"), canonical['main_net'], canonical['super_net'],
                      canonical['large_net'], canonical['mid_net'], canonical['small_net'],
                      _number(row.get("change_pct")), _number(row.get("main_ratio")), canonical['sector_type'],
                      'yuan', stale, _json(dict(row, canonical_contract=canonical)),
                      received_at or row.get("fetched_at")]
            names = ",".join(columns)
            self.con.execute(
                f"MERGE INTO multi_source_sector_flow AS target USING (VALUES ({','.join('?' for _ in columns)})) "
                f"AS source({names}) ON target.source_date=CAST(source.source_date AS DATE) "
                "AND target.sector_code=source.sector_code AND target.provider=source.provider "
                f"WHEN MATCHED THEN UPDATE SET {','.join(f'{c}=source.{c}' for c in columns[3:])} "
                f"WHEN NOT MATCHED THEN INSERT ({names}) VALUES ({','.join('source.'+c for c in columns)})",
                values,
            )
            count += 1
        return count

    def _store_quote(self, data_type, code, data, provider, asset_type, stale, trade_date):
        if not isinstance(data, dict):
            return 0
        from trade_system.units import market_caps, quote_source_event_time
        from zoneinfo import ZoneInfo
        # Preserve existing provider-valued columns and raw receipt; canonical
        # names and unknown reasons travel in the persisted JSON without DDL.
        data = dict(data, **market_caps(data))
        d = _date(data.get("date") or data.get("trade_date") or trade_date) or date.today().isoformat()
        ac = str(code or data.get("code") or "")
        if data.get('code') and str(data['code']).split('.')[0]!=ac.split('.')[0]:
            raise ValueError('quote security identity mismatch')
        event=quote_source_event_time(data,provider)
        received=datetime.now(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
        if event:
            declared=_date(data.get('date') or data.get('trade_date'))
            if event>received or (declared and declared!=event.date().isoformat()):
                raise ValueError('quote event time/date inconsistent')
            d=event.date().isoformat()
            data['source_event_time']=event.replace(tzinfo=ZoneInfo('Asia/Shanghai')).isoformat()
        columns={row[1] for row in self.con.execute("PRAGMA table_info('multi_source_quote')").fetchall()}
        clocks={'source_event_time':event,'collected_at':received,'fetched_at':received}
        clock_fields=[name for name in clocks if name in columns]
        extra_columns=','+','.join(clock_fields) if clock_fields else ''
        extra_values=',?'*len(clock_fields)
        self.con.execute(
            "DELETE FROM multi_source_quote WHERE source_date=? AND asset_type=? AND asset_code=? AND provider=?",
            [d, asset_type or data_type, ac, provider],
        )
        self.con.execute(
            "INSERT INTO multi_source_quote(source_date,asset_type,asset_code,name,price,change_pct,pe_ttm,pb,total_mv,circ_mv,provider,total_mv_unit,circ_mv_unit,is_stale,raw_json"+extra_columns+") "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?"+extra_values+")",
            [d, asset_type or data_type, ac, data.get("name"), _number(data.get("price")),
             _number(data.get("change_pct") if data.get("change_pct") is not None else data.get("pct")), _number(data.get("pe_ttm")), _number(data.get("pb")),
             _number(data.get("total_mv")), _number(data.get("circ_mv")), provider,
             data.get("total_mv_unit") or "unknown", data.get("circ_mv_unit") or "unknown",
             stale, _json(data), *[clocks[name] for name in clock_fields]],
        )
        return 1

    def sync_sector_capital(self, trade_date: str | None = None) -> int:
        """Copy fresh sector flows into the project's existing sector capital chain."""
        try:
            date_clause = " AND source_date=CAST(? AS DATE)" if trade_date else ""
            params = [trade_date] if trade_date else []
            self.con.execute("BEGIN TRANSACTION")
            # This legacy destination has no namespace/measure column. Refuse
            # an ambiguous projection, rather than silently rank unlike types
            # into the same date/code key or discard the prior good snapshot.
            collisions = self.con.execute(
                "SELECT source_date,sector_code FROM multi_source_sector_flow "
                "WHERE is_stale=FALSE AND sector_type IN "
                "('em_industry','ths_concept','ths_concept_derived') AND amount_unit='yuan'"
                + date_clause + " GROUP BY source_date,sector_code "
                "HAVING count(DISTINCT sector_type)>1 LIMIT 20", params).fetchall()
            if collisions:
                raise ValueError(f"sector_capital namespace/measure collision; snapshot preserved: {collisions}")
            sector_order = provider_rank_sql("sector_flow", "provider")
            rows = self.con.execute(
                f"SELECT source_date,sector_code,main_net,super_net,large_net,mid_net,small_net FROM ("
                "SELECT source_date,sector_code,main_net,super_net,large_net,mid_net,small_net,provider,fetched_at,"
                f"row_number() OVER (PARTITION BY source_date,sector_code ORDER BY {sector_order} ASC, fetched_at DESC) AS _rn "
                "FROM multi_source_sector_flow WHERE is_stale=FALSE"
                " AND sector_type IN ('em_industry','ths_concept','ths_concept_derived') AND amount_unit='yuan'"
                " AND (main_net IS NOT NULL OR super_net IS NOT NULL OR large_net IS NOT NULL OR mid_net IS NOT NULL OR small_net IS NOT NULL)"
                + date_clause + ") WHERE _rn=1",
                params,
            ).fetchall()
            if not rows:
                self.con.rollback()
                return 0
            for selected_date in sorted({row[0] for row in rows}):
                self.con.execute("DELETE FROM sector_capital WHERE date=CAST(? AS DATE)", [selected_date])
            for row in rows:
                self.con.execute(
                    "INSERT INTO sector_capital(date,sector_code,main_net_inflow,super_net_inflow,big_net_inflow,mid_net_inflow,small_net_inflow) VALUES (?,?,?,?,?,?,?)",
                    list(row),
                )
            self.con.commit()
            return len(rows)
        except ValueError:
            self.con.rollback()
            raise
        except Exception:
            # A partially initialized legacy DB may not have sector_capital;
            # migration data remains available in its source-aware table.
            try:
                self.con.rollback()
            except Exception:
                pass
            return 0

    def sync_kpl_intraday_flow(self, trade_date: str | None = None) -> int:
        """Promote the latest KPL minute money-flow point into the source layer.

        ``advanced_zjmm_min`` is a cumulative intraday series, so summing its
        points would double count.  One latest point per stock/date is the
        correct daily snapshot and keeps KPL as an auditable provider rather
        than replacing the Eastmoney/Sina historical series.
        """
        try:
            tables = {row[0] for row in self.con.execute("show tables").fetchall()}
            if "advanced_zjmm_min" not in tables:
                return 0
            where = "? IS NULL OR CAST(date AS DATE)=CAST(? AS DATE)"
            rows = self.con.execute(
                "SELECT date,stock_code,main_net_inflow,super_net_inflow,big_net_inflow,fetched_at "
                "FROM (SELECT date,stock_code,main_net_inflow,super_net_inflow,big_net_inflow,fetched_at, "
                "row_number() OVER (PARTITION BY date,stock_code ORDER BY try_cast(time AS TIME) DESC NULLS LAST, fetched_at DESC) AS rn "
                "FROM advanced_zjmm_min WHERE " + where + ") q WHERE rn=1",
                [trade_date, trade_date],
            ).fetchall()
            if not rows:
                return 0
            self.con.execute("BEGIN TRANSACTION")
            count = 0
            for day, stock, main, super_net, large, received in rows:
                if received is None:
                    continue
                count += self._store_stock_flow(stock, [{
                    "date": str(day), "main_net": main, "super_net": super_net, "large_net": large,
                    "amount_unit": "yuan", "flow_definition": "provider_main_orders_net",
                    "source_api": "advanced_zjmm_min", "origin_provider": "kpl",
                    "aggregation": "latest_cumulative_point",
                }], "kpl", False, received)
            self.con.commit()
            return count
        except Exception:
            try:
                self.con.rollback()
            except Exception:
                pass
            return 0

    def bootstrap_from_core(self) -> dict[str, int]:
        """Preserve already-collected core rows in the migrated source-aware layer."""
        counts = {"kline": 0, "index_kline": 0, "sector_capital": 0}
        self.con.execute("BEGIN TRANSACTION")
        try:
            tables = {row[0] for row in self.con.execute("SHOW TABLES").fetchall()}
            if "kline" in tables:
                self.con.execute("DELETE FROM multi_source_kline WHERE provider='existing_core' AND asset_type='stock'")
                self.con.execute(
                    "INSERT INTO multi_source_kline(source_date,asset_type,asset_code,open,high,low,close,volume,amount,change_pct,provider,is_stale,raw_json) "
                    "SELECT date,'stock',stock_code,open,high,low,close,volume,turnover,change_pct,'existing_core',FALSE,"
                    "COALESCE(raw_json, '{\"migrated_from\":\"kline\"}') FROM kline"
                )
                counts["kline"] = self.con.execute("SELECT count(*) FROM multi_source_kline WHERE provider='existing_core' AND asset_type='stock'").fetchone()[0]
            if "index_kline" in tables:
                self.con.execute("DELETE FROM multi_source_kline WHERE provider='existing_core' AND asset_type='index'")
                self.con.execute(
                    "INSERT INTO multi_source_kline(source_date,asset_type,asset_code,open,high,low,close,volume,amount,change_pct,provider,is_stale,raw_json) "
                    "SELECT try_cast(date AS DATE),'index',index_code,open,high,low,close,volume,turnover,change_pct,'existing_core',FALSE,"
                    "COALESCE(raw_json, '{\"migrated_from\":\"index_kline\"}') FROM index_kline"
                )
                counts["index_kline"] = self.con.execute("SELECT count(*) FROM multi_source_kline WHERE provider='existing_core' AND asset_type='index'").fetchone()[0]
            if "sector_capital" in tables:
                rows = self.con.execute(
                    "SELECT date,sector_code,main_net_inflow,super_net_inflow,big_net_inflow,"
                    "mid_net_inflow,small_net_inflow,fetched_at FROM sector_capital "
                    "QUALIFY row_number() OVER (PARTITION BY date,sector_code ORDER BY fetched_at DESC NULLS LAST)=1"
                ).fetchall()
                fields = ("date", "sector_code", "main_net", "super_net", "large_net", "mid_net", "small_net", "fetched_at")
                counts["sector_capital"] = self._store_sector_flow(
                    [dict(zip(fields, row), sector_type="legacy_core", amount_unit="yuan",
                          migrated_from="sector_capital") for row in rows], "existing_core", None, False)
            self.con.commit()
            return counts
        except Exception:
            self.con.rollback()
            raise

    def record_run(self, *, run_id: str, started: datetime, finished: datetime,
                   trade_date: str | None, data_type: str, asset_scope: str,
                   requested: int, success: int, stale: int, failed: int,
                   providers: list[str], errors: list[str]) -> None:
        self.con.execute(
            "INSERT INTO multi_source_sync_status VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [run_id, started, finished, _date(trade_date), data_type, asset_scope,
             requested, success, stale, failed, _json(providers), _json(errors)],
        )
        self.con.commit()


def infer_codes(db_path: str | Path, table: str, column: str, limit: int) -> list[str]:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        for source in (table, "kline", "tushare_daily"):
            try:
                rows = con.execute(
                    f'SELECT DISTINCT "{column}" FROM "{source}" WHERE "{column}" IS NOT NULL ORDER BY 1 LIMIT ?',
                    [max(0, int(limit))],
                ).fetchall()
                if rows:
                    return [str(row[0]) for row in rows]
            except Exception:
                continue
        return []
    finally:
        con.close()


def new_run_id() -> str:
    return uuid.uuid4().hex
