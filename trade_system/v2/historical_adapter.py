"""Read-only frozen-input staging, NOT historical account execution or repair.

Preserve the entire OOS sample and potential holding windows. Daily prices,
late-received disclosures and event-ready account observations are different
contracts; this module never fabricates the missing transitions between them.
"""
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
from pathlib import Path

from .case_supplement import verify_supplement
from .domain import identity, instrument, now_utc, number, utc
from .entitlements import validate_terms
from .gap_audit import potential_windows, read_package
from .gap_evidence import adjudicate, read_evidence, read_json, sha, write_json
from .integrated_run import implementation as parent_implementation
from .paper_ledger import fen
from .portfolio_experiment import read_portfolio_result
from .portfolio_diagnostic import validate_policy
from .rolling_research import file_hash


SCOPE = 'frozen_historical_input_staging_not_account_replay'
MODE = 'historical_repair_with_separate_knowledge_replay'


def implementation():
    return {**parent_implementation(), **{name:file_hash(Path(__file__).with_name(name)) for name in
        ('historical_adapter.py','portfolio_experiment.py','portfolio_diagnostic.py',
         'gap_audit.py','case_supplement.py','native_gap_run.py','native_gap_sources.py','rolling_research.py')}}


def _date(value):
    if not isinstance(value,str) or date.fromisoformat(value).isoformat() != value:
        raise ValueError('canonical source date required')


def validate_bundle(bundle, policy):
    if set(bundle) != {'predictions','bars','calendar','identity_map'}:
        raise ValueError('strict original frozen input bundle required')
    validate_policy(policy)
    days, mapping, predictions = bundle['calendar'], bundle['identity_map'], bundle['predictions']
    if not isinstance(days,list) or not days or days != sorted(set(days)) or len(days) > 2000:
        raise ValueError('bounded ordered frozen calendar required')
    for d in days:
        _date(d)
    if not isinstance(mapping,dict) or not mapping or len(mapping) > 20000 or len(set(mapping.values())) != len(mapping):
        raise ValueError('one-to-one declared source identity map required')
    for raw, canonical in mapping.items():
        instrument(canonical)
        if raw != canonical.split('.')[1]:
            raise ValueError('research identity and explicit exchange mapping disagree')
    if not isinstance(predictions,dict) or not 1 <= len(predictions) <= 20:
        raise ValueError('bounded original variant set required')
    expected = None
    for name, rows in predictions.items():
        if not isinstance(name,str) or not name or not isinstance(rows,list) or not 1 <= len(rows) <= 100000:
            raise ValueError('bounded nonempty named prediction variant required')
        seen = set()
        for row in rows:
            if not isinstance(row,list) or len(row) != 3:
                raise ValueError('prediction-only triples required, no labels or invented timestamps')
            d, code, score = row
            if d not in days or code not in mapping or (d,code) in seen:
                raise ValueError('invalid/duplicate frozen prediction identity')
            number(score)
            seen.add((d,code))
        if expected is not None and seen != expected:
            raise ValueError('all variants must preserve the same full OOS identities')
        expected = seen
    if {c for _,c in expected} != set(mapping):
        raise ValueError('mapping must match full OOS code universe')
    bars = bundle['bars']
    if not isinstance(bars,list) or len(bars) > 100000:
        raise ValueError('bounded frozen bars required')
    seen = set()
    for bar in bars:
        if set(bar) != {'date','instrument','open_fen','close_fen','factor'}:
            raise ValueError('strict frozen raw price fields required')
        key = bar['date'],bar['instrument']
        if key in seen or key[0] not in days or key[1] not in mapping.values():
            raise ValueError('duplicate or unmapped frozen raw bar')
        seen.add(key)
        for key in ('open_fen','close_fen'):
            if bar[key] is not None:
                fen(bar[key])
        if bar['factor'] is not None and number(bar['factor']) <= 0:
            raise ValueError('invalid factor observation')
    return expected


