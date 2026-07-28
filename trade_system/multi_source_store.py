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
from trade_system.flow_contract import ensure_stock_flow_contract, normalize_stock_flow_row


def _date(value: Any, fallback: str | None = None) -> str | None:
    raw = "".join(ch for ch in str(value or fallback or "") if ch.isdigit())
    return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) >= 8 else None


def _number(value: Any) -> float | None:
    try:
        if value in (None, "", "-"):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _hash(payload: Any) -> str:
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


class MultiSourceStore:
    """Fetch through the migrated fallback graph and persist idempotently."""

    def __init__(self, db_path: str | Path, fetcher: Callable[..., tuple[Any, dict]] | None = None):
        self.db_path = str(db_path)
        self.con = duckdb.connect(self.db_path)
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
              change_pct DOUBLE, provider VARCHAR, fetched_at TIMESTAMP DEFAULT current_timestamp,
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
        ensure_stock_flow_contract(self.con)
        self.con.execute("""
            CREATE TABLE IF NOT EXISTS multi_source_quote(
              source_date DATE, asset_type VARCHAR, asset_code VARCHAR, name VARCHAR,
              price DOUBLE, change_pct DOUBLE, pe_ttm DOUBLE, pb DOUBLE, total_mv DOUBLE,
              circ_mv DOUBLE, provider VARCHAR, fetched_at TIMESTAMP DEFAULT current_timestamp,
              is_stale BOOLEAN DEFAULT FALSE, raw_json VARCHAR)
        """)
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
        return self.fetcher(data_type, code, **kwargs)

    def store(self, data_type: str, code: str | None, data: Any, meta: dict, *, asset_type: str | None = None,
              trade_date: str | None = None, commit: bool = True) -> dict[str, Any]:
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

        self.con.execute(
            "INSERT INTO multi_source_observation "
            "(source_date,data_type,asset_type,asset_code,provider,status,latency_ms,is_stale,payload_json,payload_hash) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [source_date, data_type, asset_type or data_type, code, provider, status,
             int(float(meta.get("latency", 0) or 0) * 1000), stale, _json(payload), payload_hash],
        )

        # Do not overwrite a live row with an expired cache result.  The stale
        # observation above is enough to make the degradation auditable.
        if data is not None and not stale:
            if data_type in {"kline", "index_kline", "etf_kline", "cb_kline"}:
                rows_written = self._store_klines(data_type, code, data, provider, asset_type, stale)
            elif data_type in {"stock_flow", "fund_flow_120d", "fund_flow"}:
                rows_written = self._store_stock_flow(code, data, provider, stale)
            elif data_type == "sector_flow":
                rows_written = self._store_sector_flow(data, provider, trade_date, stale)
            elif data_type in {"valuation", "index_spot", "etf_info", "cb_quote", "bid_ask"}:
                rows_written = self._store_quote(data_type, code, data, provider, asset_type, stale, trade_date)

        if commit:
            self.con.commit()
        return {"status": status, "provider": provider, "rows_written": rows_written,
                "stale": stale, "source_date": source_date, "payload_hash": payload_hash}

    def _store_klines(self, data_type, code, data, provider, asset_type, stale):
        rows = data if isinstance(data, list) else []
        kind = asset_type or ({"index_kline": "index", "etf_kline": "etf", "cb_kline": "cb"}.get(data_type, "stock"))
        count = 0
        for row in rows:
            if not isinstance(row, dict) or not row.get("date"):
                continue
            d = _date(row.get("date"))
            ac = str(code or row.get("code") or "")
            self.con.execute(
                "DELETE FROM multi_source_kline WHERE source_date=? AND asset_type=? AND asset_code=? AND provider=?",
                [d, kind, ac, provider],
            )
            self.con.execute(
                "INSERT INTO multi_source_kline(source_date,asset_type,asset_code,open,high,low,close,volume,amount,change_pct,provider,is_stale,raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [d, kind, ac, _number(row.get("open")), _number(row.get("high")), _number(row.get("low")),
                 _number(row.get("close")), _number(row.get("volume")), _number(row.get("amount")),
                 _number(row.get("change_pct") or row.get("pct")), provider, stale, _json(row)],
            )
            count += 1
        return count

    def _store_stock_flow(self, code, data, provider, stale):
        rows = data if isinstance(data, list) else []
        count = 0
        for row in rows:
            if not isinstance(row, dict) or not (row.get("date") or row.get("trade_date")):
                continue
            # Pre-open/blocked sector and quote endpoints sometimes return a
            # syntactically valid row with every capital-flow field missing.
            # Do not persist that placeholder as usable money-flow evidence.
            flow_fields = ("main_net", "super_net", "large_net", "mid_net", "small_net")
            if not any(row.get(field) not in (None, "", "-") for field in flow_fields):
                continue
            canonical = normalize_stock_flow_row(row, provider)
            d = _date(row.get("date") or row.get("trade_date"))
            stock = str(code or row.get("code") or "")
            values = [
                canonical["main_net"], canonical["net_total"], canonical["super_net"],
                canonical["large_net"], canonical["mid_net"], canonical["small_net"],
                _number(row.get("close")), _number(row.get("change_pct") or row.get("pct")),
                _number(row.get("turnover")), canonical["amount_unit"],
                canonical["flow_definition"], canonical["source_api"],
                canonical["origin_provider"], canonical["field_mapping_version"], stale,
                _json(row), d, stock, provider,
            ]
            # DuckDB's ART unique index keeps a deleted key visible until the
            # surrounding transaction commits.  DELETE + INSERT therefore
            # fails on the second atomic market refresh even though the
            # business key is unchanged.  Update an existing observation in
            # place, then insert only when the key does not exist.  This also
            # works in fresh test databases before the unique index is added.
            self.con.execute(
                "UPDATE multi_source_stock_flow SET "
                "main_net=?,net_total=?,super_net=?,large_net=?,mid_net=?,small_net=?,"
                "close=?,change_pct=?,turnover=?,amount_unit=?,flow_definition=?,source_api=?,"
                "origin_provider=?,field_mapping_version=?,is_stale=?,raw_json=?,"
                "fetched_at=current_timestamp "
                "WHERE source_date=? AND stock_code=? AND provider=?",
                values,
            )
            self.con.execute(
                "INSERT INTO multi_source_stock_flow(source_date,stock_code,main_net,net_total,super_net,large_net,mid_net,small_net,close,change_pct,turnover,provider,amount_unit,flow_definition,source_api,origin_provider,field_mapping_version,is_stale,raw_json) "
                "SELECT ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,? "
                "WHERE NOT EXISTS (SELECT 1 FROM multi_source_stock_flow "
                "WHERE source_date=? AND stock_code=? AND provider=?)",
                [
                    d, stock, canonical["main_net"], canonical["net_total"],
                    canonical["super_net"], canonical["large_net"], canonical["mid_net"],
                    canonical["small_net"], _number(row.get("close")),
                    _number(row.get("change_pct") or row.get("pct")),
                    _number(row.get("turnover")), provider, canonical["amount_unit"],
                    canonical["flow_definition"], canonical["source_api"],
                    canonical["origin_provider"], canonical["field_mapping_version"], stale,
                    _json(row), d, stock, provider,
                ],
            )
            count += 1
        return count

    def _store_sector_flow(self, data, provider, trade_date, stale):
        rows = data if isinstance(data, list) else []
        count = 0
        d_default = _date(trade_date) or date.today().isoformat()
        for row in rows:
            if not isinstance(row, dict) or not row.get("sector_code"):
                continue
            # A pre-open Eastmoney snapshot can contain sector names/prices
            # while all flow fields are ``-``.  Such rows are not a partial
            # flow measurement and must not make readiness look available.
            flow_fields = ("main_net", "super_net", "large_net", "mid_net", "small_net")
            if not any(row.get(field) not in (None, "", "-") for field in flow_fields):
                continue
            d = _date(row.get("date") or row.get("trade_date")) or d_default
            code = str(row.get("sector_code"))
            self.con.execute(
                "DELETE FROM multi_source_sector_flow WHERE source_date=? AND sector_code=? AND provider=?",
                [d, code, provider],
            )
            self.con.execute(
                "INSERT INTO multi_source_sector_flow(source_date,sector_code,sector_name,main_net,super_net,large_net,mid_net,small_net,change_pct,main_ratio,provider,sector_type,amount_unit,is_stale,raw_json) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [d, code, row.get("sector_name"), _number(row.get("main_net")), _number(row.get("super_net")),
                 _number(row.get("large_net")), _number(row.get("mid_net")), _number(row.get("small_net")),
                 _number(row.get("change_pct")), _number(row.get("main_ratio")), provider,
                 row.get("sector_type") or {
                     "derived_ths_stock_aggregate": "ths_concept",
                     "eastmoney_sector_full": "em_industry",
                     "tushare_sector_full": "tushare_dc_sector",
                 }.get(provider),
                 row.get("amount_unit") or "yuan", stale, _json(row)],
            )
            count += 1
        return count

    def _store_quote(self, data_type, code, data, provider, asset_type, stale, trade_date):
        if not isinstance(data, dict):
            return 0
        d = _date(data.get("date") or data.get("trade_date") or trade_date) or date.today().isoformat()
        ac = str(code or data.get("code") or "")
        self.con.execute(
            "DELETE FROM multi_source_quote WHERE source_date=? AND asset_type=? AND asset_code=? AND provider=?",
            [d, asset_type or data_type, ac, provider],
        )
        self.con.execute(
            "INSERT INTO multi_source_quote(source_date,asset_type,asset_code,name,price,change_pct,pe_ttm,pb,total_mv,circ_mv,provider,is_stale,raw_json) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [d, asset_type or data_type, ac, data.get("name"), _number(data.get("price")),
             _number(data.get("change_pct") or data.get("pct")), _number(data.get("pe_ttm")), _number(data.get("pb")),
             _number(data.get("total_mv")), _number(data.get("circ_mv")), provider, stale, _json(data)],
        )
        return 1

    def sync_sector_capital(self, trade_date: str | None = None) -> int:
        """Copy fresh sector flows into the project's existing sector capital chain."""
        try:
            date_clause = " AND source_date=CAST(? AS DATE)" if trade_date else ""
            params = [trade_date] if trade_date else []
            rows = self.con.execute(
                "SELECT source_date,sector_code,main_net,super_net,large_net,mid_net,small_net FROM ("
                "SELECT source_date,sector_code,main_net,super_net,large_net,mid_net,small_net,provider,fetched_at,"
                "row_number() OVER (PARTITION BY source_date,sector_code ORDER BY "
                "CASE WHEN provider='derived_ths_stock_aggregate' THEN 0 "
                "WHEN provider='eastmoney_sector_full' THEN 1 "
                "WHEN provider='eastmoney_market' THEN 2 ELSE 3 END, fetched_at DESC) AS _rn "
                "FROM multi_source_sector_flow WHERE is_stale=FALSE"
                " AND (main_net IS NOT NULL OR super_net IS NOT NULL OR large_net IS NOT NULL OR mid_net IS NOT NULL OR small_net IS NOT NULL)"
                + date_clause + ") WHERE _rn=1",
                params,
            ).fetchall()
            if not rows:
                return 0
            if trade_date:
                # Remove quote-only compatibility rows and duplicate providers
                # for this session before copying the authoritative selection.
                self.con.execute("DELETE FROM sector_capital WHERE date=CAST(? AS DATE)", [trade_date])
            for row in rows:
                self.con.execute(
                    "INSERT INTO sector_capital(date,sector_code,main_net_inflow,super_net_inflow,big_net_inflow,mid_net_inflow,small_net_inflow) VALUES (?,?,?,?,?,?,?)",
                    list(row),
                )
            self.con.commit()
            return len(rows)
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
                "SELECT date,stock_code,main_net_inflow,super_net_inflow,big_net_inflow "
                "FROM (SELECT date,stock_code,main_net_inflow,super_net_inflow,big_net_inflow, "
                "row_number() OVER (PARTITION BY date,stock_code ORDER BY try_cast(time AS TIME) DESC NULLS LAST, fetched_at DESC) AS rn "
                "FROM advanced_zjmm_min WHERE " + where + ") q WHERE rn=1",
                [trade_date, trade_date],
            ).fetchall()
            if not rows:
                return 0
            if trade_date is not None:
                self.con.execute(
                    "DELETE FROM multi_source_stock_flow WHERE provider='kpl' AND source_date=CAST(? AS DATE)",
                    [trade_date],
                )
            for row in rows:
                self.con.execute(
                    "DELETE FROM multi_source_stock_flow WHERE provider='kpl' AND source_date=? AND stock_code=?",
                    [row[0], row[1]],
                )
                self.con.execute(
                    "INSERT INTO multi_source_stock_flow(source_date,stock_code,main_net,super_net,large_net,mid_net,small_net,close,change_pct,turnover,provider,amount_unit,flow_definition,source_api,origin_provider,field_mapping_version,is_stale,raw_json) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [row[0], row[1], _number(row[2]), _number(row[3]), _number(row[4]),
                     None, None, None, None, None, "kpl", "yuan",
                     "provider_main_orders_net", "advanced_zjmm_min", "kpl",
                     "stock_flow_v2", False,
                     '{"migrated_from":"advanced_zjmm_min","aggregation":"latest_cumulative_point"}'],
                )
            self.con.commit()
            return len(rows)
        except Exception:
            try:
                self.con.rollback()
            except Exception:
                pass
            return 0

    def sync_core_klines(self, asset_type: str | None = None) -> int:
        """Populate the existing kline/index_kline chains from fresh migrated rows."""
        try:
            where = "provider <> 'existing_core' AND is_stale=FALSE"
            params = []
            if asset_type:
                where += " AND asset_type=?"
                params.append(asset_type)
            count = 0
            existing_tables = {row[0] for row in self.con.execute("show tables").fetchall()}
            for kind, target in (("stock", "kline"), ("index", "index_kline")):
                if asset_type and asset_type != kind:
                    continue
                if target not in existing_tables:
                    continue
                self.con.execute(
                    f"DELETE FROM {target} WHERE EXISTS (SELECT 1 FROM multi_source_kline s "
                    f"WHERE s.asset_type=? AND s.provider <> 'existing_core' AND s.is_stale=FALSE "
                    f"AND s.source_date={target}.date AND s.asset_code={target}.{'stock_code' if kind == 'stock' else 'index_code'} AND {target}.ktype='D')",
                    [kind],
                )
                if kind == "stock":
                    self.con.execute(
                        "INSERT INTO kline(date,stock_code,open,high,low,close,volume,turnover,change_pct,ktype,raw_json) "
                        "SELECT source_date,asset_code,open,high,low,close,CAST(COALESCE(volume,0) AS BIGINT),CAST(COALESCE(amount,0) AS BIGINT),change_pct,'D',raw_json "
                        "FROM (SELECT *, row_number() OVER (PARTITION BY source_date,asset_code "
                        "ORDER BY is_stale ASC, fetched_at DESC NULLS LAST, provider) AS _rn "
                        f"FROM multi_source_kline WHERE {where} AND asset_type='stock') ranked WHERE _rn=1", params,
                    )
                else:
                    self.con.execute(
                        "INSERT INTO index_kline(date,index_code,open,high,low,close,volume,turnover,change_pct,ktype,raw_json) "
                        "SELECT CAST(source_date AS DATE),asset_code,open,high,low,close,CAST(COALESCE(volume,0) AS BIGINT),CAST(COALESCE(amount,0) AS BIGINT),change_pct,'D',raw_json "
                        "FROM (SELECT *, row_number() OVER (PARTITION BY source_date,asset_code "
                        "ORDER BY is_stale ASC, fetched_at DESC NULLS LAST, provider) AS _rn "
                        f"FROM multi_source_kline WHERE {where} AND asset_type='index') ranked WHERE _rn=1", params,
                    )
                count += self.con.execute(
                    "SELECT count(*) FROM multi_source_kline WHERE " + where + f" AND asset_type='{kind}'", params
                ).fetchone()[0]
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
        try:
            self.con.execute("DELETE FROM multi_source_kline WHERE provider='existing_core'")
            self.con.execute(
                "INSERT INTO multi_source_kline(source_date,asset_type,asset_code,open,high,low,close,volume,amount,change_pct,provider,is_stale,raw_json) "
                "SELECT date,'stock',stock_code,open,high,low,close,volume,turnover,change_pct,'existing_core',FALSE,"
                "COALESCE(raw_json, '{\"migrated_from\":\"kline\"}') FROM kline"
            )
            counts["kline"] = self.con.execute("SELECT count(*) FROM multi_source_kline WHERE provider='existing_core'").fetchone()[0]
        except Exception:
            self.con.rollback()

        try:
            self.con.execute(
                "INSERT INTO multi_source_kline(source_date,asset_type,asset_code,open,high,low,close,volume,amount,change_pct,provider,is_stale,raw_json) "
                "SELECT try_cast(date AS DATE),'index',index_code,open,high,low,close,volume,turnover,change_pct,'existing_core',FALSE,"
                "COALESCE(raw_json, '{\"migrated_from\":\"index_kline\"}') FROM index_kline"
            )
            counts["index_kline"] = self.con.execute("SELECT count(*) FROM multi_source_kline WHERE provider='existing_core' AND asset_type='index'").fetchone()[0]
        except Exception:
            self.con.rollback()

        try:
            self.con.execute("DELETE FROM multi_source_sector_flow WHERE provider='existing_core'")
            self.con.execute(
                "INSERT INTO multi_source_sector_flow(source_date,sector_code,sector_name,main_net,super_net,large_net,mid_net,small_net,change_pct,main_ratio,provider,sector_type,amount_unit,is_stale,raw_json) "
                "SELECT date,sector_code,NULL,main_net_inflow,super_net_inflow,big_net_inflow,mid_net_inflow,small_net_inflow,NULL,NULL,'existing_core','legacy_core','yuan',FALSE,'{\"migrated_from\":\"sector_capital\"}' FROM sector_capital"
            )
            counts["sector_capital"] = self.con.execute("SELECT count(*) FROM multi_source_sector_flow WHERE provider='existing_core'").fetchone()[0]
        except Exception:
            self.con.rollback()
        self.con.commit()
        return counts

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
