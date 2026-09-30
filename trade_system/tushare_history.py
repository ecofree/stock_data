"""Batch, resumable TuShare history collection for calendar-year analysis.

The relay accepts date-wide ``daily``/``daily_basic`` requests.  ``moneyflow``
is paged at 1,000 rows, then normalized into the project's source-aware flow
tables.  Every date/dataset is committed before the next request starts.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import math
import json
import hashlib
from pathlib import Path
import time
from typing import Any, Iterable

import duckdb

from trade_system.data_store import DuckDBStore
from trade_system.schema import init_schema
from trade_system.tushare_store import (
    MARKET_FIELDS, bulk_replace, index_code_to_ts_code, market_batch,
    stock_code_to_ts_code, store_reference, ts_code_to_stock_code,
)
from trade_system.flow_contract import ensure_stock_flow_contract, normalize_stock_flow_row
from trade_system.xiaodefa_source import XiaodefaClient, XiaodefaError
from trade_system.units import _number as _num
from trade_system.http_transport import diagnostic_budget, diagnostic_state


CHECKPOINT_DATE = "1900-01-01"
# Stock order buckets share one request and one atomic snapshot.
MONEYFLOW_MAIN_FIELDS = (
    "ts_code,trade_date,buy_elg_amount,sell_elg_amount,buy_lg_amount,sell_lg_amount,net_mf_amount"
)
MONEYFLOW_SIZE_FIELDS = (
    "ts_code,trade_date,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
    "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount"
)
# DC bucket fields are already net yuan; this endpoint has no sell buckets.
INDUSTRY_FIELDS = (
    "trade_date,content_type,ts_code,name,pct_change,close,net_amount,"
    "net_amount_rate,buy_elg_amount,buy_lg_amount,buy_md_amount,buy_sm_amount"
)
STOCK_SNAPSHOT_DATASETS = {"daily", "daily_basic", "adj_factor"}


def _iso(value: str | date) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    raise ValueError(f"invalid date: {value}")


def _ymd(value: str | date) -> str:
    return _iso(value).replace("-", "")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def derive_valuation(ts_code, trade_date, inputs, *, observed_at):
    """Separate calculated values from dated, compatible valuation evidence.

    Inputs carry original receipt hashes and clocks. This never overwrites a
    provider row or certifies daily_basic; consumers must retain the provenance.
    Negative adjusted equity yields a signed PB with a risk status, not zero.
    """
    day = date.fromisoformat(_iso(trade_date))
    cutoff = datetime.fromisoformat(str(observed_at))
    if cutoff.tzinfo is None:
        raise ValueError('explicit observation timezone required')
    from zoneinfo import ZoneInfo
    market_zone = ZoneInfo('Asia/Shanghai')
    if day > cutoff.astimezone(market_zone).date():
        raise ValueError('future valuation session')
    units = {'price': 'yuan', 'total_share': '10000_shares', 'float_share': '10000_shares',
             'equity': 'yuan', 'other_equity': 'yuan', 'total_mv': '10000_yuan', 'circ_mv': '10000_yuan'}
    semantics = {'price': {'official_close', 'suspension_reference'},
                 'total_share': {'total_shares'}, 'float_share': {'circulating_shares'},
                 'equity': {'parent_equity'}, 'other_equity': {'other_equity_tools'},
                 'total_mv': {'total_market_value'}, 'circ_mv': {'circulating_market_value'}}
    values, failures, clocks, available_clocks, receipts = {}, {}, [], [], {}
    result = {'ts_code': ts_code, 'trade_date': day.isoformat(), 'values': {},
              'field_status': {}, 'missing_inputs': failures, 'certifies_daily_basic': False,
              'observation_at': cutoff.isoformat(), 'input_receipts': receipts}
    native_market_values = all(f in inputs for f in ('total_mv', 'circ_mv'))
    required = (('total_mv', 'circ_mv', 'equity', 'other_equity') if native_market_values
                else ('price', 'total_share', 'float_share', 'equity', 'other_equity'))
    for field in required:
        item = inputs.get(field, {})
        value = _num(item.get('value'))
        reasons = []
        if value is None or not math.isfinite(value):
            reasons.append('missing_or_nonfinite')
        if item.get('ts_code') != ts_code or item.get('unit') != units[field]:
            reasons.append('identity_or_unit')
        if item.get('semantic') not in semantics[field]:
            reasons.append('definition_unverified')
        digest = item.get('receipt_sha256', '')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(ch not in '0123456789abcdef' for ch in digest)):
            reasons.append('receipt_missing')
        try:
            source_day = date.fromisoformat(item['source_date'])
            through = date.fromisoformat(item['valid_through'])
            available = datetime.fromisoformat(item['available_at'])
            received = datetime.fromisoformat(item['received_at'])
            if (available.tzinfo is None or received.tzinfo is None
                    or not source_day <= day <= through
                    or available > cutoff or received > cutoff
                    or available.astimezone(market_zone).date() > day):
                reasons.append('input_not_available_at_observation')
            if field in {'price', 'total_share', 'float_share', 'total_mv', 'circ_mv'} and source_day != day:
                reasons.append('same_session_input_required')
            if not reasons:
                clocks.append(received)
                available_clocks.append(available)
        except (KeyError, TypeError, ValueError):
            reasons.append('input_time_missing')
        if reasons:
            failures[field] = sorted(set(reasons))
        else:
            values[field] = value
            receipts[field] = digest
    price = inputs.get('price', {})
    if not native_market_values and price.get('semantic') == 'suspension_reference':
        # Reuse of yesterday's price is an explicit valuation policy, not a
        # fabricated same-day trade. Both dated suspension and action review
        # must be supported; the original price date is retained.
        for field, expected in (('suspension', 'full_day_suspended'),
                                ('corporate_actions', 'reference_price_applicable')):
            proof = inputs.get(field, {})
            digest = proof.get('receipt_sha256', '')
            if (proof.get('ts_code') != ts_code or proof.get('trade_date') != day.isoformat()
                    or proof.get('status') != expected or not isinstance(digest, str)
                    or len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest)):
                failures[field] = ['dated_evidence_required']
            else:
                receipts[field] = digest
            try:
                proof_at = datetime.fromisoformat(proof['received_at'])
                if proof_at.tzinfo is None or proof_at > cutoff:
                    raise ValueError('proof after observation')
                clocks.append(proof_at)
            except (KeyError, TypeError, ValueError):
                failures[field] = ['proof_time_missing_or_future']
        try:
            if date.fromisoformat(price['price_date']) > day:
                raise ValueError('future reference price')
            result['price_date'] = price['price_date']
        except (KeyError, TypeError, ValueError):
            failures['price_date'] = ['original_price_date_required']
        if any(f in failures for f in ('suspension', 'corporate_actions', 'price_date')):
            values.pop('price', None)
    if 'price' in values and values['price'] <= 0:
        failures['price'] = ['nonpositive_price']
        values.pop('price')
    for field in ('total_share', 'float_share'):
        if field in values and values[field] <= 0:
            failures[field] = ['nonpositive_shares']
            values.pop(field)
    if ('total_share' in values and 'float_share' in values
            and values['float_share'] > values['total_share']):
        failures['float_share'] = ['exceeds_total_shares']
        values.pop('float_share')
    for target, share in (('total_mv', 'total_share'), ('circ_mv', 'float_share')):
        if 'price' in values and share in values:
            value = values['price'] * values[share]  # yuan * 10000 shares -> 10000 yuan
            if math.isfinite(value):
                result['values'][target] = value
                result['field_status'][target] = 'derived'
    if native_market_values:
        for field in ('total_mv', 'circ_mv'):
            if field in values and values[field] > 0:
                result['values'][field] = values[field]
                result['field_status'][field] = 'observed'
            elif field in values:
                failures[field] = ['nonpositive_market_value']
        if all(f in result['values'] for f in ('total_mv', 'circ_mv')):
            if result['values']['circ_mv'] > result['values']['total_mv']:
                failures['circ_mv'] = ['exceeds_total_market_value']
                result['values'].pop('circ_mv')
    if all(f in values for f in ('equity', 'other_equity')):
        if any(inputs['equity'].get(f) != inputs['other_equity'].get(f)
               for f in ('source_date', 'receipt_sha256')):
            failures['equity'] = ['inconsistent_financial_statement']
            values.pop('equity')
    if all(f in values for f in ('equity', 'other_equity')) and 'total_mv' in result['values']:
        denominator = values['equity'] - values['other_equity']
        if values['other_equity'] < 0 or not math.isfinite(denominator):
            failures['other_equity'] = ['invalid_adjusted_equity']
        elif denominator == 0:
            result['field_status']['pb'] = 'undefined_zero_adjusted_equity'
        else:
            pb = result['values']['total_mv'] * 10000 / denominator
            if math.isfinite(pb):
                result['values']['pb'] = pb
                result['field_status']['pb'] = ('negative_adjusted_equity' if denominator < 0 else 'derived')
    for field in ('pb', 'total_mv', 'circ_mv', 'pe'):
        result['field_status'].setdefault(field, 'unknown')
    result['input_received_at_min'] = min(clocks).isoformat() if clocks else None
    result['input_received_at_max'] = max(clocks).isoformat() if clocks else None
    result['input_available_at_min'] = min(available_clocks).isoformat() if available_clocks else None
    result['input_available_at_max'] = max(available_clocks).isoformat() if available_clocks else None
    result['status'] = ('derived_core_fields' if all(f in result['values'] for f in ('pb', 'total_mv', 'circ_mv'))
                        and not failures else 'incomplete')
    return result


class TushareHistoryCollector:
    def __init__(self, db_path: str | Path, *, client: XiaodefaClient | None = None,
                 request_timeout: int = 20, retries: int = 3,
                 batch_limit: int = 5000, moneyflow_page_size: int = 1000,
                 budget_seconds: float = 300.0, offline: bool = False, connection=None):
        # Validate the only approved client before opening a business database.
        self.client = client if client is not None else (None if offline else XiaodefaClient(
            timeout=request_timeout, max_retries=retries))
        self.db_path = str(db_path)
        self.offline = offline
        self.store = DuckDBStore(self.db_path, read_only=offline, connection=connection)
        try:
            if offline:
                # Bind the planning queries without initializing or migrating tables.
                for table, columns in {
                    'history_fetch_checkpoint': 'dataset,trade_date,page_no,status',
                    'tushare_trade_cal': 'exchange,cal_date,is_open',
                    'tushare_stock_basic': 'ts_code,list_date,delist_date',
                    'multi_source_observation': 'data_type,provider,payload_json,observed_at',
                }.items():
                    self.store.conn.execute(f'SELECT {columns} FROM {table} LIMIT 0')
            else:
                init_schema(self.store.conn)
                ensure_stock_flow_contract(self.store.conn)
        except Exception:
            self.store.close()
            raise
        self._last_source_provider = self._provider_name(self.client)
        self.batch_limit = max(100, int(batch_limit))
        self.moneyflow_page_size = max(100, min(int(moneyflow_page_size), 1000))
        self.budget_seconds = max(1.0, float(budget_seconds))
        self.started = time.monotonic()
        self._input_received = {}
        # Per-table operations, not unique instruments or network requests.
        # Only transaction owners may add committed writes. Receipts, schema,
        # checkpoints and certifications are deliberately outside these scopes.
        self._product_counts = {}

    def close(self) -> None:
        self.store.close()
        from trade_system.collection_profiles import emit_product_counts
        for scope, counts in self._product_counts.items():
            emit_product_counts('tushare_history.' + scope, **counts)
        self._product_counts.clear()

    def _count_product(self, scope, *, parsed=0, written=0, reused=False):
        counts = self._product_counts.setdefault(scope, {'rows_parsed': 0, 'rows_written': 0})
        counts['rows_parsed'] += parsed
        counts['rows_written'] += written
        if reused:
            counts['receipt_reused'] = True

    def __enter__(self) -> "TushareHistoryCollector":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _budget_left(self) -> bool:
        return time.monotonic() - self.started < self.budget_seconds

    @contextmanager
    def _transaction(self, enabled=True):
        if enabled:
            self.store.conn.execute('BEGIN TRANSACTION')
        try:
            yield
            if enabled:
                self.store.conn.execute('COMMIT')
        except Exception:
            if enabled:
                self.store.conn.execute('ROLLBACK')
            raise

    def _validate_stock_snapshot(self, dataset: str, rows: list[Any], trade_date: str) -> None:
        """Reject an empty/short real close snapshot before publishing it."""
        if not self._is_production_source():
            return
        if not rows:
            raise XiaodefaError(f"empty {dataset} response for {_iso(trade_date)}")
        expected = self._expected_stock_codes(trade_date, dataset)
        observed = {r["ts_code"] if isinstance(r, dict) else r[0] for r in rows}
        reference = self._reference_version() or {}
        if reference.get('membership_date') == _iso(trade_date) and observed & set(reference.get('not_listed', [])):
            raise XiaodefaError('price response conflicts with official listing membership')
        if dataset == 'daily_basic':
            observed = {r['ts_code'] for r in rows if isinstance(r, dict)
                        and (pb := _num(r.get('pb'))) is not None and math.isfinite(pb)
                        and (total := _num(r.get('total_mv'))) is not None and math.isfinite(total) and total > 0
                        and (floating := _num(r.get('circ_mv'))) is not None and math.isfinite(floating) and 0 < floating <= total}
        if dataset == 'daily_basic':
            observed |= set(self.qualified_valuation_reviews(trade_date))
        if dataset in {'daily', 'daily_basic', 'moneyflow'} and expected - observed and self._suspension_rows(trade_date) is None:
            self._read_rows('suspend_d', {'trade_date': _ymd(trade_date)},
                            'ts_code,trade_date,suspend_timing,suspend_type')
            expected = self._expected_stock_codes(trade_date, dataset)
        if dataset == 'daily_basic' and expected and expected - observed:
            # Retain valid rows without exempting missing valuation/share fields.
            suspended = sorted((expected - observed) - self._expected_stock_codes(trade_date, 'daily'))
            self._record_snapshot('daily_basic_gaps', {
                'trade_date': _iso(trade_date), 'missing_codes': sorted(expected - observed),
                'full_day_suspended': suspended,
                'supplemental_fields': self._suspended_basic_evidence(trade_date, suspended),
                'reviewed_valuation': self.qualified_valuation_reviews(trade_date),
                'field_status': {'turnover_rate': 'not_applicable_only_if_full_day_suspended',
                    'volume_ratio': 'unknown', 'pe': 'unknown', 'pb': 'unknown',
                    'total_mv': 'unknown', 'circ_mv': 'unknown'},
                'acceptance': 'incomplete_not_certified'})
            return
        if not expected or expected - observed:
            raise XiaodefaError(f"incomplete {dataset} response: "
                               f"{len(expected - observed)} missing instruments; expected universe={len(expected)}")

    @diagnostic_budget()
    def collect_valuation_inputs(self, trade_date, codes):
        """Explicit bounded repair diagnostic, never an automatic close retry.

        Callers authorize a new diagnostic before invoking this method. The
        shared transport enforces two attempts per endpoint/six total, including
        pagination and retries. No provider daily_basic row is replaced here.
        """
        if self.offline:
            raise ValueError('read-only collector cannot acquire valuation inputs')
        session = date.fromisoformat(_iso(trade_date))
        if session > date.today():
            raise ValueError('future valuation session')
        fields = ('ts_code,ann_date,f_ann_date,end_date,report_type,update_flag,'
                  'total_hldr_eqy_exc_min_int,oth_eqt_tools')
        results = {}
        for code in sorted(set(codes)):
            if code != stock_code_to_ts_code(code):
                raise ValueError('explicit canonical security required')
            if diagnostic_state.get()['stopped'] or not self._budget_left():
                results[code] = {'status': 'not_requested_budget_or_stop'}
                continue
            # The documented default is type 1 (latest consolidated), NOT all
            # report types. Request it explicitly and never certify a complete
            # revision inventory merely because pagination finished.
            params = {'ts_code': code, 'start_date': f'{session.year-1}0101',
                      'end_date': _ymd(trade_date), 'report_type': '1'}
            inventory = self.store.conn.execute(
                "SELECT payload_json,payload_hash FROM multi_source_observation "
                "WHERE data_type='tushare_balancesheet_inventory_snapshot' AND provider=? "
                "AND json_extract_string(payload_json,'$.params.ts_code')=? "
                "AND observed_at BETWEEN current_timestamp-INTERVAL 1 DAY AND current_timestamp "
                'ORDER BY observed_at DESC LIMIT 1', [self._provider_name(self.client), code]).fetchone()
            completed = (json.loads(inventory[0]) if inventory and
                         hashlib.sha256(inventory[0].encode()).hexdigest() == inventory[1] else {})
            retained = self.valuation_completion_report(trade_date, [code])['rows'][code]
            if (completed.get('params') == params
                    and completed.get('scope') == 'latest_consolidated_in_requested_interval'):
                results[code] = {'status': 'retained_statement_reused',
                                 'financial_input': retained.get('financial_input'), 'inventory_sha256': inventory[1]}
                continue
            try:
                rows = self._read_rows('balancesheet', params, fields)
                if any(r.get('ts_code') != code or str(r.get('report_type')) != '1' for r in rows):
                    raise XiaodefaError('financial statement identity/report type mismatch')
                self._record_snapshot('balancesheet_inventory', {'params': params, 'rows': rows,
                    'scope': 'latest_consolidated_in_requested_interval',
                    'requested_report_types': ['1'], 'complete_revision_inventory': False,
                    'qualification': 'request_completed_not_latest_applicable_statement_certification'})
                results[code] = {'status': 'received_not_certified', 'rows': len(rows)}
            except Exception as exc:
                results[code] = {'status': 'unavailable', 'error_type': type(exc).__name__}
        state = diagnostic_state.get()
        budget = {'attempts': state['attempts'], 'stopped': state['stopped'],
                  'endpoints': [{'host': host, 'path': path, 'attempts': count}
                                for (host, path), count in state['endpoints'].items()]}
        return {'requests': results, 'transport_budget': budget,
                'completion': self.valuation_completion_report(trade_date, results),
                'certifies_daily_basic': False}

    def _valuation_review(self, review, trade_date, observed_at):
        """Validate an explicitly reviewed bundle against retained source bytes.

        SHA bindings establish integrity, not the truth of a review. The named
        reviewer owns the financial revision inventory and policy interpretation;
        a supplier response cannot self-authorize either. No bare qualified flag
        or unbound numeric input is accepted.
        """
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Shanghai')
        day = _iso(trade_date)
        cutoff = datetime.fromisoformat(observed_at)
        if cutoff.tzinfo is None:
            raise ValueError('explicit observation timezone required')
        if review.get('schema') != 'reviewed_valuation_inputs_v1' or review.get('trade_date') != day:
            raise ValueError('valuation review schema/session mismatch')
        reviewed = datetime.fromisoformat(review['reviewed_at'])
        if not str(review.get('reviewed_by', '')).strip() or reviewed.tzinfo is None or reviewed > cutoff:
            raise ValueError('dated reviewer required')
        code = review['ts_code']
        if code != stock_code_to_ts_code(code):
            raise ValueError('canonical valuation identity required')
        documents = {}
        for digest in review['source_receipts']:
            records = self.store.conn.execute(
                'SELECT payload_json,observed_at FROM multi_source_observation WHERE payload_hash=?',
                [digest]).fetchall()
            valid = [(raw, arrival.replace(tzinfo=zone) if arrival.tzinfo is None else arrival)
                     for raw, arrival in records if hashlib.sha256(raw.encode()).hexdigest() == digest]
            valid = [(raw, arrival) for raw, arrival in valid if arrival <= reviewed]
            if not valid:
                raise ValueError('review source receipt absent, changed or received after review')
            raw, arrival = min(valid, key=lambda item: item[1])
            documents[digest] = (json.loads(raw), arrival.isoformat())
        inventory = review['financial_inventory']
        if (inventory.get('as_of') != day or inventory.get('scope') != 'all_published_consolidated_revisions'
                or not inventory.get('selection_reason') or not inventory.get('document_receipts')
                or any(h not in documents for h in inventory['document_receipts'])
                or inventory.get('selected_statement_receipt') not in documents
                or inventory.get('selected_statement_receipt') not in inventory.get('statement_receipts', [])
                or any(h not in documents for h in inventory.get('statement_receipts', []))):
            raise ValueError('complete dated financial revision inventory required')
        inputs = json.loads(_json(review['inputs']))
        for field, item in inputs.items():
            digest = item['receipt_sha256']
            if digest not in documents:
                raise ValueError('unbound valuation input')
            payload, arrival = documents[digest]
            item['received_at'] = arrival  # The bundle cannot refresh source age.
            if field in {'suspension', 'corporate_actions'}:
                if not item.get('review_basis'):
                    raise ValueError('documented suspension/action interpretation required')
                continue
            value = payload
            parent = None
            for part in item['value_path']:
                parent, value = value, value[part]
            if isinstance(parent, dict) and parent.get('ts_code', code) != code:
                raise ValueError('valuation source identity mismatch')
            if isinstance(value, bool) or _num(value) is None or _num(value) != _num(item.get('value')):
                raise ValueError('valuation value does not match retained source')
            if not isinstance(parent, dict) or parent.get('ts_code') != code:
                raise ValueError('valuation source row identity required')
            if field in {'equity', 'other_equity'}:
                native = {'equity': 'total_hldr_eqy_exc_min_int', 'other_equity': 'oth_eqt_tools'}[field]
                announcement = _iso(parent.get('f_ann_date') or parent['ann_date'])
                if (item['value_path'][-1] != native or str(parent.get('report_type')) not in {'1', '4'}
                        or _iso(parent['end_date']) > announcement or announcement > day
                        or item['source_date'] != announcement):
                    raise ValueError('financial period, announcement or field mapping mismatch')
            else:
                if not parent.get('trade_date'):
                    raise ValueError('valuation source session required')
                source_day = _iso(parent['trade_date'])
                expected_day = item.get('price_date') if item.get('semantic') == 'suspension_reference' else day
                if source_day != expected_day:
                    raise ValueError('valuation source session mismatch')
        statement = inventory['selected_statement_receipt']
        if any(inputs.get(f, {}).get('receipt_sha256') != statement for f in ('equity', 'other_equity')):
            raise ValueError('financial inputs differ from reviewed statement')
        # A newer retained revision invalidates an earlier review inventory.
        for raw, digest in self.store.conn.execute(
                "SELECT payload_json,payload_hash FROM multi_source_observation WHERE data_type='tushare_balancesheet'").fetchall():
            if hashlib.sha256(raw.encode()).hexdigest() != digest:
                continue
            for row in json.loads(raw).get('rows', []):
                if row.get('ts_code') != code:
                    continue
                announcement = _iso(row.get('f_ann_date') or row.get('ann_date', ''))
                if announcement <= day and digest not in inventory.get('statement_receipts', []):
                    raise ValueError('financial revision inventory omits retained statement')
        # A claimed full-day suspension cannot override observed trading.
        if inputs.get('price', {}).get('semantic') == 'suspension_reference':
            suspension = self._suspension_rows(day) or []
            if not any(r['ts_code'] == code and r.get('suspend_type') == 'S'
                       and r.get('suspend_timing') in (None, '') for r in suspension):
                raise ValueError('full-day suspension source missing')
            snapshot = self.store.conn.execute(
                "SELECT payload_hash FROM multi_source_observation WHERE data_type='tushare_suspend_d_snapshot' "
                "AND status='qualified' AND provider=? AND json_extract_string(payload_json,'$.params.trade_date')=? "
                'ORDER BY observed_at DESC LIMIT 1',
                ['xiaodefa' if self.offline else self._provider_name(self.client), _ymd(day)]).fetchone()
            if not snapshot or inputs.get('suspension', {}).get('receipt_sha256') != snapshot[0]:
                raise ValueError('suspension proof does not bind qualified snapshot')
            traded = self.store.conn.execute(
                'SELECT count(*) FROM tushare_daily WHERE date=? AND ts_code=? AND (volume>0 OR turnover>0)',
                [day, code]).fetchone()[0]
            if traded:
                raise ValueError('suspension conflicts with retained trading')
        result = derive_valuation(code, day, inputs, observed_at=observed_at)
        result['reviewed_by'] = review['reviewed_by']
        result['reviewed_at'] = review['reviewed_at']
        result['financial_inventory'] = inventory
        result['source_receipts'] = sorted(documents)
        result['valuation_eligible'] = result['status'] == 'derived_core_fields'
        # Daily-basic raw provenance is never relabelled as a provider result.
        result['qualification'] = 'reviewed_derived_valuation' if result['valuation_eligible'] else 'incomplete'
        return result

    def import_valuation_reviews(self, path, expected_sha256, trade_date):
        """Explicit, hash-pinned local intake; no network or provider-row update."""
        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValueError('valuation evidence file hash mismatch')
        bundle = json.loads(raw)
        reviews = bundle['reviews']
        if not reviews or len({r['ts_code'] for r in reviews}) != len(reviews):
            raise ValueError('empty or duplicate valuation review scope')
        now = datetime.now(timezone.utc).isoformat()
        results = [self._valuation_review(r, trade_date, now) for r in reviews]
        if not all(r['valuation_eligible'] for r in results):
            raise ValueError('valuation review inputs incomplete')
        with self._transaction(True):
            for review in reviews:
                payload = _json(dict(review=review, import_bundle_sha256=expected_sha256))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                if not self.store.conn.execute(
                        "SELECT 1 FROM multi_source_observation WHERE data_type='valuation_review' AND payload_hash=?",
                        [digest]).fetchone():
                    self.store.conn.execute(
                        'INSERT INTO multi_source_observation(data_type,asset_type,asset_code,provider,status,payload_json,payload_hash) '
                        "VALUES ('valuation_review','stock',?,'reviewed_evidence','reviewed',?,?)",
                        [review['ts_code'], payload, digest])
        return {'trade_date': _iso(trade_date), 'rows': results, 'market_requests': 0,
                'raw_daily_basic_overwritten': False, 'bundle_sha256': expected_sha256}

    def qualified_valuation_reviews(self, trade_date, *, observed_at=None):
        """Revalidate underlying receipts on every read; never trust cached green."""
        now = observed_at or datetime.now(timezone.utc).isoformat()
        results, seen = {}, set()
        records = self.store.conn.execute(
            "SELECT payload_json,payload_hash FROM multi_source_observation WHERE data_type='valuation_review' "
            "AND json_extract_string(payload_json,'$.review.trade_date')=? ORDER BY observed_at DESC,rowid DESC",
            [_iso(trade_date)]).fetchall()
        for raw, digest in records:
            try:
                payload = json.loads(raw)
                code = payload['review']['ts_code']
                if code in seen:
                    continue
                seen.add(code)  # A broken newer review must not revive an older one.
                if hashlib.sha256(raw.encode()).hexdigest() != digest:
                    continue
                result = self._valuation_review(payload['review'], trade_date, now)
                if result['valuation_eligible']:
                    results[code] = dict(result, review_sha256=digest)
            except (KeyError, TypeError, ValueError, IndexError, AttributeError):
                continue
        return results

    def valuation_completion_report(self, trade_date, codes, *, observed_at=None):
        """Read retained receipts once; never issue a diagnostic request here.

        BAK PB/BVPS is useful arithmetic evidence but has no verified daily_basic
        denominator mapping. A prior close cannot silently become today's close.
        Every unresolved security remains in the report, including null-PB rows.
        """
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Shanghai')
        at = datetime.fromisoformat(observed_at) if observed_at else datetime.now(timezone.utc)
        if at.tzinfo is None:
            raise ValueError('explicit observation timezone required')
        day = _iso(trade_date)
        wanted = set(codes)
        provider = 'xiaodefa' if self.offline else self._provider_name(self.client)
        retained = {code: {} for code in sorted(wanted)}
        rejected = []
        types = ('tushare_daily_basic', 'tushare_daily', 'tushare_stk_premarket', 'tushare_bak_basic')
        receipts = self.store.conn.execute(
            "SELECT data_type,payload_json,payload_hash,observed_at FROM multi_source_observation "
            "WHERE data_type IN (?,?,?,?) AND provider=? "
            "AND json_extract_string(payload_json,'$.params.trade_date')=? "
            "ORDER BY observed_at DESC,payload_hash", [*types, provider, _ymd(trade_date)]).fetchall()
        for typ, raw, digest, arrival in receipts:
            received = arrival.replace(tzinfo=zone) if arrival.tzinfo is None else arrival
            if hashlib.sha256(raw.encode()).hexdigest() != digest or received > at:
                rejected.append({'receipt_sha256': digest, 'reason': 'hash_or_arrival_invalid'})
                continue
            payload = json.loads(raw)
            api = typ.removeprefix('tushare_')
            if payload.get('api') != api or payload.get('error_type'):
                continue
            rows = payload.get('rows', [])
            identities = [r.get('ts_code') for r in rows if isinstance(r, dict)]
            for row in rows:
                code = row.get('ts_code')
                if code not in wanted or api in retained[code]:
                    continue
                if identities.count(code) != 1 or row.get('trade_date') != _ymd(trade_date):
                    rejected.append({'receipt_sha256': digest, 'ts_code': code, 'reason': 'identity_or_date'})
                    continue
                retained[code][api] = {'row': row, 'receipt_sha256': digest, 'received_at': received.isoformat()}
        # Financial inputs have announcement/report dates, not trade_date.
        # Only retained original consolidated statements are eligible; absent
        # other-equity instruments remain unknown, never assumed to be zero.
        financial = {}
        for raw, digest, arrival in self.store.conn.execute(
                "SELECT payload_json,payload_hash,observed_at FROM multi_source_observation "
                "WHERE data_type='tushare_balancesheet' AND provider=? ORDER BY observed_at DESC",
                [provider]).fetchall():
            received = arrival.replace(tzinfo=zone) if arrival.tzinfo is None else arrival
            if hashlib.sha256(raw.encode()).hexdigest() != digest or received > at:
                continue
            payload = json.loads(raw)
            if payload.get('api') != 'balancesheet' or payload.get('error_type'):
                continue
            for row in payload.get('rows', []):
                code = row.get('ts_code')
                if code not in wanted or str(row.get('report_type')) != '1':
                    continue
                try:
                    announcement = _iso(row.get('f_ann_date') or row['ann_date'])
                    period = _iso(row['end_date'])
                    date.fromisoformat(announcement)
                    date.fromisoformat(period)
                except (KeyError, TypeError, ValueError):
                    continue
                if period > announcement or announcement > day:
                    continue
                rank = (period, announcement, received)
                if code not in financial or rank > financial[code]['rank']:
                    financial[code] = dict(row=row, receipt_sha256=digest, received_at=received.isoformat(),
                                           rank=rank, announcement=announcement)
        output = {}
        reviewed_values = self.qualified_valuation_reviews(day, observed_at=at.isoformat())
        for code, sources in retained.items():
            inputs = {}
            for field, native, semantic, unit in (
                    ('price', 'close', 'official_close', 'yuan'),
                    ('total_share', 'total_share', 'total_shares', '10000_shares'),
                    ('float_share', 'float_share', 'circulating_shares', '10000_shares')):
                source = sources.get('daily_basic', {})
                if source.get('row', {}).get(native) is None:
                    source = sources.get('daily' if field == 'price' else 'stk_premarket', {})
                value = source.get('row', {}).get(native)
                if value is not None:
                    inputs[field] = dict(value=value, unit=unit, semantic=semantic, ts_code=code,
                        source_date=day, valid_through=day, available_at=day+'T15:00:00+08:00',
                        received_at=source['received_at'], receipt_sha256=source['receipt_sha256'])
            native_source = sources.get('daily_basic', {})
            for field, semantic in (('total_mv', 'total_market_value'), ('circ_mv', 'circulating_market_value')):
                value = native_source.get('row', {}).get(field)
                if value is not None:
                    inputs[field] = dict(value=value, unit='10000_yuan', semantic=semantic, ts_code=code,
                        source_date=day, valid_through=day, available_at=day+'T15:00:00+08:00',
                        received_at=native_source['received_at'], receipt_sha256=native_source['receipt_sha256'])
            balance = financial.get(code)
            if balance:
                for field, native, semantic in (('equity', 'total_hldr_eqy_exc_min_int', 'parent_equity'),
                                                ('other_equity', 'oth_eqt_tools', 'other_equity_tools')):
                    if balance['row'].get(native) is not None:
                        inputs[field] = dict(value=balance['row'][native], unit='yuan', semantic=semantic,
                            ts_code=code, source_date=balance['announcement'], valid_through=day,
                            available_at=balance['announcement']+'T23:59:59+08:00',
                            received_at=balance['received_at'], receipt_sha256=balance['receipt_sha256'])
            result = derive_valuation(code, day, inputs, observed_at=at.isoformat())
            if balance:
                result['financial_input'] = dict(receipt_sha256=balance['receipt_sha256'],
                    report_period=balance['row']['end_date'], announcement_date=balance['announcement'],
                    status='latest_retained_original_statement_not_full_revision_inventory')
                # A latest *retained* report is not proof that no newer or
                # restated report was published by this session. Preserve the
                # useful calculation, but require the dated revision inventory.
                result['missing_inputs']['financial_revision_inventory'] = ['dated_complete_revision_inventory_required']
                result['status'] = 'incomplete'
            if code in reviewed_values:
                result = reviewed_values[code]
            result['retained_sources'] = {api: {k: v for k, v in source.items() if k != 'row'}
                                          for api, source in sources.items()}
            native_pb = _num(sources.get('daily_basic', {}).get('row', {}).get('pb'))
            raw_pb = sources.get('daily_basic', {}).get('row', {}).get('pb')
            result['native_pb_status'] = ('finite' if native_pb is not None and math.isfinite(native_pb)
                                         else 'row_absent' if 'daily_basic' not in sources
                                         else 'provider_null' if raw_pb is None else 'invalid_value')
            pre = sources.get('stk_premarket', {}).get('row', {})
            bak = sources.get('bak_basic', {}).get('row', {})
            price, bvps, pb = (_num(pre.get('pre_close')), _num(bak.get('bvps')), _num(bak.get('pb')))
            if all(v is not None and math.isfinite(v) for v in (price, bvps, pb)) and price > 0 and bvps != 0:
                implied = price / bvps
                if math.isfinite(implied):
                    result['alternative_pb_check'] = dict(reported_pb=pb, reference_price_div_bvps=implied,
                        agrees_at_reported_precision=abs(implied-pb) <= 0.005000001,
                        status='arithmetic_only', required=['denominator_definition', 'suspension_price_policy',
                                                           'corporate_action_review', 'original_price_date'])
            output[code] = result
        return {'trade_date': day, 'observed_at': at.isoformat(), 'rows': output,
                'rejected_receipts': rejected, 'market_requests': 0, 'certifies_daily_basic': False}

    @diagnostic_budget()
    def _suspended_basic_evidence(self, trade_date, codes):
        """Retain dated alternatives separately; none certifies daily_basic."""
        from trade_system.http_transport import request_budget
        results = {}
        deadline = min(self.started + self.budget_seconds, time.monotonic() + 30)
        for code in codes:
            evidence = results[code] = {}
            for api, fields in (
                ('stk_premarket', 'total_share,float_share,pre_close'),
                ('bak_basic', 'pe,pb,bvps,list_date'),
            ):
                params = {'ts_code': code, 'trade_date': _ymd(trade_date)}
                cached = self.store.conn.execute(
                    "SELECT payload_json,payload_hash,observed_at FROM multi_source_observation "
                    "WHERE data_type=? AND provider=? AND asset_code=? "
                    "AND json_extract_string(payload_json,'$.params.trade_date')=? "
                    "AND observed_at BETWEEN current_timestamp-INTERVAL 1 DAY AND current_timestamp "
                    "ORDER BY observed_at DESC LIMIT 1",
                    ['tushare_'+api, self._provider_name(self.client), code, _ymd(trade_date)]).fetchone()
                entry = evidence[api] = {'status': 'unknown', 'provider': self._provider_name(self.client),
                    'source_trade_date': _iso(trade_date), 'certifies_daily_basic': False}
                try:
                    payload = json.loads(cached[0]) if cached and hashlib.sha256(cached[0].encode()).hexdigest() == cached[1] else None
                    if payload is not None and payload.get('params') == params and payload.get('api') == api:
                        rows = payload.get('rows', [])
                        if rows or (datetime.now()-cached[2]).total_seconds() < 900:
                            entry.update(received_at=cached[2].isoformat(), raw_payload_hash=cached[1], reused=True)
                            self._count_product(api, parsed=len(rows), reused=True)
                            if payload.get('error_type'):
                                entry.update(status='retry_cooldown', error_type=payload['error_type'])
                                continue
                        else:
                            payload = None
                    else:
                        payload = None
                    if payload is None:
                        if diagnostic_state.get()['stopped']:
                            entry.update(status='diagnostic_stopped', reason=diagnostic_state.get()['stopped'])
                            continue
                        if len(codes)>20 or time.monotonic() >= deadline:
                            entry['status'] = 'budget_exhausted'
                            continue
                        with request_budget(deadline-time.monotonic()):
                            rows = self._read_rows(api, params, 'ts_code,trade_date,'+fields)
                        receipt = self.store.conn.execute(
                            "SELECT observed_at,payload_hash FROM multi_source_observation "
                            "WHERE data_type=? AND asset_code=? AND provider=? "
                            "AND json_extract_string(payload_json,'$.params.trade_date')=? "
                            "ORDER BY observed_at DESC LIMIT 1",
                            ['tushare_'+api, code, self._provider_name(self.client), _ymd(trade_date)]).fetchone()
                        if receipt:
                            entry.update(received_at=receipt[0].isoformat(), raw_payload_hash=receipt[1], reused=False)
                    if (len(rows) != 1 or rows[0].get('ts_code') != code
                            or rows[0].get('trade_date') != _ymd(trade_date)):
                        entry['status'] = 'unavailable_or_identity_mismatch'
                        continue
                    row = rows[0]
                    values = {field: _num(row.get(field)) for field in fields.split(',') if field != 'list_date'}
                    if any(v is not None and not math.isfinite(v) for v in values.values()):
                        entry['status'] = 'invalid_nonfinite_value'
                        continue
                    if api == 'stk_premarket':
                        if (any(value is None or value <= 0 for value in values.values())
                                or values['float_share'] > values['total_share']):
                            entry['status'] = 'invalid_share_or_reference_price'
                            continue
                        entry.update(values=values, share_unit='10000_shares', price_semantics='previous_close')
                    else:
                        # Dynamic PE is a different metric; zero is not usable PE.
                        entry.update(values={'pb': values['pb'], 'bvps': values['bvps'],
                            'pe_dynamic': values['pe'] if values['pe'] is not None and values['pe'] > 0 else None},
                            pe_static_status='unknown')
                    entry['status'] = 'observed_alternative' if any(v is not None for v in entry['values'].values()) else 'unknown'
                except Exception as exc:
                    entry.update(status='unavailable', error_type=type(exc).__name__)
                    encoded = _json({'api': api, 'params': params, 'rows': [], 'error_type': type(exc).__name__})
                    self.store.conn.execute(
                        "INSERT INTO multi_source_observation(data_type,asset_type,asset_code,provider,status,payload_json,payload_hash) "
                        "VALUES (?,'receipt',?,?,'error',?,?)",
                        ['tushare_'+api, code, self._provider_name(self.client), encoded,
                         hashlib.sha256(encoded.encode()).hexdigest()])
        return results

    def _expected_stock_codes(self, trade_date, dataset=None):
        expected = {r[0] for r in self.store.conn.execute(
            "SELECT DISTINCT ts_code FROM tushare_stock_basic WHERE ts_code IS NOT NULL "
            "AND (list_date IS NULL OR list_date<=CAST(? AS DATE)) "
            "AND (delist_date IS NULL OR delist_date>CAST(? AS DATE))", [_iso(trade_date)] * 2).fetchall()}
        reference = self._reference_version() or {}
        if reference.get('membership_only') and reference.get('membership_date') != _iso(trade_date):
            raise XiaodefaError('historical listing dates unavailable for membership-only identities')
        if reference.get('membership_date') == _iso(trade_date):
            expected -= set(reference.get('not_listed', []))
        if dataset in {'daily', 'moneyflow'}:
            rows = self._suspension_rows(trade_date)
            if rows is not None:
                suspended = {r['ts_code'] for r in rows if r.get('trade_date') == _ymd(trade_date)
                             and r.get('suspend_type') == 'S' and r.get('suspend_timing') in (None, '')}
                traded = {r[0] for r in self.store.conn.execute(
                    "SELECT ts_code FROM tushare_daily WHERE date=? AND (volume>0 OR turnover>0)",
                    [_iso(trade_date)]).fetchall()}
                # Independent full-day evidence needs no invented zero-price row.
                # Conflicting traded evidence keeps the instrument required.
                expected -= suspended - traded
        return expected

    def _suspension_rows(self, trade_date):
        receipt = self.store.conn.execute(
            "SELECT payload_json,payload_hash FROM multi_source_observation WHERE data_type='tushare_suspend_d_snapshot' "
            "AND status='qualified' AND provider=? AND json_extract_string(payload_json,'$.params.trade_date')=? "
            "ORDER BY observed_at DESC LIMIT 1",
            ['xiaodefa' if self.offline else self._provider_name(self.client), _ymd(trade_date)]).fetchone()
        if not receipt or hashlib.sha256(receipt[0].encode()).hexdigest() != receipt[1]:
            return None
        payload = json.loads(receipt[0])
        rows = payload.get('rows')
        if not isinstance(rows, list) or len({r.get('ts_code') for r in rows}) != len(rows) or any(
                not r.get('ts_code') or r.get('trade_date') != _ymd(trade_date)
                or r.get('suspend_type') not in {'S', 'R'} for r in rows):
            return None
        return rows

    def _is_production_source(self) -> bool:
        return isinstance(self.client, XiaodefaClient)

    @staticmethod
    def _provider_name(source: Any) -> str:
        return "xiaodefa" if isinstance(source, XiaodefaClient) else "custom"





    def _is_complete_stock_table(self, table: str, date_column: str, trade_date: str) -> bool:
        expected = self._expected_stock_codes(trade_date, table.removeprefix("tushare_"))
        return bool(expected) and expected <= self._covered_codes(table.removeprefix("tushare_"), trade_date)

    def _certify_close_snapshot(self, dataset: str, trade_date: str, *, status: str,
                                error_message: str = "", provider: str = "tushare") -> None:
        table_by_dataset = {
            "daily": ("tushare_daily", "date", "close"),
            "daily_basic": ("tushare_daily_basic", "date", None),
            "adj_factor": ("tushare_adj_factor", "date", "adj_factor"),
        }
        spec = table_by_dataset.get(dataset)
        if not spec:
            return
        table, date_column, value_column = spec
        expected = len(self._expected_stock_codes(trade_date, dataset))
        observed = int(self.store.conn.execute(
            f"SELECT count(*) FROM {table} WHERE {date_column}=?", [_iso(trade_date)]
        ).fetchone()[0] or 0)
        distinct_codes = int(self.store.conn.execute(
            f"SELECT count(DISTINCT ts_code) FROM {table} WHERE {date_column}=? AND ts_code IS NOT NULL",
            [_iso(trade_date)],
        ).fetchone()[0] or 0)
        invalid_rows = 0
        if value_column:
            invalid_rows = int(self.store.conn.execute(
                f"SELECT count(*) FROM {table} WHERE {date_column}=? AND ({value_column} IS NULL OR {value_column} <= 0)",
                [_iso(trade_date)],
            ).fetchone()[0] or 0)
        coverage = round(distinct_codes * 100.0 / expected, 4) if expected else None
        if dataset == 'daily_basic':
            qualified = self._covered_codes(dataset, trade_date)
            applicable = self._expected_stock_codes(trade_date, dataset)
            raw_codes = {r[0] for r in self.store.conn.execute(
                'SELECT ts_code FROM tushare_daily_basic WHERE date=?', [_iso(trade_date)]).fetchall()}
            invalid_rows = len((raw_codes | applicable) - qualified) + observed - len(raw_codes)
            reviewed_codes = set(self.qualified_valuation_reviews(trade_date))
            observed += len(reviewed_codes - raw_codes)
            distinct_codes = len(raw_codes | reviewed_codes)
            if reviewed_codes:
                error_message = 'coverage includes reviewed derived valuation; raw provider rows retained'
            coverage = round(len(qualified & applicable)*100.0/expected, 4) if expected else None
        certified = (
            status == "certified"
            and expected >= 1000
            and self._is_complete_stock_table(table, date_column, trade_date)
            and invalid_rows == 0
        )
        final_status = "certified" if certified else (status if status != "certified" else "incomplete")
        self.store.conn.execute(
            "INSERT INTO close_snapshot_certification "
            "(dataset,trade_date,provider,expected_rows,observed_rows,distinct_codes,invalid_rows,coverage_pct,source_event_date,fetched_at,status,error_message) "
            "VALUES (?,?,?,?,?,?,?,?,CAST(? AS DATE),current_timestamp,?,?) "
            "ON CONFLICT(dataset,trade_date,provider) DO UPDATE SET "
            "expected_rows=excluded.expected_rows,observed_rows=excluded.observed_rows,distinct_codes=excluded.distinct_codes,"
            "invalid_rows=excluded.invalid_rows,coverage_pct=excluded.coverage_pct,source_event_date=excluded.source_event_date,"
            "fetched_at=excluded.fetched_at,status=excluded.status,error_message=excluded.error_message",
            [dataset, _iso(trade_date), provider, expected, observed, distinct_codes, invalid_rows,
             coverage, _iso(trade_date), final_status, error_message[:500]],
        )
        self.store.conn.commit()

    def _checkpoint(self, dataset: str, trade_date: str, status: str, *, rows: int = 0,
                    attempts: int = 0, error: str = "") -> None:
        now = datetime.now()
        self.store.conn.execute(
            "INSERT INTO history_fetch_checkpoint "
            "(dataset,trade_date,page_no,status,rows_written,attempts,last_error,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT (dataset,trade_date,page_no) DO UPDATE SET status=excluded.status,"
            "rows_written=excluded.rows_written,attempts=excluded.attempts,last_error=excluded.last_error,"
            "updated_at=excluded.updated_at",
            [dataset, _iso(trade_date), 0, status, rows, attempts, error[:500], now],
        )
        # Autocommit outside a publication; inside it, the caller owns commit.

    def _is_done(self, dataset: str, trade_date: str, force: bool) -> bool:
        if force:
            return False
        row = self.store.conn.execute(
            "SELECT status FROM history_fetch_checkpoint WHERE dataset=? AND trade_date=? AND page_no=0",
            [dataset, _iso(trade_date)],
        ).fetchone()
        if not row or row[0] != "success":
            return False
        table_by_dataset = {
            "daily": ("tushare_daily", "date"),
            "daily_basic": ("tushare_daily_basic", "date"),
            "adj_factor": ("tushare_adj_factor", "date"),
            "moneyflow": ("tushare_moneyflow", "date"),
            "industry_flow": ("tushare_moneyflow_industry", "trade_date"),
        }
        if dataset == "stock_basic":
            return self._reference_version() is not None
        table_info = table_by_dataset.get(dataset)
        if table_info:
            table, date_column = table_info
            count = int(self.store.conn.execute(
                f"SELECT count(*) FROM {table} WHERE {date_column}=?", [_iso(trade_date)]
            ).fetchone()[0] or 0)
            # A success checkpoint with zero stored rows means an empty relay
            # response was recorded as success (observed 2026-08-10 on
            # daily/daily_basic).  Treat it as not-done so the next run
            # re-fetches the date instead of permanently skipping it.
            if count == 0:
                return False
            if dataset in STOCK_SNAPSHOT_DATASETS | {"moneyflow"} and not self._is_complete_stock_table(table, date_column, trade_date):
                return False
        return True

    def _next_attempt(self, dataset: str, trade_date: str) -> int:
        row = self.store.conn.execute(
            "SELECT attempts FROM history_fetch_checkpoint "
            "WHERE dataset=? AND trade_date=? AND page_no=0",
            [dataset, _iso(trade_date)],
        ).fetchone()
        return int((row[0] if row else 0) or 0) + 1

    def _read_rows(self, api, params, fields):
        if self.client is None:
            raise XiaodefaError("offline collector cannot acquire data")
        # Reuse only a completed L/D acquisition, never splice partial pages from
        # different attempts. Its receipt time is not refreshed by reuse.
        if api == 'stock_basic' and self._is_production_source() and not getattr(self, '_force_reference', False):
            cached = self.store.conn.execute(
                "SELECT payload_json,payload_hash FROM multi_source_observation "
                "WHERE data_type='tushare_stock_basic_acquisition_snapshot' AND provider=? "
                "AND status='qualified' AND CAST(observed_at AS DATE)=current_date "
                "AND observed_at BETWEEN current_timestamp-INTERVAL 15 MINUTE AND current_timestamp "
                "ORDER BY observed_at DESC", [self._provider_name(self.client)]).fetchall()
            for encoded, digest in cached:
                if hashlib.sha256(encoded.encode()).hexdigest() != digest:
                    continue
                value = json.loads(encoded)
                if value.get('params') == params and value.get('fields') == fields:
                    return value['rows']
        # Keep the original acquisition time and source identity even when a
        # later page or coverage validation fails. Reuse the existing receipt table.
        def record(offset, rows):
            self._count_product(api, parsed=len(rows))
            arrival = datetime.now()
            for row in rows:
                self._input_received[(api, row.get('ts_code'), row.get('trade_date'))] = arrival
            payload = _json({"source": "tushare", "delivery": self._provider_name(self.client),
                             "api": api, "params": params, "offset": offset, "rows": rows})
            self.store.conn.execute(
                "INSERT INTO multi_source_observation "
                "(data_type,asset_type,asset_code,provider,status,payload_json,payload_hash,observed_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                ["tushare_" + api, "receipt", params.get("ts_code"), self._provider_name(self.client),
                 "received_unverified", payload, hashlib.sha256(payload.encode()).hexdigest(), arrival])
        from trade_system.http_transport import request_budget
        remaining = self.started + self.budget_seconds - time.monotonic()
        if remaining <= 0:
            raise XiaodefaError('collection request budget exhausted')
        with request_budget(remaining):
            if self._is_production_source():
                rows = self.client.query_all(api, page_size=self.batch_limit, fields=fields,
                                             on_page=record, **({'total_timeout': min(60, remaining)} if api == 'stock_basic' else {}), **params)
            else:
                rows = self.client.query_rows(api, params, fields)
                record(0, rows)
        if api == 'stock_basic' and self._is_production_source():
            self._record_snapshot('stock_basic_acquisition', {'params': params, 'fields': fields, 'rows': rows})
        if api == 'suspend_d':
            if (len({r.get('ts_code') for r in rows}) != len(rows) or any(
                    not r.get('ts_code') or r.get('trade_date') != params['trade_date']
                    or r.get('suspend_type') not in {'S', 'R'} for r in rows)):
                raise XiaodefaError('ambiguous suspension evidence')
            # A completed request, not the last pagination page, supplies negative evidence.
            self._record_snapshot(api, {'params': params, 'rows': rows})
        return rows

    def _record_snapshot(self, dataset, payload):
        encoded = _json(payload)
        self.store.conn.execute(
            "INSERT INTO multi_source_observation(data_type,asset_type,provider,status,payload_json,payload_hash) "
            "VALUES (?,'reference',?,'qualified',?,?)",
            ['tushare_' + dataset + '_snapshot', self._provider_name(self.client), encoded,
             hashlib.sha256(encoded.encode()).hexdigest()])

    def _reference_rows(self):
        return self.store.conn.execute(
            'SELECT ts_code,stock_code,stock_name,area,industry,market,list_date,delist_date '
            'FROM tushare_stock_basic ORDER BY ts_code').fetchall()

    def _reference_version(self):
        from trade_system.ths_quality import qualified_stock_reference
        return qualified_stock_reference(self.store.conn,
            provider='xiaodefa' if self.offline else self._provider_name(self.client))

    def _exchange_listing_membership(self, exchange='SZ'):
        """Dated complete exchange inventory, only for unresolved native dates."""
        import io
        import re
        import urllib.request
        import zipfile
        from xml.etree import ElementTree as ET
        from trade_system.http_transport import read_verified_once, request_budget
        today = date.today().isoformat()
        cached = self.store.conn.execute(
            "SELECT payload_json,payload_hash FROM multi_source_observation "
            "WHERE data_type=? AND provider=? "
            "AND observed_at BETWEEN current_timestamp-INTERVAL 15 MINUTE AND current_timestamp "
            "ORDER BY observed_at DESC LIMIT 1", [('szse' if exchange == 'SZ' else 'sse')+'_listing_membership',
                                                   'szse' if exchange == 'SZ' else 'sse']).fetchone()
        if cached and hashlib.sha256(cached[0].encode()).hexdigest() == cached[1]:
            value = json.loads(cached[0])
            if value.get('as_of') == today:
                return value
        if exchange not in {'SZ', 'SH'}:
            raise ValueError('unsupported listing exchange')
        if exchange == 'SH':
            import urllib.parse
            base = 'https://query.sse.com.cn/sseQuery/commonQuery.do'
            listings, receipts = {}, []
            with request_budget(min(30, self.budget_seconds-(time.monotonic()-self.started))):
                for board in ('1', '8'):
                    params = {'STOCK_TYPE':board, 'sqlId':'COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L',
                        'COMPANY_STATUS':'2,4,5,7,8', 'type':'inParams', 'isPagination':'true',
                        'pageHelp.pageSize':'10000', 'pageHelp.pageNo':'1', 'pageHelp.beginPage':'1',
                        'pageHelp.endPage':'1', 'pageHelp.cacheSize':'1'}
                    raw = read_verified_once(urllib.request.Request(base+'?'+urllib.parse.urlencode(params),
                        headers={'User-Agent':'Mozilla/5.0','Referer':'https://www.sse.com.cn/assortment/stock/list/share/'}),
                        timeout=12,max_bytes=3_000_000)
                    data = json.loads(raw); rows = data.get('result'); page = data.get('pageHelp',{})
                    if (not isinstance(rows,list) or not 100<=len(rows)<=2000 or page.get('pageNo')!=1
                            or page.get('pageCount')!=1 or page.get('total')!=len(rows)):
                        raise XiaodefaError('incomplete SSE listing inventory')
                    for row in rows:
                        code, listed = row.get('A_STOCK_CODE',''), row.get('LIST_DATE','')
                        if (not re.fullmatch(r'6\d{5}',code) or code in listings
                                or row.get('STOCK_TYPE')!=board or not listed
                                or not date(1990,1,1)<=date.fromisoformat(_iso(listed))<=date.today()):
                            raise XiaodefaError('invalid or duplicate SSE listing identity')
                        listings[code] = _iso(listed)
                    receipts.append(dict(board=board,rows=len(rows),sha256=hashlib.sha256(raw).hexdigest()))
            value = dict(as_of=today,recordcount=len(listings),listings=listings,source=base,receipts=receipts)
            encoded = _json(value)
            self.store.conn.execute("INSERT INTO multi_source_observation(data_type,asset_type,provider,status,payload_json,payload_hash) "
                "VALUES ('sse_listing_membership','reference','sse','qualified',?,?)",
                [encoded,hashlib.sha256(encoded.encode()).hexdigest()])
            return value
        base = 'https://www.szse.cn/api/report/ShowReport'
        with request_budget(min(30, self.budget_seconds - (time.monotonic()-self.started))):
            meta_raw = read_verified_once(urllib.request.Request(
                base+'/data?SHOWTYPE=JSON&CATALOGID=1110&TABKEY=tab1&PAGENO=1'), timeout=15, max_bytes=500_000)
            tab = json.loads(meta_raw.decode('utf-8'))[0]
            meta = tab['metadata']
            count = meta['recordcount']
            if (meta.get('subname', '').strip() != today or type(count) is not int or not 1000 <= count <= 10000
                    or meta.get('pageno') != 1 or len(tab['data']) != meta.get('pagesize')):
                raise XiaodefaError('undated or incomplete exchange listing metadata')
            raw = read_verified_once(urllib.request.Request(
                base+'?SHOWTYPE=xlsx&CATALOGID=1110&TABKEY=tab1'), timeout=15, max_bytes=3_000_000)
        ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            if sum(info.file_size for info in archive.infolist()) > 30_000_000:
                raise XiaodefaError('exchange listing archive exceeds budget')
            strings = [''.join(x.itertext()) for x in ET.fromstring(archive.read('xl/sharedStrings.xml')).findall('s:si', ns)]
            sheet = ET.fromstring(archive.read('xl/worksheets/sheet1.xml'))
        rows = []
        for row in sheet.findall('s:sheetData/s:row', ns):
            cells = {}
            for cell in row.findall('s:c', ns):
                text = cell.findtext('s:v', '', ns)
                if cell.get('t') == 's':
                    text = strings[int(text)]
                elif cell.get('t') == 'inlineStr':
                    text = ''.join(cell.find('s:is', ns).itertext())
                cells[re.sub(r'\d', '', cell.attrib['r'])] = text
            rows.append(cells)
        if not rows or rows[0].get('E') != 'A股代码' or rows[0].get('G') != 'A股上市日期':
            raise XiaodefaError('exchange listing schema changed')
        listings = {r.get('E'): r.get('G') for r in rows[1:]}
        if (len(rows)-1 != count or len(listings) != count
                or any(not re.fullmatch(r'\d{6}', code or '') or not listed
                       or not date(1990, 1, 1) <= date.fromisoformat(listed) <= date.today()
                       for code, listed in listings.items())
                or any(listings.get(r['agdm']) != r['agssrq'] for r in tab['data'])):
            raise XiaodefaError('incomplete or conflicting exchange listing inventory')
        value = {'as_of': today, 'recordcount': count, 'listings': listings,
                 'metadata_sha256': hashlib.sha256(meta_raw).hexdigest(),
                 'xlsx_sha256': hashlib.sha256(raw).hexdigest(), 'source': base}
        encoded = _json(value)
        self.store.conn.execute(
            "INSERT INTO multi_source_observation(data_type,asset_type,provider,status,payload_json,payload_hash) "
            "VALUES ('szse_listing_membership','reference','szse','qualified',?,?)",
            [encoded, hashlib.sha256(encoded.encode()).hexdigest()])
        return value

    @staticmethod
    def stock_listing_evidence(con, code):
        receipt = con.execute(
            "SELECT payload_json,payload_hash,observed_at FROM multi_source_observation "
            "WHERE data_type='stock_listing_reference' AND provider='hithink' AND asset_code=? "
            "AND observed_at>=current_timestamp-INTERVAL 1 DAY ORDER BY observed_at DESC LIMIT 1",
            [code]).fetchone()
        evidence = None
        if receipt and hashlib.sha256(receipt[0].encode()).hexdigest() == receipt[1]:
            cached = json.loads(receipt[0])
            items = cached.get('item', [])
            stamp = cached.get('timestamp')
            age = (datetime.now() - receipt[2]).total_seconds()
            if cached.get('error_type') and 0 <= age < 900:
                raise XiaodefaError('native listing reference retry cooldown')
            ttl = 86400 if len(items) == 1 and items[0].get('list_date') else 900
            if (type(stamp) in (int, float) and 0 <= time.time()-stamp/1000 <= 86400
                    and 0 <= age < ttl):
                evidence = cached
        if evidence is None:
            from trade_system.hithink_client import HiThinkClient
            native = HiThinkClient(timeout=10)
            try:
                evidence = native.stock_listing(code)
            except Exception as exc:
                evidence = {'error_type': type(exc).__name__, 'thscode': code}
                encoded = _json(evidence)
                con.execute("INSERT INTO multi_source_observation "
                    "(data_type,asset_type,asset_code,provider,status,payload_json,payload_hash) "
                    "VALUES ('stock_listing_reference','reference',?,'hithink','error',?,?)",
                    [code, encoded, hashlib.sha256(encoded.encode()).hexdigest()])
                raise
            encoded = _json(evidence)
            con.execute(
                "INSERT INTO multi_source_observation(data_type,asset_type,asset_code,provider,status,payload_json,payload_hash) "
                "VALUES ('stock_listing_reference','reference',?,'hithink','received_unverified',?,?)",
                [code, encoded, hashlib.sha256(encoded.encode()).hexdigest()])
        return evidence

    def _bse_listing_membership(self):
        """Complete official inventory, dated by its source session and verified calendar."""
        import re
        import urllib.parse
        import urllib.request
        from trade_system.http_transport import read_verified_once, request_budget
        today = date.today().isoformat()
        last = self.store.conn.execute("SELECT max(cal_date) FROM (SELECT cal_date FROM tushare_trade_cal "
            "WHERE cal_date<=? AND exchange IN ('SSE','SZSE') GROUP BY cal_date "
            "HAVING count(*)=2 AND count(DISTINCT exchange)=2 AND bool_and(is_open))", [today]).fetchone()[0]
        if last is None or self.ensure_calendar(str(last), today, allow_fetch=False) != [str(last)]:
            raise XiaodefaError('BSE reference requires a complete current exchange calendar')
        cached = self.store.conn.execute("SELECT payload_json,payload_hash FROM multi_source_observation "
            "WHERE data_type='bse_listing_membership' AND provider='bse' "
            "AND observed_at BETWEEN current_timestamp-INTERVAL 15 MINUTE AND current_timestamp "
            "ORDER BY observed_at DESC LIMIT 1").fetchone()
        if cached and hashlib.sha256(cached[0].encode()).hexdigest() == cached[1]:
            value = json.loads(cached[0])
            if value.get('as_of') == today and value.get('source_session') == str(last):
                return value
        url = 'https://www.bse.cn/nqxxController/nqxxCnzq.do'
        rows, pages, expected, total_pages = [], [], None, 1
        with request_budget(min(30, self.budget_seconds-(time.monotonic()-self.started))):
            page = 0
            while page < total_pages:
                body = urllib.parse.urlencode({'page':page,'typejb':'T','xxfcbj[]':'2',
                    'xxzqdm':'','sortfield':'xxzqdm','sorttype':'asc'}).encode()
                raw = read_verified_once(urllib.request.Request(url, data=body, headers={
                    'Content-Type':'application/x-www-form-urlencoded', 'User-Agent':'Mozilla/5.0',
                    'Referer':'https://www.bse.cn/nq/listedcompany.html',
                    'X-Requested-With':'XMLHttpRequest'}), timeout=10,max_bytes=500_000)
                text = raw.decode('utf-8').strip()
                if not text.startswith('null(') or not text.endswith(')'):
                    raise XiaodefaError('unexpected BSE inventory envelope')
                payload = json.loads(text[5:-1])
                if not isinstance(payload,list) or len(payload)!=1:
                    raise XiaodefaError('ambiguous BSE inventory response')
                item = payload[0]; batch = item.get('content')
                count, count_pages = item.get('totalElements'), item.get('totalPages')
                if (type(count) is not int or not 1<=count<=1000 or type(count_pages) is not int
                        or not 1<=count_pages<=50 or item.get('number')!=page or not isinstance(batch,list)
                        or not batch or (expected is not None and (count!=expected or count_pages!=total_pages))):
                    raise XiaodefaError('incomplete or changing BSE inventory pages')
                expected,total_pages=count,count_pages
                rows.extend(batch); pages.append({'page':page,'sha256':hashlib.sha256(raw).hexdigest(),
                    'received_at':datetime.now().isoformat()}); page+=1
        if (len(rows)!=expected or len({r.get('xxzqdm') for r in rows})!=expected or any(
                not re.fullmatch(r'\d{6}',r.get('xxzqdm','')) or r.get('xxfcbj')!='2'
                or r.get('xxjsrq')!=_ymd(last) for r in rows)):
            raise XiaodefaError('BSE inventory incomplete, duplicate or stale source session')
        # fxssrq may be a pre-2021 selected-tier date, not a BSE IPO date.
        value={'as_of':today,'source_session':str(last),'recordcount':expected,
            'listings':{r['xxzqdm']:r.get('fxssrq') for r in rows},'source':url,'pages':pages,
            'date_semantics':'current_inventory_no_intervening_open_session; IPO dates require native evidence'}
        encoded=_json(value)
        self.store.conn.execute("INSERT INTO multi_source_observation(data_type,asset_type,provider,status,payload_json,payload_hash) "
            "VALUES ('bse_listing_membership','reference','bse','qualified',?,?)",
            [encoded,hashlib.sha256(encoded.encode()).hexdigest()])
        return value

    def _collect_reference(self, dataset, start=None, end=None):
        if dataset == "trade_cal":
            params = {"start_date": start, "end_date": end}
            fields = "exchange,cal_date,is_open,pretrade_date"
        else:
            params = {"exchange": "", "list_status": "L"}
            fields = "ts_code,symbol,name,area,industry,market,list_date,list_status,delist_date"
        rows = []
        selectors = ([dict(params, exchange=e) for e in ("SSE", "SZSE")] if dataset == "trade_cal"
                     else [dict(params, list_status=s) for s in ("L", "D")])
        for request in selectors:
            batch = self._read_rows(dataset, request, fields)
            if dataset == "trade_cal":
                days = {_iso(r.get("cal_date")) for r in batch}
                if (len(days) != len(batch) or len(days) != (date.fromisoformat(_iso(end)) - date.fromisoformat(_iso(start))).days + 1
                        or any(r.get("exchange") != request["exchange"] or not _iso(start) <= _iso(r["cal_date"]) <= _iso(end)
                               or str(r.get("is_open")) not in {"0", "1"} for r in batch)):
                    raise XiaodefaError("incomplete or invalid exchange calendar response")
            elif ((request["list_status"] == "L" and not batch) or any(
                    r.get("list_status") != request["list_status"] or
                    (r["list_status"] == "D" and (not r.get("delist_date") or not r.get("list_date")
                     or _iso(r["delist_date"]) <= _iso(r["list_date"]))) for r in batch)):
                raise XiaodefaError("invalid stock lifecycle response")
            rows.extend(batch)
        if not rows:
            raise XiaodefaError(f"empty {dataset} response")
        if dataset == 'stock_basic':
            # Resolve only malformed dates, with retained official evidence.
            # Unknown dates are never replaced by subscription dates or dropped.
            corrections = []
            membership = None
            sh_membership = None
            bse_membership = self._bse_listing_membership() if self._is_production_source() else None
            not_listed = []
            membership_only = []
            for row in rows:
                try:
                    listed = date.fromisoformat(_iso(row.get('list_date')))
                    invalid_date = listed < date(1990, 1, 1) or listed > date.today()
                except (TypeError, ValueError):
                    invalid_date = True
                if not invalid_date or row.get('list_status') != 'L' or not self._is_production_source():
                    continue
                if len(corrections) >= 20 or not self._budget_left():
                    raise XiaodefaError('native listing repair budget exhausted')
                code = row.get('ts_code')
                evidence = self.stock_listing_evidence(self.store.conn, code)
                items = evidence.get('item', [])
                stamp = evidence.get('timestamp')
                if (len(items) > 1 or (items and (items[0].get('thscode') != code
                        or items[0].get('asset_type') != 'a-share'))
                        or type(stamp) not in (int, float)
                        or not 0 <= time.time() - stamp / 1000 <= 86400):
                    raise XiaodefaError('native listing receipt identity mismatch')
                replacement = items[0].get('list_date') if items else None
                corrections.append({'ts_code': code, 'original': row.get('list_date'),
                                    'list_date': replacement, 'provider': 'hithink'})
                if replacement is not None:
                    # A confirmed future listing is retained, but not yet in the
                    # applicable universe (_expected_stock_codes applies dates).
                    date.fromisoformat(replacement)
                    row['list_date'] = replacement
                elif code.endswith('.SZ'):
                    membership = membership or self._exchange_listing_membership()
                    if code[:6] not in membership['listings']:
                        not_listed.append(code)
                        row['list_date'] = None
                    else:
                        row['list_date'] = membership['listings'][code[:6]]
                        corrections[-1].update(list_date=row['list_date'], provider='szse')
                elif code.endswith('.BJ'):
                    bse_membership = bse_membership or self._bse_listing_membership()
                    if code[:6] not in bse_membership['listings']:
                        not_listed.append(code)
                        row['list_date'] = None
                    else:
                        row['list_date'] = None
                        membership_only.append(code)
                elif code.endswith('.SH'):
                    sh_membership = sh_membership or self._exchange_listing_membership('SH')
                    if code[:6] not in sh_membership['listings']:
                        not_listed.append(code)
                        row['list_date'] = None
                    else:
                        row['list_date'] = sh_membership['listings'][code[:6]]
                        corrections[-1].update(list_date=row['list_date'], provider='sse')
            if bse_membership:
                known = {row['ts_code'] for row in rows}
                for code in sorted(set(bse_membership['listings'])-{c[:6] for c in known if c.endswith('.BJ')}):
                    if len(corrections)>=20 or not self._budget_left():
                        raise XiaodefaError('native listing repair budget exhausted')
                    evidence=self.stock_listing_evidence(self.store.conn,code+'.BJ')
                    items=evidence.get('item',[]); stamp=evidence.get('timestamp')
                    if (len(items)!=1 or type(stamp) not in (int,float)
                            or not 0<=time.time()-stamp/1000<=86400):
                        raise XiaodefaError('native listing receipt identity mismatch')
                    native=items[0]
                    listed=native.get('list_date')
                    if (native.get('thscode')!=code+'.BJ' or native.get('asset_type')!='a-share'
                            or (listed is not None and not date(1990,1,1)<=date.fromisoformat(listed)<=date.fromisoformat(bse_membership['source_session']))):
                        raise XiaodefaError('BSE/native listing date disagreement: '+code)
                    if listed is None:
                        membership_only.append(code+'.BJ')
                    rows.append(dict(ts_code=code+'.BJ',symbol=code,name=native.get('name'),market='北交所',list_date=listed,list_status='L'))
                    corrections.append(dict(ts_code=code+'.BJ',list_date=listed,provider='hithink',membership_provider='bse'))
            if self._is_production_source():
                # A plausible listing date does not prove current membership:
                # retired/replaced codes can survive in the relay's L list.
                # Reconcile all current SH/SZ rows against dated full exchange
                # inventories, retaining historical rows and listing dates.
                for suffix in ('.SZ', '.SH'):
                    current = [r for r in rows if r.get('list_status') == 'L'
                               and str(r.get('ts_code','')).endswith(suffix)
                               and r.get('list_date') and _iso(r['list_date']) <= date.today().isoformat()]
                    if not current:
                        continue
                    if suffix == '.SZ':
                        membership = membership or self._exchange_listing_membership()
                        inventory = membership
                    else:
                        sh_membership = sh_membership or self._exchange_listing_membership('SH')
                        inventory = sh_membership
                    if inventory.get('as_of') != date.today().isoformat() or not inventory.get('listings'):
                        raise XiaodefaError('current exchange membership evidence required')
                    not_listed.extend(r['ts_code'] for r in current
                                      if r['ts_code'][:6] not in inventory['listings'])
                not_listed = sorted(set(not_listed))
            invalid = []
            for row in rows:
                if row.get('ts_code') in membership_only:
                    if (row.get('list_date') is not None or not bse_membership
                            or bse_membership.get('as_of') != date.today().isoformat()
                            or bse_membership.get('source_session') != date.today().isoformat()
                            or row['ts_code'][:6] not in bse_membership['listings']):
                        raise XiaodefaError('current membership evidence required for unknown listing date')
                    continue
                if row.get('ts_code') in not_listed:
                    continue
                try:
                    listed = date.fromisoformat(_iso(row.get('list_date')))
                    valid = bool(row.get('ts_code')) and date(1990, 1, 1) <= listed
                    if listed > date.today():
                        valid = valid and any(c['ts_code'] == row['ts_code'] and c['list_date'] == row['list_date'] for c in corrections)
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    invalid.append(str(row.get('ts_code') or '<missing>'))
            if invalid or len({r.get('ts_code') for r in rows}) != len(rows):
                raise XiaodefaError('invalid or unknown stock listing date/identity; '
                                   f'count={len(invalid)} codes={",".join(invalid[:20])}')
            with self._transaction():
                # Keep immutable old snapshots and raw receipts; replace only this projection.
                self.store.conn.execute('DELETE FROM tushare_stock_basic')
                count = store_reference(self.store, dataset, rows)
                snapshot = self._reference_rows()
                self._record_snapshot(dataset, {'rows': snapshot, 'scope': ['L', 'D'],
                    'version': hashlib.sha256(_json(snapshot).encode()).hexdigest(),
                    'listing_corrections': corrections,
                    'listing_membership': {'as_of': date.today().isoformat(), 'not_listed': not_listed,
                        'membership_only': sorted(membership_only), 'historical_listing_dates_complete': not membership_only,
                        'szse': {k:v for k,v in (membership or {}).items() if k!='listings'},
                        'sse': {k:v for k,v in (sh_membership or {}).items() if k!='listings'},
                        'bse': {k:v for k,v in (bse_membership or {}).items() if k!='listings'}}
                        if membership or sh_membership or bse_membership else {}})
                self._checkpoint(dataset, CHECKPOINT_DATE, 'success', rows=count, attempts=1)
            self._count_product(dataset, written=count)
            return count
        with self._transaction():
            count = store_reference(self.store, dataset, rows)
        self._count_product(dataset, written=count)
        return count

    def _query_date_batch(self, api: str, trade_date: str, fields: str, *, with_limit: bool = True) -> list[dict[str, Any]]:
        """One acquisition; page termination and coverage are independent checks."""
        target = _iso(trade_date)
        params = {"trade_date": _ymd(trade_date)}
        rows = self._read_rows(api, params, fields)
        if any(not r.get("ts_code") or not r.get("trade_date") or
               _iso(r["trade_date"]) != target for r in rows):
            raise XiaodefaError("response contains wrong session or missing identity")
        keys = [str(r["ts_code"]) for r in rows]
        if len(keys) != len(set(keys)):
            raise XiaodefaError("duplicate instrument in snapshot")
        self._validate_stock_snapshot(api, rows, trade_date)
        self._last_source_provider = self._provider_name(self.client)
        return rows



    def ensure_calendar(self, start_date: str, end_date: str, *, allow_fetch: bool = True) -> list[str]:
        start = date.fromisoformat(_iso(start_date))
        end = date.fromisoformat(_iso(end_date))
        if end < start:
            raise ValueError("end_date must be on or after start_date")
        expected_days = (end - start).days + 1

        def read_calendar():
            result = {}
            for day, opened in self.store.conn.execute(
                "SELECT CAST(cal_date AS VARCHAR),CASE WHEN count(*)=2 AND count(DISTINCT exchange)=2 "
                "AND count(is_open)=2 AND min(is_open)=max(is_open) THEN bool_and(is_open) END "
                "FROM tushare_trade_cal WHERE exchange IN ('SSE','SZSE') "
                "AND cal_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) GROUP BY cal_date",
                [start, end]).fetchall():
                if day in result and result[day] != opened:
                    raise XiaodefaError("conflicting exchange calendar states")
                result[day] = opened
            return result

        calendar = read_calendar()
        if len(calendar) < expected_days or None in calendar.values():
            if not allow_fetch:
                raise XiaodefaError("verified calendar required for offline planning")
            try:
                self._collect_reference("trade_cal", _ymd(start_date), _ymd(end_date))
            except Exception as exc:
                raise XiaodefaError(f"trade_cal fetch failed for {start}..{end}: {exc}") from exc
            calendar = read_calendar()
        if len(calendar) != expected_days or None in calendar.values():
            raise XiaodefaError(f"trade_cal incomplete for {start}..{end}: {len(calendar)}/{expected_days}")
        # Stored prices never substitute for the exchange calendar. Closed and
        # unknown sessions cannot be silently converted into trading days.
        return sorted(day for day, opened in calendar.items() if opened)

    def collect_stock_basic(self, *, force: bool = False) -> int:
        if self._is_done("stock_basic", CHECKPOINT_DATE, force):
            return 0
        self._checkpoint("stock_basic", CHECKPOINT_DATE, "running", attempts=1)
        self._force_reference = force
        try:
            return self._collect_reference("stock_basic")
        except Exception as exc:
            self._checkpoint("stock_basic", CHECKPOINT_DATE, "error", attempts=1, error=str(exc))
            raise
        finally:
            self._force_reference = False

    def _applicable_codes(self, codes, trade_date, dataset=None):
        known = {r[0] for r in self.store.conn.execute("SELECT ts_code FROM tushare_stock_basic").fetchall()}
        # Unknown identities remain required; only dated lifecycle facts exclude them.
        return set(codes) - (known - self._expected_stock_codes(trade_date, dataset))

    def _covered_codes(self, dataset, trade_date):
        predicate = "TRUE"
        if dataset in {"daily", "index_daily"}:
            predicate = "close>0 AND isfinite(close)"
        elif dataset == "adj_factor":
            predicate = "adj_factor>0 AND isfinite(adj_factor)"
        elif dataset == "moneyflow":
            predicate = "(isfinite(net_mf_amount) OR (" + " AND ".join(f"isfinite({f})" for f in
                ("buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount")) + "))"
        elif dataset == "daily_basic":
            # A lone PB/PE or turnover value does not establish valuation coverage.
            # PE may be NULL for loss-making issuers; never replace it with zero.
            predicate = ("isfinite(pb) AND isfinite(total_mv) AND total_mv>0 "
                         "AND isfinite(circ_mv) AND circ_mv>0 AND circ_mv<=total_mv")
        native = {r[0] for r in self.store.conn.execute(
            f"SELECT ts_code FROM tushare_{dataset} WHERE date=? AND {predicate} GROUP BY ts_code HAVING count(*)=1",
            [_iso(trade_date)]).fetchall()}
        if dataset == 'daily_basic':
            native |= set(self.qualified_valuation_reviews(trade_date))
        return native

    def _collect_market(self, dataset, trade_date, *, codes=None, force=False):
        expected = None if codes is None else (set(codes) if dataset == "index_daily"
                                               else self._applicable_codes(codes, trade_date, dataset))
        if expected is None:
            rows = self._query_date_batch(dataset, trade_date, MARKET_FIELDS[dataset])
        else:
            missing = expected if force else expected - self._covered_codes(dataset, trade_date)
            rows = []
            for code in sorted(missing):
                if not self._budget_left():
                    raise XiaodefaError("request budget exhausted; coverage incomplete")
                batch = self._read_rows(dataset, {"ts_code": code, "trade_date": _ymd(trade_date)},
                                        MARKET_FIELDS[dataset])
                if any(r.get("ts_code") != code or _iso(r.get("trade_date")) != _iso(trade_date)
                       for r in batch):
                    raise XiaodefaError("scoped response identity/session mismatch")
                if len(batch) > 1:
                    raise XiaodefaError("duplicate instrument in scoped snapshot")
                rows.extend(batch)
            if self._is_production_source() and dataset == 'daily' and expected - {r['ts_code'] for r in rows}:
                self._read_rows('suspend_d', {'trade_date': _ymd(trade_date)},
                                'ts_code,trade_date,suspend_timing,suspend_type')
                expected = self._applicable_codes(codes, trade_date, dataset)
        value = "adj_factor" if dataset == "adj_factor" else "close"
        if dataset != "daily_basic" and any(
            _num(r.get(value)) is None or not math.isfinite(_num(r.get(value))) or _num(r.get(value)) <= 0
            for r in rows
        ):
            raise XiaodefaError("invalid price or adjustment in response")
        if dataset == "daily_basic" and any(not any(
            _num(r.get(f)) is not None and math.isfinite(_num(r.get(f)))
            for f in ("turnover_rate", "volume_ratio", "pe", "pb", "total_mv", "circ_mv")
        ) for r in rows):
            raise XiaodefaError("daily_basic contains no qualified fields")
        out, columns = market_batch(dataset, rows, self._provider_name(self.client))
        out = [(*row, self._input_received.get((dataset, row[0], _ymd(trade_date)), datetime.now()))
               for row in out]
        columns.append('fetched_at')
        self.store.conn.execute("BEGIN TRANSACTION")
        try:
            count = bulk_replace(self.store.conn, "tushare_" + dataset, out, columns, ["ts_code", "date"])
            self.store.conn.execute("COMMIT")
        except Exception:
            self.store.conn.execute("ROLLBACK")
            raise
        # Coverage failure below does not undo this committed partial batch.
        self._count_product(dataset, written=count)
        if dataset == 'daily_basic':
            unresolved_valuation = (self._expected_stock_codes(trade_date, dataset) if expected is None else expected)
            unresolved_valuation = unresolved_valuation - self._covered_codes(dataset, trade_date)
            if unresolved_valuation:
                report = self.valuation_completion_report(trade_date, unresolved_valuation)
                encoded = _json(report)
                self.store.conn.execute(
                    "INSERT INTO multi_source_observation(data_type,asset_type,provider,status,payload_json,payload_hash) "
                    "VALUES ('valuation_completion','diagnostic',?,'incomplete',?,?)",
                    [self._provider_name(self.client), encoded, hashlib.sha256(encoded.encode()).hexdigest()])
        if expected is not None:
            # Forced refresh still needs qualified fields, not identity alone.
            unresolved = expected - self._covered_codes(dataset, trade_date)
            if force:
                unresolved |= expected - {r['ts_code'] for r in rows}
            if unresolved:
                raise XiaodefaError(f"coverage incomplete: {len(unresolved)} missing instruments; "
                                   "provider absence does not prove suspension")
        else:
            missing = self._expected_stock_codes(trade_date, dataset) - self._covered_codes(dataset, trade_date)
            if missing:
                raise XiaodefaError(f"coverage incomplete: {len(missing)} instruments lack qualified fields")
            if dataset == 'daily_basic':
                self._record_snapshot('daily_basic_gaps', {'trade_date': _iso(trade_date),
                    'missing_codes': [], 'supplemental_fields': {},
                    'reviewed_valuation': self.qualified_valuation_reviews(trade_date),
                    'acceptance': 'valuation_coverage_complete', 'raw_provider_rows_preserved': True})
            self._certify_close_snapshot(dataset, trade_date, status="certified",
                                         provider=self._provider_name(self.client))
        return count

    def _collect_daily(self, trade_date):
        return self._collect_market("daily", trade_date)

    def _collect_daily_basic(self, trade_date):
        return self._collect_market("daily_basic", trade_date)

    def _collect_adj_factor(self, trade_date):
        return self._collect_market("adj_factor", trade_date)

    @staticmethod
    def _validate_amounts(rows, fields):
        for row in rows:
            for field in fields:
                value = row.get(field)
                if value in (None, '', '-'):
                    continue
                number = _num(value)
                if isinstance(value, bool) or number is None or not math.isfinite(number):
                    raise XiaodefaError(f'invalid finite amount: {field}')

    def _publish_flow(self, dataset, trade_date, out, columns, keys, attempts):
        table, day, sync = (
            ('tushare_moneyflow', 'date', self.sync_stock_flow) if dataset == 'moneyflow'
            else ('tushare_moneyflow_industry', 'trade_date', self.sync_sector_flow))
        with self._transaction():
            api = 'moneyflow' if dataset == 'moneyflow' else 'moneyflow_ind_dc'
            code_pos = columns.index('ts_code')
            out = [(*row, self._input_received.get((api, row[code_pos], _ymd(trade_date)), datetime.now()))
                   for row in out]
            columns = [*columns, 'fetched_at']
            raw_count = bulk_replace(self.store.conn, table, out, columns, keys)
            self.store.conn.execute(f'DELETE FROM {table} WHERE {day}=? AND ts_code NOT IN (SELECT unnest(?))',
                                    [_iso(trade_date), [r[code_pos] for r in out]])
            if dataset == 'moneyflow' and self._expected_stock_codes(trade_date, dataset) - self._covered_codes(dataset, trade_date):
                raise XiaodefaError('moneyflow coverage incomplete; missing values are not qualified facts')
            count = sync(trade_date, atomic=False)
            if count <= 0:
                raise XiaodefaError('no qualified flow rows; request is not complete')
            self._checkpoint(dataset, trade_date, 'success', rows=count, attempts=attempts)
        self._count_product('moneyflow' if dataset == 'moneyflow' else 'moneyflow_ind_dc', written=raw_count)
        self._count_product('stock_flow_projection' if dataset == 'moneyflow' else 'sector_flow_projection',
                            written=count)
        return count

    def _collect_moneyflow(self, trade_date: str, *, attempts=0) -> int:
        # Request the retained provider's full projection once. Missing values stay NULL.
        fields = ",".join(dict.fromkeys((MONEYFLOW_MAIN_FIELDS+","+MONEYFLOW_SIZE_FIELDS).split(",")))
        rows = self._query_date_batch("moneyflow", trade_date, fields)
        self._validate_amounts(rows, fields.split(',')[2:])
        for row in rows:
            normalized = normalize_stock_flow_row({**row, 'source_api': 'moneyflow',
                                                   'amount_unit': '10000_yuan'}, 'tushare')
            if not any(normalized.get(k) is not None and math.isfinite(normalized[k])
                       for k in ('main_net', 'net_total')):
                raise XiaodefaError('moneyflow contains no qualified net amount')
        out = [(r["ts_code"], ts_code_to_stock_code(r["ts_code"]), _iso(r["trade_date"]),
                *[_num(r.get(k)) for k in ("buy_sm_amount","sell_sm_amount","buy_md_amount",
                  "sell_md_amount","buy_lg_amount","sell_lg_amount","buy_elg_amount",
                  "sell_elg_amount","net_mf_amount")]) for r in rows]
        return self._publish_flow('moneyflow', trade_date, out,
                ["ts_code","stock_code","date","buy_sm_amount","sell_sm_amount","buy_md_amount",
                 "sell_md_amount","buy_lg_amount","sell_lg_amount","buy_elg_amount","sell_elg_amount","net_mf_amount"],
                ["ts_code","date"], attempts)

    def _collect_industry_flow(self, trade_date: str, *, attempts=0) -> int:
        api = "moneyflow_ind_dc"
        fields = INDUSTRY_FIELDS
        rows = self._read_rows(api, {'trade_date': _ymd(trade_date)}, fields)
        if not rows:
            raise XiaodefaError("empty industry flow response")
        if (any(not r.get('ts_code') or _iso(r.get("trade_date")) != _iso(trade_date) for r in rows)
                or len({r['ts_code'] for r in rows}) != len(rows)):
            raise XiaodefaError("industry flow identity/session mismatch")
        self._validate_amounts(rows, fields.split(',')[4:])
        if any(_num(r.get('close')) is None or _num(r['close']) <= 0
               or _num(r.get('net_amount')) is None for r in rows):
            raise XiaodefaError('industry flow contains no qualified close/net amount')
        rows = [{**r, "source_api":api} for r in rows]
        out = [
            (_iso(row.get("trade_date") or trade_date), row.get("ts_code"), row.get("name"), _num(row.get("pct_change")),
             _num(row.get("close")), _num(row.get("net_amount")), _num(row.get("buy_elg_amount")),
             _num(row.get("sell_elg_amount")), _num(row.get("buy_lg_amount")), _num(row.get("sell_lg_amount")),
             _num(row.get("buy_md_amount")), _num(row.get("sell_md_amount")), _num(row.get("buy_sm_amount")),
             _num(row.get("sell_sm_amount")), _json(row))
            for row in rows if row.get("ts_code")
        ]
        return self._publish_flow('industry_flow', trade_date, out,
                ["trade_date", "ts_code", "sector_name", "change_pct", "close", "net_amount", "buy_elg_amount", "sell_elg_amount",
                 "buy_lg_amount", "sell_lg_amount", "buy_md_amount", "sell_md_amount", "buy_sm_amount", "sell_sm_amount", "raw_json"],
                ["trade_date", "ts_code"], attempts)

    def sync_stock_flow(self, trade_date: str, *, atomic=True) -> int:
        rows = self.store.conn.execute(
            "SELECT ts_code,stock_code,buy_sm_amount,sell_sm_amount,buy_md_amount,sell_md_amount,"
            "buy_lg_amount,sell_lg_amount,buy_elg_amount,sell_elg_amount,net_mf_amount,fetched_at "
            "FROM tushare_moneyflow WHERE date=? ORDER BY fetched_at DESC",
            [_iso(trade_date)],
        ).fetchall()
        self._count_product('stock_flow_projection', parsed=len(rows))
        out = []
        seen = set()
        for row in rows:
            if row[1] in seen:
                continue
            seen.add(row[1])
            raw = dict(zip(("buy_sm_amount", "sell_sm_amount", "buy_md_amount", "sell_md_amount",
                            "buy_lg_amount", "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
                            "net_mf_amount"), row[2:11]))
            normalized = normalize_stock_flow_row({**raw, "source_api": "moneyflow",
                                                   "amount_unit": "10000_yuan"}, "tushare")
            out.append([_iso(trade_date), row[1], *[normalized[k] for k in (
                        "main_net", "net_total", "super_net", "large_net", "mid_net", "small_net")],
                        "tushare", normalized["amount_unit"], normalized["flow_definition"], "moneyflow",
                        "tushare", normalized["field_mapping_version"], False,
                        _json({**raw, "ts_code": row[0], "source": "tushare_moneyflow", "unit": "10000_yuan"}), row[11]])
        # An empty source batch is not a valid replacement.  In particular,
        # an upstream timeout can leave the raw table empty while the last
        # verified normalized snapshot is still usable.  Return before the
        # DELETE so the old provider rows survive the transient failure.
        if not out:
            return 0
        with self._transaction(atomic):
            count = bulk_replace(self.store.conn,
                "multi_source_stock_flow", out,
                ["source_date", "stock_code", "main_net", "net_total", "super_net", "large_net", "mid_net", "small_net", "provider", "amount_unit", "flow_definition", "source_api", "origin_provider", "field_mapping_version", "is_stale", "raw_json", "fetched_at"],
                ["source_date", "stock_code", "provider"],
            )
            self.store.conn.execute("DELETE FROM multi_source_stock_flow WHERE source_date=? AND provider='tushare' "
                "AND stock_code NOT IN (SELECT unnest(?))", [_iso(trade_date), [r[1] for r in out]])
        if atomic:
            self._count_product('stock_flow_projection', written=count)
        return count

    def sync_sector_flow(self, trade_date: str, *, atomic=True) -> int:
        rows = self.store.conn.execute(
            "SELECT ts_code,sector_name,change_pct,close,net_amount,buy_elg_amount,sell_elg_amount,"
            "buy_lg_amount,sell_lg_amount,buy_md_amount,sell_md_amount,buy_sm_amount,sell_sm_amount,raw_json,fetched_at "
            "FROM tushare_moneyflow_industry WHERE trade_date=? AND close IS NOT NULL AND close>0",
            [_iso(trade_date)],
        ).fetchall()
        self._count_product('sector_flow_projection', parsed=len(rows))
        out = []
        for row in rows:
            raw = json.loads(row[13] or '{}')
            # Unlike stock-level ``moneyflow`` (万元), TuShare's DC industry
            # endpoint documents these fields as yuan.  Do not multiply them
            # again or sector totals become four orders of magnitude too high.
            super_net, large, mid, small = [_num(row[i]) for i in (5, 7, 9, 11)]
            out.append([_iso(trade_date), row[0], row[1], _num(row[4]) if row[4] is not None else None,
                        super_net, large, mid, small, row[2], "tushare_sector_full",
                        {"行业": "em_industry", "概念": "em_concept", "地域": "em_region"}.get(raw.get('content_type'), 'tushare_dc_sector'), "yuan", False,
                        _json({**raw, "close": row[3], "source": "moneyflow_ind_dc", "unit": "yuan"}), row[14]])
        if not out:
            return 0
        with self._transaction(atomic):
            count = bulk_replace(self.store.conn,
                "multi_source_sector_flow", out,
                ["source_date", "sector_code", "sector_name", "main_net", "super_net", "large_net", "mid_net", "small_net", "change_pct", "provider", "sector_type", "amount_unit", "is_stale", "raw_json", "fetched_at"],
                ["source_date", "sector_code", "provider"],
            )
            self.store.conn.execute("DELETE FROM multi_source_sector_flow WHERE source_date=? AND "
                "(provider='tushare' OR (provider='tushare_sector_full' AND sector_code NOT IN (SELECT unnest(?))))",
                [_iso(trade_date), [r[1] for r in out]])
        if atomic:
            self._count_product('sector_flow_projection', written=count)
        return count

    def run(self, start_date: str, end_date: str, *, datasets: Iterable[str],
            max_days: int | None = None, force: bool = False, gap_only: bool = False,
            retry_passes: int = 0, retry_delay_seconds: float = 0.0,
            stock_codes: Iterable[str] | None = None, index_codes: Iterable[str] | None = None,
            plan_only: bool = False) -> dict[str, Any]:
        if self.offline and not plan_only:
            raise ValueError('offline collector only supports plan_only')
        datasets = list(dict.fromkeys(datasets))
        supported = set(MARKET_FIELDS) | {"stock_basic", "moneyflow", "industry_flow"}
        if not datasets or set(datasets) - supported:
            raise ValueError("unsupported or empty TuShare datasets")
        stocks = None if stock_codes is None else sorted({stock_code_to_ts_code(c) for c in stock_codes})
        indexes = None if index_codes is None else sorted({index_code_to_ts_code(c) for c in index_codes})
        if stocks is not None and (not stocks or set(datasets) - set(MARKET_FIELDS)):
            raise ValueError("stock scope requires nonempty codes and only daily/basic/adjustment/index datasets")
        if "index_daily" in datasets and not indexes:
            raise ValueError("index_daily requires explicit index_codes")
        scopes = {d: indexes if d == "index_daily" else stocks for d in datasets if d in MARKET_FIELDS}

        def checkpoint_key(dataset):
            codes = scopes.get(dataset)
            if codes is None:
                return dataset
            identity = hashlib.sha256(_json(codes).encode()).hexdigest()
            return f"{dataset}:scope:{identity}"

        def is_done(dataset, trade_date):
            if (reference_needed or self.offline) and reference_version is None:
                return False
            codes = scopes.get(dataset)
            if codes is None:
                return self._is_done(dataset, trade_date, force)
            expected = set(codes) if dataset == "index_daily" else self._applicable_codes(codes, trade_date, dataset)
            return not force and expected <= self._covered_codes(dataset, trade_date)

        dates = self.ensure_calendar(start_date, end_date, allow_fetch=not plan_only)
        results_by_key: dict[tuple[str, str], dict[str, Any]] = {}
        reference_error = None
        reference_needed = ('stock_basic' in datasets or self._is_production_source()
                            and bool(set(datasets) & (STOCK_SNAPSHOT_DATASETS | {'moneyflow'})))
        if reference_needed:
            reference = {'dataset': 'stock_basic', 'trade_date': CHECKPOINT_DATE}
            try:
                if plan_only:
                    reference['status'] = 'covered' if self._reference_version() else 'planned'
                elif not self._budget_left():
                    raise XiaodefaError('reference refresh budget exhausted')
                else:
                    reference['status'] = 'success' if self.collect_stock_basic(force=force) else 'skipped'
            except Exception as exc:
                reference_error = str(exc)[:240]
                reference.update(status='error', error=reference_error)
            if 'stock_basic' in datasets or reference_error:
                results_by_key[('stock_basic', CHECKPOINT_DATE)] = reference
        reference_version = self._reference_version()
        if gap_only:
            dates = [
                trade_date for trade_date in dates
                if any(
                    dataset != "stock_basic" and (reference_error or not is_done(dataset, trade_date))
                    for dataset in datasets
                )
            ]
        if max_days is not None:
            limit = max(0, int(max_days))
            if limit:
                dates = dates[-limit:]
        # Close runs commonly scan a lookback window under a fixed budget.  The
        # newest session is the operational dependency, so process newest first
        # and let the checkpointed history pass repair older gaps afterwards.
        dates = list(reversed(dates))
        handlers = {
            "daily": self._collect_daily,
            "daily_basic": self._collect_daily_basic,
            "adj_factor": self._collect_adj_factor,
            "moneyflow": self._collect_moneyflow,
            "industry_flow": self._collect_industry_flow,
        }
        retry_keys: set[tuple[str, str]] | None = None
        for pass_no in range(max(0, int(retry_passes)) + 1):
            failed_keys: set[tuple[str, str]] = set()
            for trade_date in dates:
                for dataset in datasets:
                    if dataset == "stock_basic" or dataset not in (set(handlers) | set(MARKET_FIELDS)):
                        continue
                    key = (dataset, _iso(trade_date))
                    if retry_keys is not None and key not in retry_keys:
                        continue
                    if reference_error and dataset in STOCK_SNAPSHOT_DATASETS | {'moneyflow'}:
                        # Acquire once for repair/replay, but never certify an unknown
                        # universe or shrink its denominator to the rows that arrived.
                        fields = (MARKET_FIELDS[dataset] if dataset in MARKET_FIELDS else
                                  ','.join(dict.fromkeys((MONEYFLOW_MAIN_FIELDS+','+MONEYFLOW_SIZE_FIELDS).split(','))))
                        error = 'reference prerequisite: ' + reference_error
                        acquired = 0
                        try:
                            if not self._budget_left():
                                raise XiaodefaError('request budget exhausted')
                            for code in scopes.get(dataset) or [None]:
                                if not self._budget_left():
                                    raise XiaodefaError('request budget exhausted')
                                params = {'trade_date': _ymd(trade_date)}
                                if code is not None:
                                    params['ts_code'] = code
                                acquired += len(self._read_rows(dataset, params, fields))
                        except Exception as exc:
                            error += '; acquisition: ' + str(exc)[:180]
                        self._checkpoint(checkpoint_key(dataset), trade_date, 'error',
                                         attempts=self._next_attempt(checkpoint_key(dataset), trade_date), error=error)
                        results_by_key[key] = {'dataset': dataset, 'trade_date': trade_date,
                            'status': 'error', 'error': error, 'received_unverified_rows': acquired,
                            'publication': 'raw_receipts_only_reference_unqualified'}
                        continue
                    if not self._budget_left():
                        results_by_key[key] = {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "budget_exhausted",
                        }
                        continue
                    if plan_only:
                        codes = scopes.get(dataset)
                        expected = None if codes is None else (set(codes) if dataset == "index_daily"
                            else self._applicable_codes(codes, trade_date, dataset))
                        results_by_key[key] = {"dataset": dataset, "trade_date": trade_date,
                            "status": "covered" if reference_version and is_done(dataset, trade_date) else "planned",
                            "missing_codes": sorted(expected - self._covered_codes(dataset, trade_date))
                                             if expected is not None else None,
                            "not_listed_codes": sorted(set(codes) - self._applicable_codes(codes, trade_date))
                                                if expected is not None and dataset != 'index_daily' else [],
                            "suspended_codes": sorted(self._applicable_codes(codes, trade_date) - expected)
                                               if expected is not None and dataset in {'daily', 'moneyflow'} else []}
                        continue
                    if is_done(dataset, trade_date):
                        results_by_key.setdefault(key, {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "skipped",
                        })
                        continue
                    checkpoint = checkpoint_key(dataset)
                    attempt = self._next_attempt(checkpoint, trade_date)
                    self._checkpoint(checkpoint, trade_date, "running", attempts=attempt)
                    try:
                        rows = (self._collect_market(dataset, trade_date, codes=scopes.get(dataset), force=force)
                                if dataset in MARKET_FIELDS else handlers[dataset](trade_date, attempts=attempt))
                        if dataset not in {'moneyflow', 'industry_flow'}:
                            self._checkpoint(checkpoint, trade_date, "success", rows=rows, attempts=attempt)
                        results_by_key[key] = {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "success",
                            "rows": rows,
                        }
                    except Exception as exc:
                        # The handlers publish inside a transaction.  Preserve the
                        # last verified date on a network/validation failure and
                        # make the checkpoint carry the error instead of deleting
                        # good data from the production tables.
                        self._checkpoint(checkpoint, trade_date, "error", attempts=attempt, error=str(exc))
                        if dataset in STOCK_SNAPSHOT_DATASETS and scopes.get(dataset) is None:
                            self._certify_close_snapshot(
                                dataset,
                                trade_date,
                                status="error",
                                error_message=str(exc),
                                provider=self._provider_name(self.client),
                            )
                        results_by_key[key] = {
                            "dataset": dataset,
                            "trade_date": trade_date,
                            "status": "error",
                            "error": str(exc)[:240],
                        }
                        failed_keys.add(key)
            if not failed_keys or pass_no >= max(0, int(retry_passes)):
                break
            retry_keys = failed_keys
            delay = max(0.0, float(retry_delay_seconds)) * (pass_no + 1)
            remaining = self.budget_seconds - (time.monotonic() - self.started)
            if remaining <= 1.0:
                break
            if delay:
                time.sleep(min(delay, max(0.0, remaining - 1.0)))
        return {"start_date": _iso(start_date), "end_date": _iso(end_date), "dates": dates,
                "reference": reference_version,
                "reference_status": 'qualified' if reference_version else 'unverified',
                "datasets": datasets, "results": list(results_by_key.values()),
                "elapsed_seconds": round(time.monotonic() - self.started, 3)}


def render_report(db_path: str | Path, result: dict[str, Any], out_path: str | Path) -> Path:
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        counts = con.execute(
            "SELECT dataset,status,count(*),sum(rows_written),max(updated_at) FROM history_fetch_checkpoint "
            "GROUP BY dataset,status ORDER BY dataset,status"
        ).fetchall()
        tables = []
        table_date_expr = {
            "tushare_stock_basic": "max(list_date)",
            "tushare_daily": "max(date)",
            "tushare_index_daily": "max(date)",
            "tushare_daily_basic": "max(date)",
            "tushare_adj_factor": "max(date)",
            "tushare_moneyflow": "max(date)",
            "tushare_moneyflow_industry": "max(trade_date)",
            "multi_source_stock_flow": "max(source_date)",
            "multi_source_sector_flow": "max(source_date)",
        }
        for table, date_expr in table_date_expr.items():
            try:
                tables.append((table, *con.execute(f"SELECT count(*),{date_expr} FROM {table}").fetchone()))
            except Exception:
                pass
    finally:
        con.close()
    lines = ["# Tushare 2026 历史回填", "", f"- 日期范围: `{result['start_date']}` ~ `{result['end_date']}`",
             f"- 本次交易日数: {len(result['dates'])}", f"- elapsed_seconds: {result['elapsed_seconds']}", "",
             "## 本次结果", "", "| dataset | date | status | received unverified | reason |", "|---|---|---|---:|---|"]
    for item in result['results']:
        reason = str(item.get('error', '')).replace('|', '/').replace('\n', ' ')
        lines.append(f"| {item['dataset']} | {item['trade_date']} | {item['status']} | {item.get('received_unverified_rows', 0)} | {reason} |")
    lines.extend(["", "## 历史检查点汇总", "", "| dataset | status | dates/tasks | rows | last_updated |", "|---|---|---:|---:|---:|"])
    lines.extend(f"| {row[0]} | {row[1]} | {row[2]} | {row[3] or 0} | {row[4] or '-'} |" for row in counts)
    lines.extend(["", "## 表覆盖", "", "| table | rows | latest_date |", "|---|---:|---|"])
    lines.extend(f"| {table} | {rows} | {latest or '-'} |" for table, rows, latest in tables)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