def stage_inputs(bundle, policy, audit, evidence, documents, *, artifact_available_at, repair_asof, replay_asof):
    """Pure transformation. All production sources are validated by the runner.

    artifact_available_at is proof of the frozen file's local existence, not an
    invented per-row market receipt. replay_asof never rewrites effective dates.
    """
    expected = validate_bundle(bundle,policy)
    repair, replay_cutoff, known = utc(repair_asof),utc(replay_asof),utc(artifact_available_at)
    if replay_cutoff > repair or known > repair:
        raise ValueError('future/inconsistent knowledge cutoffs')
    windows = potential_windows(bundle,policy)
    wanted, owners = set(),defaultdict(list)
    for window in windows:
        if len(window['days']) != policy['hold_sessions']:
            raise ValueError('candidate window lacks full frozen horizon')
        window['window_id'] = identity(window)
        for d in window['days']:
            key = d,window['instrument']
            wanted.add(key)
            owners[key].append(window['window_id'])
    indexed = {(b['date'],b['instrument']):b for b in bundle['bars']}
    if set(indexed) - wanted:
        raise ValueError('unrequested raw rows must not expand the frozen experiment')
    problems = defaultdict(set)
    for window in windows:
        first = indexed.get((window['days'][0],window['instrument']))
        for i,d in enumerate(window['days']):
            key = d,window['instrument']
            bar = indexed.get(key)
            if bar is None:
                problems[key].add('missing_raw_bar')
                continue
            if not bar['open_fen'] or not bar['close_fen']:
                problems[key].add('missing_or_zero_raw_price')
            if bar['factor'] is None:
                problems[key].add('missing_factor')
            if i and first and first['factor'] is not None and bar['factor'] is not None and number(first['factor']) != number(bar['factor']):
                problems[key].add('factor_change_needs_entitlement_not_ratio_inference')
    cases = audit['cases']
    case_keys = {(c['date'],c['instrument']) for c in cases}
    if len(case_keys) != len(cases) or len({c['case_id'] for c in cases}) != len(cases) or case_keys != set(problems):
        raise ValueError('frozen anomaly identities differ from audit; create a new audit')
    case_rows, actions, knowledge_counts = [],[],Counter()
    for case in sorted(cases,key=lambda c:(c['date'],c['instrument'])):
        key = case['date'],case['instrument']
        decision = adjudicate(case,evidence,asof=repair,mode='historical_repair')
        past = adjudicate(case,evidence,asof=replay_cutoff,mode='system_replay')
        knowledge_counts[past['classification']] += 1
        included = set(decision['included_evidence_ids'])
        candidates = [r for r in evidence if r['evidence_id'] in included and r['claim']['kind'] == 'corporate_action']
        terms_docs = [r for r in documents if r['document']['kind'] == 'entitlement_terms'
            and (r['document']['date'],r['document']['instrument']) == key and utc(r['available_at']) <= repair]
        missing_data, missing_policy, derived = [],[],[]
        if 'missing_factor' in problems[key]:
            missing_data.append('raw_factor_observation')
        if 'missing_or_zero_raw_price' in problems[key]:
            missing_data.append('positive_unadjusted_open_and_close')
        if 'missing_raw_bar' in problems[key]:
            missing_data += ['structured_exact_suspension_span','structured_resumption_or_terminal_lifecycle','post_resumption_raw_prices']
            missing_policy += ['frozen_halt_mark_expiry_and_exit_horizon']
            if decision['classification'] != 'suspension_supported_not_authenticated':
                missing_data.append('case_day_session_explanation')
        if 'factor_change_needs_entitlement_not_ratio_inference' in problems[key]:
            missing_policy += ['tax_basis_and_explicit_scenario_assessment','cash_share_rounding_and_cost_policy']
            derived += ['record_close_lots_from_new_parent_replay','per_lot_receivable_delivery_release_events']
            if terms_docs:
                # Conflicting transcriptions remain visible. A source order is not a conflict resolver.
                distinct = {identity({k:v for k,v in r['terms'].items() if k not in ('available_at','evidence_refs')}) for r in terms_docs}
                if len(distinct) > 1 or candidates:
                    missing_data.append('resolve_corporate_action_component_or_revision_conflict')
                for row in terms_docs:
                    validate_terms(row['terms'])
                    actions.append({'case_id':case['case_id'],'source':'manual_disclosure_terms',
                        'available_at':row['available_at'],'terms':row['terms'],
                        'terms_schema_complete':True,'system_replay_eligible':utc(row['available_at']) <= replay_cutoff,
                        'account_receipt_proven':False,'can_apply_to_account':False})
                missing_data += ['registry_fractional_allocation_if_needed','independent_disclosure_review']
            elif candidates:
                for row in candidates:
                    value = row['claim']['value']
                    actions.append({'case_id':case['case_id'],'source':'native_cash_component',
                        'available_at':row['available_at'],'terms':deepcopy(value),
                        'terms_schema_complete':False,'system_replay_eligible':utc(row['available_at']) <= replay_cutoff,
                        'evidence_id':row['evidence_id'],'account_receipt_proven':False,'can_apply_to_account':False})
                missing_data += ['record_date','cash_pay_date','complete_share_component_declaration']
            else:
                missing_data += ['dated_corporate_action_terms']
        if decision['classification'] == 'conflicting_evidence':
            missing_data.append('resolve_conflicting_evidence')
        missing_data += ['independent_source_and_use_rights_review']
        case_rows.append({'case_id':case['case_id'],'date':key[0],'instrument':key[1],
            'problems':sorted(problems[key]),'window_ids':sorted(set(owners[key])),
            'historical_repair_decision':decision,'system_replay_decision':past,
            'missing_source_fields':sorted(set(missing_data)),'missing_declared_policies':sorted(set(missing_policy)),
            'derive_only_after_new_replay':derived,'can_resume_portfolio':False})
    daily = [{'source_row_index':i,**bar,'effective_date':bar['date'],'intraday_effective_at':None,
              'historical_row_available_at':None,'parent_market_event_ready':False}
             for i,bar in enumerate(bundle['bars'])]
    report = {'scope':SCOPE,'mode':MODE,'repair_asof':repair.isoformat(),'replay_asof':replay_cutoff.isoformat(),
        'frozen_artifact_proven_available_at':known.isoformat(),
        'full_oos_identity_sha256':identity(sorted(expected)),
        'variant_counts':{name:len(rows) for name,rows in bundle['predictions'].items()},
        'prediction_value_sha256':identity(bundle['predictions']),
        'calendar_sha256':identity(bundle['calendar']),'identity_map_sha256':identity(bundle['identity_map']),
        'window_counts':dict(Counter(w['variant'] for w in windows)),
        'potential_windows':len(windows),'requested_raw_identities':len(wanted),'retained_raw_bars':len(daily),
        'missing_raw_identities':len(wanted-set(indexed)),
        'case_count':len(case_rows),'cases':case_rows,
        'repair_classification_counts':dict(Counter(c['historical_repair_decision']['classification'] for c in case_rows)),
        'replay_classification_counts':dict(knowledge_counts),
        'daily_rows_with_proven_historical_availability':0,
        'frozen_artifact_available_by_replay_cutoff':known <= replay_cutoff,
        'action_candidates':len(actions),'complete_action_term_candidates':sum(a['terms_schema_complete'] for a in actions),
        'parent_adapter_contract':{
            'daily_bar':'staged_raw_daily_observation_not_quote_or_capacity',
            'selection_window':'potential_selection_not_order_or_fill',
            'action_terms':'candidate_schedule_not_account_registration_or_receipt',
            'session_status':'exact_case_day_only_not_inferred_multi_day_halt',
            'missing_event_fields':['intraday_observation_time','buy_capacity','sell_capacity','actual_or_declared_fill_policy'],
            'target_accepts':'synthetic_integrated_entitlement_account_not_execution',
            'historical_scope_supported_by_target':False},
        'next_registration_blockers':['explicit_new_historical_diagnostic_policy_and_scope',
            'daily_observation_to_parent_event_semantics_not_implemented','case_source_fields_and_declared_policies',
            'historical_repair_vs_knowledge_replay_must_not_be_mixed'],
        'emitted_parent_events':0,'new_experiment_registered':False,
        'historical_diagnostic_ready':False,'execution_ready':False,'portfolio_resumed':False,
        'selection_advantage':None,'portfolio_return':None,'signal_impact':'disabled'}
    return {'report':report,'daily_observations':{'scope':'raw_daily_observations_not_parent_events','rows':daily},
            'selection_windows':{'scope':'all_frozen_potential_windows_not_actual_holdings','rows':windows},
            'action_candidates':{'scope':'candidate_terms_not_account_receipts','rows':actions}}


