"""Reproducible synthetic close-to-next-close slice, never a live recommendation."""
import argparse
import csv
from io import StringIO
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from trade_system.v2.storage import Store
from trade_system.v2.domain import utc
from trade_system.v2.accounts import import_snapshot, append_account_event
from trade_system.v2.decisions import DecisionService, RiskPolicy
from trade_system.v2.strategies import StrategyPolicy, record_signal
from trade_system.v2.reporting import project_review, render_review
from trade_system.v2.publisher import publish


class DemoClock:
    value = utc('2026-09-09T15:00:00+08:00')
    def __call__(self):
        return self.value


def run_demo(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    clock = DemoClock()
    trace = []
    with Store(output / 'paper.duckdb', clock=clock) as store:
        store.register_product('fixture.quote', 'CNY', 'point', 'paper_confirmation', 'synthetic_fixture')
        policy = StrategyPolicy('demo-only-v1', '9', '11', '8', '2026-09-10T09:40:00+08:00')
        def stage(code, *, price, auction, theme=True, funds=True):
            at = clock().isoformat()
            if price is not None:
                store.ingest('fixture.quote', code, at, price, at, json.dumps({'price':price,'fixture':True}).encode())
            manifest = store.freeze(clock())
            sig = record_signal(store, code, manifest, policy, at=at, price=price,
                                auction_received_at=auction, auction_confirmed=auction is not None,
                                theme_supported=theme, funds_supported=funds)
            trace.append({'asof':at,'instrument':code,'signal_id':sig,'manifest_id':manifest})
            return sig, manifest
        stage('SZ.000002', price='10', auction=None)
        clock.value = utc('2026-09-10T09:24:00+08:00')
        stage('SZ.000002', price='10', auction='2026-09-10T09:26:00+08:00')
        clock.value = utc('2026-09-10T09:31:00+08:00')
        sig, manifest = stage('SZ.000002', price='10', auction='2026-09-10T09:26:00+08:00')
        account = {'account_id':'demo-paper', 'mode':'paper', 'asof':'2026-09-10T09:30:00+08:00',
                   'valid_until':'2026-09-10T10:00:00+08:00', 'cash':'4000.00', 'frozen_cash':'0.00',
                   'equity':'10000.00', 'positions':[{'instrument':'SH.600000','quantity':600,'sellable':600,'mark_price':'10.00'}],
                   'open_orders':[], 'declared_complete':True, 'source':'manual_declaration'}
        import_snapshot(store, json.dumps(account).encode())
        decisions = DecisionService(store, RiskPolicy('demo-only-risk','.7','.2',100,60,1000,0))
        draft = decisions.propose('demo-paper', sig, manifest, 'fixture.quote')
        confirmed = decisions.confirm(draft['decision_id'], quantity_requested=100, operator='fixture-operator',
                                       request_id='demo-confirm', quote_manifest=manifest)
        cancelled, context = stage('SZ.000003', price='7.5', auction='2026-09-10T09:26:00+08:00', theme=False)
        decisions.propose('demo-paper', cancelled, context, 'fixture.quote')
        blocked, context = stage('SZ.000004', price=None, auction='2026-09-10T09:26:00+08:00')
        decisions.propose('demo-paper', blocked, context, 'fixture.quote')
        clock.value = utc('2026-09-10T15:00:00+08:00')
        stage('SZ.000002', price='10', auction='2026-09-10T09:26:00+08:00')
        append_account_event(store, 'demo-review', 'demo-paper', 'external_action_unknown',
                             {'note':'纸面确认不代表实际成交；示例未提供券商成交事实。'})
        decisions.mark_unknown(confirmed['reservation_id'])
        data = project_review(store, 'demo-paper')
        data['fixture_only'] = True
        data['trace'] = trace
    csv_file = StringIO(newline='')
    writer = csv.writer(csv_file)
    writer.writerow(['instrument','state','strategy_version','execution_ready'])
    for sig in data['signals']:
        writer.writerow([sig['instrument'],sig['state'],sig['strategy_version'],False])
    publish(output / 'reports', 'demo', {'review.html':render_review(data).encode('utf-8'),
            '2026-09-10-plans.csv':csv_file.getvalue().encode('utf-8'),
            'evidence.json':json.dumps(data,ensure_ascii=False,indent=2).encode('utf-8')}, generation=1)
    return {'scope':'synthetic_fixture_only','review':str(output/'reports/runs/demo/review.html'),
            'signal_events':len(trace), 'real_execution_ready':False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',required=True,type=Path)
    print(json.dumps(run_demo(parser.parse_args().output),ensure_ascii=False))
