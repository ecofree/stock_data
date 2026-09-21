"""Bounded auction acquisition with distinct market, tick, anomaly, final and opening products.

The close task uses the market sequence and final-match payload. Explicit
maintenance may request scoped ticks or global anomalies; neither product is
inferred from a final-match snapshot. Official final snapshots and relay opening
bars retain receipt times and quality gaps, never tick or pre-open qualification.
All share the existing store and budget.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import math
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from trade_system.data_store import DuckDBStore, KPLClient
from collectors.collect_misc import collect_auction_market, collect_auction_tick, collect_auction_bidding_anomaly
from trade_system.config import API_KEY, DB_PATH, TODAY
from trade_system.schema import init_schema
from trade_system.api_health import require_api_key
from trade_system.auction_evidence import observed_auction_rows, EVIDENCE_COLUMNS, ensure_auction_evidence_tables


def collect(db_path: str | Path, trade_date: str, *, out: str | Path = "",
            product: str = 'market', codes=(), budget_seconds: float = 60) -> dict:
    # Distinct endpoints remain distinct products; no legacy signal-based fan-out.
    if product not in {'market', 'tick', 'anomaly', 'final', 'opening'} or (product in {'tick','final'} and not codes):
        raise ValueError('supported auction product and explicit codes for tick required')
    if not math.isfinite(budget_seconds) or not 0 < budget_seconds <= 60:
        raise ValueError('auction budget must be finite within 60 seconds')
    if product == 'final' and (len(codes)>100 or any(len(c)!=9 or not c[:6].isascii() or not c[:6].isdigit()
            or c[-3:] not in ('.SH','.SZ','.BJ') for c in codes)):
        raise ValueError('final snapshot requires at most 100 explicit exchange-qualified codes')
    target = Path(out) if out else None
    result: dict = {
        "trade_date": trade_date,
        "status": "error",
        "source": {'market': '/auction/market', 'tick': '/auction/tick',
                   'anomaly': '/auction/bidding-anomaly', 'final':'/api/a-share/auction/snapshot',
                   'opening':'stk_auction_o'}[product],
        "product": product,
        "stock_rows": 0,
        "tick_rows": 0,
        "quote_rows": 0,
    }
    try:
        if product not in {'final','opening'}:
            require_api_key(API_KEY)
        store = DuckDBStore(str(db_path))
        try:
            init_schema(store.conn)
            if product in {'final','opening'}:
                provider = 'hithink' if product=='final' else 'xiaodefa'
                asset = 'hithink_auction_final' if product=='final' else 'stk_auction_o'
                cached = store.conn.execute("SELECT payload_json,payload_hash,observed_at FROM multi_source_observation "
                    "WHERE source_date=? AND asset_code=? AND provider=? "
                    "AND data_type IN ('provider_gap_probe','auction_native_snapshot') "
                    "ORDER BY observed_at DESC LIMIT 1",[trade_date,asset,provider]).fetchone()
                evidence = None
                if cached and hashlib.sha256(cached[0].encode()).hexdigest()==cached[1]:
                    payload=json.loads(cached[0])
                    evidence=observed_auction_rows(payload,trade_date,provider,cached[2],cached[1])
                    if product=='final' and not set(codes)<= {r['thscode'] for r in payload['item']}:
                        evidence=None
                reused = evidence is not None
                if evidence is None:
                    if product=='final':
                        from trade_system.hithink_client import HiThinkClient
                        if trade_date != datetime.now().date().isoformat():
                            raise ValueError('native final snapshot has no historical date parameter')
                        payload=HiThinkClient(timeout=budget_seconds)._get(result['source'],
                            {'thscodes':','.join(dict.fromkeys(codes)),'stage':'final'})
                    else:
                        from trade_system.xiaodefa_source import XiaodefaClient
                        if trade_date==datetime.now().date().isoformat() and datetime.now().hour<20:
                            raise ValueError('historical opening API is published after 20:00')
                        params={'trade_date':trade_date.replace('-',''),'limit':10000}
                        rows=XiaodefaClient(timeout=budget_seconds,max_retries=1).query_rows('stk_auction_o',params)
                        payload=dict(api='stk_auction_o',params=params,rows=rows)
                    received=datetime.now()
                    encoded=json.dumps(payload,ensure_ascii=False,sort_keys=True,allow_nan=False)
                    fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
                    store.conn.execute("INSERT INTO multi_source_observation "
                        "(source_date,data_type,asset_type,asset_code,provider,status,payload_json,payload_hash,observed_at) "
                        "VALUES (?,'auction_native_snapshot','receipt',?,?,'received_unverified',?,?,?)",
                        [trade_date,asset,provider,encoded,fingerprint,received])
                    evidence=observed_auction_rows(payload,trade_date,provider,received,fingerprint)
                if product=='final':
                    wanted={c[:6] for c in codes}
                    evidence=[r for r in evidence if r['stock_code'] in wanted]
                    if len(evidence)!=len(wanted):
                        raise ValueError('native final snapshot missing requested codes')
                # Use the existing evidence table and bulk writer. No tick,
                # quote-window or phase-success checkpoint is manufactured.
                columns=EVIDENCE_COLUMNS+['generated_at']
                ensure_auction_evidence_tables(db_path,connection=store.conn)
                count=store.insert_rows('auction_evidence_snapshot',
                    [[r.get(c) for c in EVIDENCE_COLUMNS]+[datetime.now()] for r in evidence],
                    columns,replace_on=['trade_date','stock_code','source_table'])
                result.update(status='success' if count else 'empty',stock_rows=count,
                    evidence_rows=count,receipt_reused=reused,scope='observed_native_product_not_market_phase_acceptance',
                    invalid_rows=sum(';invalid_native_ohlc' in r['missing_reason'] or ';no_positive_' in r['missing_reason'] for r in evidence),
                    qualified_tick=False,qualified_order_book=False,predeclared_observation=False)
            else:
                client = KPLClient(request_timeout=30, max_attempts=2, total_budget_seconds=budget_seconds)
                if product == 'market':
                    parsed = collect_auction_market(client, store, trade_date)
                else:
                    collector = collect_auction_tick if product == 'tick' else collect_auction_bidding_anomaly
                    rows = collector(client, store, trade_date, list(dict.fromkeys(codes)) or [''])
                    failures = sum(int(client.stats.get(k) or 0) for k in ('error', 'rate_limited', 'circuit_open'))
                    parsed = {product + '_rows': rows,
                              'status': 'partial' if failures and rows else 'success' if rows else 'error'}
                result.update(parsed)
        finally:
            store.close()
    except Exception as exc:
        result.update({"status": "error", "error": f"{type(exc).__name__}: {exc}"})

    if target:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(" ".join(f"{key}={value}" for key, value in result.items()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--date", default=TODAY)
    parser.add_argument('--product', choices=('market', 'tick', 'anomaly','final','opening'), default='market',
                        help='Distinct market sequence/final snapshot, scoped ticks or global anomalies; never interchangeable.')
    parser.add_argument('--codes', default='', help='Explicit codes; final requires exchange-qualified codes such as 000001.SZ (max 100).')
    parser.add_argument('--budget-seconds', type=float, default=60)
    parser.add_argument("--out", default="reports/auction_market_collection_latest.json")
    args = parser.parse_args()
    result = collect(args.db, args.date, out=args.out, product=args.product,
                     codes=[c.strip() for c in args.codes.split(',') if c.strip()], budget_seconds=args.budget_seconds)
    return 0 if result.get("status") == "success" else 2


if __name__ == "__main__":
    raise SystemExit(main())