def render_review(report):
    lines = ['# 冻结历史输入适配与缺字段清单', '',
        '只读准备包，不是账户回放、完整回测或实盘资格。全部变体及候选窗口保留，不筛掉未解决案件。', '',
        f"候选窗口 {report['potential_windows']}；所需证券日期 {report['requested_raw_identities']}；日线 {report['retained_raw_bars']}；缺行 {report['missing_raw_identities']}。",
        f"修复截止 {report['repair_asof']}；历史知识截止 {report['replay_asof']}。日线字段未保留逐行历史可用时间，不据此伪造时间戳。", '',
        '| 日期 | 证券 | 当前修复裁决 | 历史知识裁决 | 缺来源字段 | 待声明政策 |',
        '|---|---|---|---|---|---|']
    for c in report['cases']:
        lines.append('| '+' | '.join([c['date'],c['instrument'],c['historical_repair_decision']['classification'],
            c['system_replay_decision']['classification'],', '.join(c['missing_source_fields']),
            ', '.join(c['missing_declared_policies'])])+' |')
    lines += ['', 'structured 字段缺口表示现有证据尚未映射为完整机器可消费区间，不等于公告原文从未提及。',
        '登记日持仓和批次应收应从新的历史假设父账推导，不能要求用户真实账户来冒充研究账户；具体真实到账资格仍独立验收。',
        '日线价格不等于盘口价格/容量，公告计划不等于账户到账；本包生成父账事件 0 条，不登记新实验，不计算收益。']
    return '\n'.join(lines)+'\n'


