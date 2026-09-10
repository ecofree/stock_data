"""Capture local implementation evidence, never a production acceptance verdict."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as handle:
        for part in iter(lambda: handle.read(1024 * 1024), b''):
            result.update(part)
    return result.hexdigest()


def capture(output, installed_python, wheel, test_reports=None):
    output.mkdir(parents=True, exist_ok=False)
    files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=ROOT).decode().split('\0')
    included = ('trade_system/', 'collectors/', 'scripts/', 'tests/', 'tools/v2/', 'migrations/', '.github/workflows/')
    selected = sorted(set(p for p in files if p and (p.startswith(included) or p in
        ('pyproject.toml', 'requirements.lock', 'requirements-dev.lock', 'requirements-build.in', 'requirements-build.lock', 'requirements-qlib.txt', 'requirements-qlib.lock'))))
    hashes = {p: digest(ROOT/p) for p in selected if (ROOT/p).is_file()}
    source_hash = hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest()
    tests = {}
    paths = test_reports or [ROOT/'tmp'/name for name in ('v2-final-source-tests.xml', 'v2-final-py311-tests.xml')]
    for path in paths:
        name = path.name
        suites = ET.parse(path).getroot().findall('testsuite')
        tests[name] = {k: sum(int(s.attrib.get(k, 0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        tests[name]['sha256'] = digest(path)
        if not tests[name]['tests'] or tests[name]['failures'] or tests[name]['errors']:
            raise RuntimeError('test evidence has failures or no tests')
    probe = '''
import json, sys, tempfile
from pathlib import Path
import duckdb
import trade_system
from trade_system.migrations import discover_migrations
from trade_system.schema import init_schema
from trade_system.v2.service import Service
assert 'site-packages' in str(Path(trade_system.__file__))
with duckdb.connect(':memory:') as con:
    init_schema(con)
    tables = len(con.execute('SHOW TABLES').fetchall())
with tempfile.TemporaryDirectory() as folder:
    with Service(Path(folder)/'paper.duckdb') as service:
        status = service.submit('status').result(10)
        assert status['execution_ready'] is False
        bundle = {'trade_date':'2020-01-02','asof':'2020-01-02T18:00:00+08:00', 'mode':'historical_research',
                  'expected_universe':['SH.600001'], 'products':{'fixture':{
                  'delivery_provider':'fixture','origin_family':'fixture','source_api':'fixture',
                  'metric_definition':'unadjusted_daily_bar','unit':'CNY','priority':0,'license_status':'unverified'}}}
        policy = {'version':'wheel-fixture-v1','min_coverage':1,'min_members':1,
                  'min_advancing_fraction':1,'max_theme_universe_fraction':1,'max_candidates':1}
        context = service.submit('context_import',bundle=bundle,policy=policy).result(10)
        assert context['execution_ready'] is False and context['candidates'] == 0
        from trade_system.v2.domain import utc
        at = utc(service.clock()).isoformat()
        day = utc(at).astimezone(__import__('zoneinfo').ZoneInfo('Asia/Shanghai')).date().isoformat()
        service.submit('product',dataset='fixture.quote',unit='CNY',semantics='point',consumer='event:quote',origin='fixture').result(10)
        service.submit('market_event',dataset='fixture.quote',code='SZ.000002',kind='quote',event_at=at,
                       payload={'price':'10','phase':'continuous'},source_event_id='fixture').result(10)
        cfg = {'account_id':'wheel-paper','mode':'paper','trading_days':[day],'opened_at':at,
               'initial_cash_fen':100000,'initial_lots':[{'instrument':'SZ.000002','quantity':100,'cost_fen':100000,
                  'mark_price_fen':1000,'sellable_from':day}],'quote_ttl_seconds':30,'mark_ttl_seconds':60,'account_ttl_seconds':60,
               'fees':{'version':'fixture','effective_from':day,'effective_to':day,'commission_bps':'0',
                       'minimum_commission_fen':0,'transfer_bps':'0','sell_tax_bps':'0'},
               'instruments':{'SZ.000002':{'version':'fixture','effective_from':day,'effective_to':day,
                              'buy_lot':100,'sell_lot':100,'t_plus_sessions':1,'allow_odd_sell_all':True}}}
        opened = service.submit('paper_open',config=cfg).result(10)
        assert opened['cash_fen']==100000 and opened['execution_ready'] is False
        updated = service.submit('paper_event',account_id='wheel-paper',event={'event_id':'deposit','kind':'cash_transfer',
                                 'payload':{'amount_fen':100,'evidence_id':'fixture'}}).result(10)
        from datetime import timedelta
        risk = {'version':'wheel-fixture','max_total_fraction':'.5','max_single_fraction':'.5','lot_size':100,
                'quote_ttl_seconds':30,'max_quantity':1000,'fee_buffer_fen':0}
        manifest = service.submit('freeze',asof=utc(service.clock()).isoformat()).result(10)
        draft = service.submit('propose_exit',risk_policy=risk,account_id='wheel-paper',code='SZ.000002',
            quote_manifest=manifest,quote_dataset='fixture.quote',exit_policy={'version':'wheel-fixture-exit','reason':'manual_reduce',
            'reference_id':'fixture','expires_at':(utc(at)+timedelta(minutes=5)).isoformat()}).result(10)
        confirmed = service.submit('confirm',risk_policy=risk,decision_id=draft['decision_id'],quantity_requested=100,
                                   operator='fixture',request_id='fixture-exit',quote_manifest=manifest).result(10)
        assert confirmed['hypothetical_ready'] is True and confirmed['execution_ready'] is False
        service.submit('paper_sell',account_id='wheel-paper',confirmation_request_id='fixture-exit',order_id='wheel-sell').result(10)
        updated = service.submit('paper_event',account_id='wheel-paper',event={'event_id':'sell-fill','kind':'market',
            'payload':{'instrument':'SZ.000002','source_event_at':utc(service.clock()).isoformat(),'evidence_kind':'observed_orderbook',
                'phase':'continuous','tradable':True,'rule_version':'fixture','lower_limit_fen':800,'upper_limit_fen':1200,
                'bid_fen':1000,'ask_fen':1000,'last_fen':1000,'bid_quantity':100,'ask_quantity':100}}).result(10)
        attribution = service.submit('attribution',account_id='wheel-paper').result(10)
        assert attribution['paper_portfolio_ledger']['orders'][0]['filled_quantity']==100
        assert attribution['paper_portfolio_ledger']['reconciliation_difference_fen']==0
        assert attribution['actual_operator_ledger']['trades'] is None
    with Service(Path(folder)/'paper.duckdb') as restarted:
        assert restarted.submit('paper_status',account_id='wheel-paper').result(10)==updated
    import pandas as pd
    from trade_system.v2.rolling_research import register_experiment,run_experiment,read_result,file_hash,FoldDataset
    days = list(pd.bdate_range('2020-01-02',periods=50).strftime('%Y-%m-%d'))
    path = Path(folder)/'features.csv'
    pd.DataFrame([{'datetime':d,'instrument':str(j),'f':i+j,'label_next_ret':j-1,
                   'label_end_time':days[i+2]+' 15:00:00','label_available_time':days[i+2]+' 16:00:00'}
                  for i,d in enumerate(days[:45]) for j in range(3)]).to_csv(path,index=False)
    metadata = {'feature_columns':['f'],'label_definition':'fixture','availability_assumption':'fixture Shanghai',
                'artifact_hashes':{path.name:file_hash(path)}}
    path.with_suffix('.metadata.json').write_text(json.dumps(metadata),encoding='utf-8')
    plan = {'experiment_id':'wheel-fixture','scope':'historical_research_only','exposure_status':'previously_inspected_not_untouched',
            'evaluation_asof':days[-1]+'T18:00:00+08:00','label_definition':'fixture','availability_assumption':'fixture Shanghai',
            'max_rows':135,'train_observations':10,'valid_observations':5,'test_observations':5,'excluded_tail_observations':5,
            'max_folds':5,'num_boost_round':2,'num_threads':1,'seed':1,'top_k':1,'round_trip_cost_bps':[0,10],
            'comparison_baseline':'price','variants':{'price':['f']}}
    def fake_fit(frames,features,plan,out):
        dataset = FoldDataset(frames,features,fitting=False)
        return pd.Series(float(frames['train'].label_next_ret.mean()),index=dataset.prepare('test').index),{'backend':'fixture'}
    experiment = register_experiment(Path(folder)/'experiments',path,plan)
    result = run_experiment(experiment,fit=fake_fit)
    assert read_result(experiment)==result and len(result['folds'])==5
    assert result['aggregate']['price']['portfolio_return'] is None and result['signal_impact']=='disabled'
    from trade_system.v2.portfolio_diagnostic import evaluate_portfolio,SCOPE
    from trade_system.v2.portfolio_experiment import source_hashes
    assert len(source_hashes())==4
    portfolio_policy = {'scope':SCOPE,'exposure_status':'previously_inspected_not_untouched',
        'fill_assumption':'unconstrained_next_open_and_due_close',
        'calendar_assumption':'SSE_sessions_shared_not_exchange_certified',
        'missing_mark_policy':'stop_account','corporate_action_policy':'stop_on_held_factor_change',
        'initial_cash_fen':1000000,'top_k':1,'max_positions':2,'ticket_bps':5000,
        'buy_lot':100,'hold_sessions':2,'t_plus_sessions':1,
        'fees':{'version':'fixture','commission_bps':'0','minimum_commission_fen':0,'transfer_bps':'0','sell_tax_bps':'0'}}
    bars = [{'date':d,'instrument':'SZ.000001','open_fen':1000,'close_fen':1100,'factor':'1'} for d in days[:3]]
    account = evaluate_portfolio([[days[0],'000001',1]],bars,days[:3],{'000001':'SZ.000001'},portfolio_policy)
    assert account['period_complete'] and account['cash_fen']==1050000 and account['portfolio_return'] is None
    bars[2]['factor']='2'
    blocked = evaluate_portfolio([[days[0],'000001',1]],bars,days[:3],{'000001':'SZ.000001'},portfolio_policy)
    assert blocked['hypothetical_return'] is None and len(blocked['open_positions'])==1
    from trade_system.v2.gap_evidence import ingest_response,read_evidence,adjudicate,sha
    from trade_system.v2.domain import canonical
    from trade_system.v2.gap_audit import seal,read_package
    claim = {'instrument':'SZ.000001','date':'2020-01-02','kind':'session_status',
             'value':{'status':'suspended','coverage':'full_session'}}
    raw = canonical({'schema_version':1,'rows':[claim]}).encode('utf-8')
    raw_path = Path(folder)/'response.json'
    raw_path.write_bytes(raw)
    receipt = {'schema_version':1,'source_family':'other','source_api':'fixture','origin_family':'fixture',
        'reference':'synthetic-only','source_revision':'fixture-v1','source_event_at':'2020-01-02T16:00:00+08:00',
        'provider_received_at':'2020-01-02T16:01:00+08:00','license_status':'unverified','row_index':0,'raw_sha256':sha(raw)}
    receipt_path = Path(folder)/'receipt.json'
    receipt_path.write_text(canonical(receipt),encoding='utf-8')
    imported = ingest_response(receipt_path,raw_path,Path(folder)/'import')
    assert read_evidence(Path(folder)/'import')==imported
    case = {'case_id':'fixture','date':'2020-01-02','instrument':'SZ.000001'}
    decision = adjudicate(case,[imported],asof=imported['available_at'],mode='historical_repair')
    assert decision['classification']=='suspension_supported_not_authenticated' and not decision['can_resume_portfolio']
    old = adjudicate(case,[imported],asof='2020-01-02T18:00:00+08:00',mode='system_replay')
    assert old['classification']=='unknown'
    sealed = Path(folder)/'gap-package'
    sealed.mkdir()
    (sealed/'report.json').write_text(canonical(decision),encoding='utf-8')
    seal(sealed,{'scope':'synthetic-only'})
    assert read_package(sealed)==decision
    from trade_system.v2.native_gap_sources import request_spec,convert_response
    from trade_system.v2.native_gap_run import fingerprint
    assert len(fingerprint())==2
    native_case = {'case_id':'fixture','instrument':'SZ.000001','date':'2020-01-02'}
    spec = request_spec(native_case,'suspend_d')
    native = canonical({'code':0,'data':{'fields':['ts_code','trade_date','suspend_type','suspend_timing'],
                       'items':[['000001.SZ','20200102','S',None]]}}).encode('utf-8')
    mapped = convert_response(spec,native)
    assert mapped['claims'][0]['claim']['value']=={'status':'suspended','coverage':'full_session'}
    assert request_spec(native_case,'hithink_daily')['params']['adjust']=='none'
    from trade_system.v2.entitlement_run import golden_scenario,run_scenario,verify_scenario
    from trade_system.v2.case_supplement import validate_document,METHOD
    entitlement_folder = Path(folder)/'entitlement-fixture'
    entitlement = run_scenario(golden_scenario(),entitlement_folder)
    assert verify_scenario(entitlement_folder)==entitlement
    final = entitlement['final']
    assert final['net_cash_contribution_fen']==675 and final['cost_transfer_fen']==97143
    assert final['released_unrestricted_quantity']==40 and not final['execution_ready']
    validate_document({'id':'fixture','url':'https://static.cninfo.com.cn/fixture.pdf','title':'fixture',
        'published_date':'2026-05-07','pages':[1],'instrument':'SZ.000609','date':'2026-04-22',
        'kind':'session_status','value':{'status':'suspended','coverage':'full_session'},
        'interpretation':'synthetic only','method':METHOD})
    from trade_system.v2.integrated_run import golden_scenarios,run_scenarios,verify_scenarios
    integrated_folder = Path(folder)/'integrated-fixture'
    integrated = run_scenarios(golden_scenarios(),integrated_folder)
    assert verify_scenarios(integrated_folder)==integrated
    assert integrated['entitlement_parent']['final']['fresh_equity_fen']==1315650
    assert integrated['entitlement_parent']['final']['cash_reconciliation_residual_fen']==0
    assert integrated['suspension_exit']['final']['cash_fen']==179980
    assert integrated['delisted_retention']['final']['fresh_equity_fen'] is None
    assert integrated['delisted_retention']['final']['holdings']['SZ.000001']==100
    from trade_system.v2.historical_adapter import stage_inputs
    staged = stage_inputs({'predictions':{'a':[[days[0],'000001',1]],'b':[[days[0],'000001',0]]},
        'calendar':days[:3],'identity_map':{'000001':'SZ.000001'},'bars':[
            {'date':days[1],'instrument':'SZ.000001','open_fen':1000,'close_fen':1100,'factor':'1'},
            {'date':days[2],'instrument':'SZ.000001','open_fen':1000,'close_fen':1100,'factor':'2'}]},
        portfolio_policy,{'cases':[{'case_id':'factor-fixture','date':days[2],'instrument':'SZ.000001'}]},[],[],
        artifact_available_at='2026-09-10T08:00:00+00:00',repair_asof='2026-09-10T09:00:00+00:00',
        replay_asof=days[2]+'T18:00:00+08:00')
    assert staged['report']['potential_windows']==2 and staged['report']['retained_raw_bars']==2
    assert staged['report']['emitted_parent_events']==0 and not staged['report']['historical_diagnostic_ready']
    from trade_system.v2.disclosure_fields import stage_fields, _record, METHOD
    values = {'record_date':days[1],'ex_date':days[2],'cash_pay_date':days[2],
        'gross_cny_per_share':'0.5','bonus_shares_per_share':'0','capitalization_shares_per_share':'0.3',
        'share_delivery_date':None,'share_listing_date':None,
        'cash_payment_scope':'csdc_delegated_A_share_holders_not_self_distribution'}
    document = {'id':'installed-fixture','url':'https://static.cninfo.com.cn/fixture.PDF',
        'title':'synthetic installation probe','published_date':days[0],
        'instrument':'SZ.000001','date':days[2],'kind':'distribution','archive_id':None,
        'notes':'synthetic not real disclosure','method':METHOD,
        'fields':{k:{'value':v,'pages':[1] if v is not None else [],'basis':'synthetic'} for k,v in values.items()}}
    at = '2026-09-10T09:00:00+00:00'
    disclosure = stage_fields(staged['report'],[],[_record(document,'synthetic',at,at)],
        repair_asof=at,replay_asof=days[2]+'T18:00:00+08:00')
    assert disclosure['historically_available_new_documents']==0
    assert disclosure['cases'][0]['total_new_shares_per_share']=='0.3'
    assert 'explicit_share_delivery_date' in disclosure['cases'][0]['remaining_source_fields']
    assert disclosure['emitted_parent_events']==0 and not disclosure['execution_ready']
    from trade_system.v2.daily_account import diagnose_case,ASSUMPTIONS,SCOPE as DAILY_SCOPE
    historical_case = {'case_id':'installed-daily-case','instrument':'SZ.000001','date':days[2],
        'nonconflicting_disclosure_values':{**values,'share_listing_date':days[2]},
        'conflicting_fields':[],'native_cash_conflict_evidence_ids':[]}
    historical_bundle = {'calendar':days,'bars':staged['daily_observations']['rows']}
    diagnostic = diagnose_case(historical_bundle,historical_case,repair_asof=at,assumptions=ASSUMPTIONS)
    assert diagnostic['case_hypothesis_complete'] and diagnostic['config']['scope']==DAILY_SCOPE
    assert sum(f['quantity'] for f in diagnostic['final']['fills'])==130
    assert diagnostic['final']['cash_reconciliation_residual_fen']==0
    historical_case['nonconflicting_disclosure_values']={'halt_start_date':days[2]}
    historical_bundle['bars']=historical_bundle['bars'][:1]
    blocked = diagnose_case(historical_bundle,historical_case,repair_asof=at,assumptions=ASSUMPTIONS)
    assert blocked['final']['holdings']['SZ.000001']==100 and blocked['hypothetical_case_return'] is None
    assert [r['status_used'] for r in blocked['daily']]==['suspended','unknown','unknown']
print(json.dumps({'python':sys.version.split()[0], 'module':trade_system.__file__,
                  'migrations':len(discover_migrations()), 'tables':tables, 'service':status,
                  'installed_context_migration_and_import':True,'installed_event_and_ledger_restart':True,
                  'installed_exit_confirmation_fill_attribution':True,'installed_rolling_fixture_runner':True,
                  'installed_portfolio_accounting_and_corporate_action_stop':True,
                  'installed_gap_evidence_import_time_gate_and_manifest':True,
                  'installed_native_source_request_and_mapping':True,
                  'installed_entitlement_replay_and_manual_document_contract':True,
                  'installed_integrated_parent_halt_exit_and_delisted_retention':True,
                  'installed_frozen_input_adapter_no_fake_market_events':True,
                  'installed_disclosure_fields_no_fake_delivery_or_backdating':True,
                  'installed_historical_case_same_ledger_and_unknown_halt_retention':True}))
'''
    result = subprocess.run([str(installed_python), '-I', '-c', probe], cwd=output,
                            capture_output=True, text=True, check=True, timeout=60)
    installed = json.loads(result.stdout.strip().splitlines()[-1])
    report = {'scope':'local_implementation_evidence_not_production_acceptance',
              'base_commit':subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
              'worktree_is_dirty':True, 'source_sha256':source_hash, 'source_files':hashes,
              'tests':tests, 'installed_wheel':{'path':str(wheel), 'sha256':digest(wheel), **installed},
              'execution_ready':False, 'production_cutover':False, 'real_account_acceptance':'pending_user_has_no_export'}
    (output/'verification.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('scope','source_sha256','tests','installed_wheel','execution_ready')}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--installed-python', type=Path, required=True)
    parser.add_argument('--wheel', type=Path, required=True)
    parser.add_argument('--test-report', type=Path, action='append')
    args = parser.parse_args()
    capture(args.output.resolve(), args.installed_python.resolve(), args.wheel.resolve(), args.test_report)