def _load_parents(plan):
    portfolio, audit_path, supplement = (Path(plan[k]).resolve() for k in ('portfolio','audit','supplement'))
    original = read_portfolio_result(portfolio)
    registration,_ = read_json(portfolio/'registration.json')
    audit = read_package(audit_path)
    disclosure = verify_supplement(supplement)
    supplement_reg,_ = read_json(supplement/'registration.json')
    if (audit['parent_registration_id'] != original['registration_id'] or
        Path(audit['parent_portfolio']).resolve() != portfolio or
        audit['parent_completion_sha256'] != file_hash(portfolio/'run/completed.json') or
        audit['snapshot_sha256'] != registration['snapshot_sha256'] or
        Path(supplement_reg['plan']['audit']).resolve() != audit_path):
        raise ValueError('portfolio/audit/disclosure parents are not the same frozen investigation')
    inputs,raw = read_json(portfolio/'inputs.json')
    if sha(raw) != registration['input_sha256'] or len(inputs['bars']) != registration['available_bars']:
        raise ValueError('frozen inputs changed during parent inspection')
    evidence = [read_evidence(p) for p in sorted((supplement/'adjudication/evidence').iterdir())]
    binding = {'portfolio_registration_sha256':file_hash(portfolio/'registration.json'),
        'portfolio_completion_sha256':file_hash(portfolio/'run/completed.json'),
        'input_sha256':file_hash(portfolio/'inputs.json'),'audit_sha256':file_hash(audit_path/'completed.json'),
        'supplement_sha256':file_hash(supplement/'completed.json')}
    return registration,audit,disclosure,evidence,inputs,raw,binding


def build_adapter(plan,output,*,clock=now_utc):
    if set(plan) != {'scope','mode','portfolio','audit','supplement','replay_asof'} or plan['scope'] != SCOPE or plan['mode'] != MODE:
        raise ValueError('strict read-only adapter investigation plan required')
    if utc(plan['replay_asof']) > utc(clock()):
        raise ValueError('future replay cutoff')
    output = Path(output).resolve()
    if output.exists():
        raise FileExistsError('new adapter evidence directory required')
    reg,audit,disclosure,evidence,inputs,raw,binding = _load_parents(plan)
    now = utc(clock()).isoformat()
    outputs = stage_inputs(inputs,reg['policy'],audit,evidence,disclosure['documents'],
        artifact_available_at=reg['registered_at'],repair_asof=now,replay_asof=plan['replay_asof'])
    output.mkdir(parents=True,exist_ok=False)
    registration = {'plan':plan,'created_at':now,'parents':binding,'implementation':implementation()}
    write_json(output/'registration.json',registration)
    with (output/'frozen_inputs.json').open('xb') as stream:
        stream.write(raw)
    for name,value in outputs.items():
        write_json(output/(name+'.json'),value)
    with (output/'review.md').open('x',encoding='utf-8') as stream:
        stream.write(render_review(outputs['report']))
    if _load_parents(plan)[-1] != binding:
        raise ValueError('source parents changed during adapter build; incomplete output is not sealed')
    manifest = {'registration_id':identity(registration),
        'artifact_hashes':{p.name:file_hash(p) for p in output.iterdir() if p.is_file()}}
    write_json(output/'completed.json',{**manifest,'manifest_id':identity(manifest)})
    return outputs['report']


def verify_adapter(folder):
    folder = Path(folder).resolve()
    manifest,_ = read_json(folder/'completed.json')
    mid = manifest.pop('manifest_id')
    if identity(manifest) != mid:
        raise ValueError('adapter manifest changed')
    paths = list(folder.iterdir())
    if any(not p.is_file() or p.is_symlink() for p in paths) or {p.name:file_hash(p) for p in paths if p.name != 'completed.json'} != manifest['artifact_hashes']:
        raise ValueError('adapter package members/bytes changed')
    registration,_ = read_json(folder/'registration.json')
    if identity(registration) != manifest['registration_id'] or registration['implementation'] != implementation():
        raise ValueError('adapter registration/source changed')
    reg,audit,disclosure,evidence,inputs,raw,binding = _load_parents(registration['plan'])
    if binding != registration['parents'] or file_hash(folder/'frozen_inputs.json') != binding['input_sha256']:
        raise ValueError('adapter source parent changed')
    expected = stage_inputs(inputs,reg['policy'],audit,evidence,disclosure['documents'],
        artifact_available_at=reg['registered_at'],repair_asof=registration['created_at'],replay_asof=registration['plan']['replay_asof'])
    for name,value in expected.items():
        if read_json(folder/(name+'.json'))[0] != value:
            raise ValueError('adapter transformation does not reproduce: '+name)
    if (folder/'review.md').read_text(encoding='utf-8') != render_review(expected['report']):
        raise ValueError('adapter readable review not reproducible')
    return expected['report']
