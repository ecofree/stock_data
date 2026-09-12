"""Bounded real-data acceptance. Reads source only; writes a new isolated evidence folder."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import duckdb
from trade_system.normalize import _create_kline_daily
from trade_system.units import normalization_sql
from trade_system.v2.domain import file_hash, identity
from tools.v2.verify_delivery import source_files


def partition(source, output, sessions=20):
    if not 1 <= sessions <= 32:
        raise ValueError('one to 32 observed sessions per acceptance partition')
    target=output/'partition.duckdb'
    if target.exists(): raise ValueError('new partition required')
    with duckdb.connect(str(source),read_only=True) as original:
        dates=[str(r[0])[:10] for r in original.execute(
            'SELECT DISTINCT date FROM tushare_daily ORDER BY date DESC LIMIT ?', [sessions]).fetchall()]
        if not dates: raise ValueError('nonempty source partition required')
        fields=','.join('?' for _ in dates)
        # Ordered original row identity is carried to the immutable export.
        raw=original.execute(f'SELECT *, rowid AS source_rowid FROM tushare_daily WHERE date IN ({fields}) ORDER BY rowid',dates).fetch_arrow_table()
        with duckdb.connect(str(target)) as con:
            con.register('source_partition',raw)
            con.execute('CREATE TABLE tushare_daily AS SELECT * FROM source_partition ORDER BY source_rowid')
            raw_path=output/'raw-partition.parquet'
            canonical_path=output/'canonical-partition.parquet'
            con.execute('COPY tushare_daily TO ? (FORMAT PARQUET)',[str(raw_path)])
            _create_kline_daily(con)
            expected=normalization_sql('turnover','amount_unit','amount')
            mismatches=con.execute(f'''WITH expected AS (SELECT date,stock_code,{expected} AS amount,
                row_number() OVER (PARTITION BY date,stock_code ORDER BY fetched_at DESC NULLS LAST,rowid DESC) AS rn
                FROM tushare_daily WHERE close IS NOT NULL)
                SELECT count(*) FROM expected e JOIN v_kline_daily v ON e.date=v.trade_date AND e.stock_code=v.stock_code
                WHERE e.rn=1 AND (e.amount IS DISTINCT FROM v.turnover OR v.amount_unit<>'yuan' OR v.volume_unit<>'shares')''').fetchone()[0]
            con.execute('COPY v_kline_daily TO ? (FORMAT PARQUET)',[str(canonical_path)])
            counts=con.execute('SELECT count(*),count(turnover),count(volume),count(DISTINCT (trade_date,stock_code)) FROM v_kline_daily').fetchone()
            reasons=con.execute('''SELECT coalesce(amount_unit,'unknown') AS source_unit,
                count(*) AS rows,count(turnover) AS provided FROM tushare_daily GROUP BY 1 ORDER BY 1''').fetchall()
    if mismatches or counts[0]!=counts[3]:
        raise ValueError('canonical consumer mismatch or duplicate identities')
    return {'source':str(source),'source_modified':False,'dates':sorted(dates),'raw_rows':raw.num_rows,
        'canonical_rows':counts[0],'nonempty_amount':counts[1],'nonempty_volume':counts[2],
        'source_unit_groups':reasons,'consumer_mismatches':mismatches,
        'raw_sha256':file_hash(raw_path),'canonical_sha256':file_hash(canonical_path),
        'scope':'bounded_selected_partition_not_full_database_repair'}


def preview(source, output, day, as_of=None, *, workspace_only=False):
    from trade_system.review_web import _render_review_bundle
    from trade_system.daily_review import build_daily_review_context
    from scripts.audit_daily_review_artifact import audit_artifact
    context=build_daily_review_context(source,day,as_of=as_of)
    from trade_system.review_facts import workspace_snapshot
    context['review_as_of']=as_of or 'current_build_clock_not_original_PIT'
    snapshot=workspace_snapshot(context)
    (output/'review-snapshot.json').write_text(json.dumps(snapshot,ensure_ascii=False,indent=2),encoding='utf-8')
    if workspace_only:
        return {'snapshot_id':snapshot['snapshot_id'],'trade_date':day,'themes':len(snapshot['themes']),
                'legacy_page_rendered':False,'execution_ready':False,'production_publication':False}
    html, selected, _=_render_review_bundle(source,day,context=context,as_of=as_of)
    flow=context['capital_flow']
    explanations={'trade_date':day,'as_of':as_of,'scope':'existing_candidate_context_not_new_scores_or_model_attribution',
                  'candidates':flow['candidate_picks'],'sources':flow['concept_source_evidence'],
                  'history':flow['concept_history'],'execution_ready':False}
    (output/'candidate-explanations.json').write_text(json.dumps(explanations,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    if selected!=day: raise ValueError('requested review day changed')
    target=output/'daily_review.html'; target.write_text(html,encoding='utf-8')
    good=audit_artifact(source,target,day)
    mutant=output/'missing-inline.html'
    mutant.write_text(html.replace("id='trail-data'", "id='removed-trail-data'"),encoding='utf-8')
    rejected=audit_artifact(source,mutant,day)
    if good['status']!='pass' or rejected['status']!='fail':
        raise ValueError('real page acceptance/mutation failed: '+json.dumps(good,ensure_ascii=False))
    return {'correct_page':good,'missing_payload_rejected':True,'sha256':file_hash(target), 'explicit_asof':as_of,
            'candidate_context_count':len(flow['candidate_picks']),
            'candidates_with_concept_evidence':sum(bool(r.get('concept_evidence')) for r in flow['candidate_picks']),
            'production_publication':False}


def sector_replay(source, output, day):
    """Replay retained real rows; fault injections are never called live evidence."""
    import math
    from unittest.mock import patch
    from scripts import collect_intraday_sector_flow_full as collector
    from trade_system.capital_flow_health import _relation_health
    from datetime import timedelta
    with duckdb.connect(str(source), read_only=True) as con:
        raw = con.execute("""SELECT sector_code,raw_json FROM multi_source_sector_flow
            WHERE source_date=CAST(? AS DATE) AND provider='eastmoney_sector_full'
            ORDER BY sector_code""", [day]).fetchall()
    if not raw or len(raw) > 10000: raise ValueError('bounded nonempty retained sector snapshot required')
    rows = [json.loads(row[1]) for row in raw]
    codes = [str(row[0]) for row in raw]
    if len(set(codes)) != len(codes) or sorted(str(r['sector_code']) for r in rows) != codes:
        raise ValueError('retained sector payload identities differ')
    receipt = output/'sector-replay-input.json'
    receipt.write_text(json.dumps({'source':str(source),'date':day,'rows':rows},ensure_ascii=False),encoding='utf-8')
    version = file_hash(receipt)
    target = output/'sector-replay.duckdb'
    if target.exists(): raise ValueError('new sector replay database required')
    page_size = 100
    max_pages = math.ceil(len(rows)/page_size)
    cases = {}
    preserved = None
    for scenario in ('retained_snapshot', 'missing_identity', 'equal_count_wrong_identity', 'duplicate_pages'):
        selected = rows if scenario == 'retained_snapshot' else rows[1:] if scenario == 'missing_identity' else [dict(rows[0],sector_code='FAULT-INJECTION-ID'),*rows[1:]]
        def page(*, page, sort_order, **kwargs):
            ordered = list(reversed(selected)) if sort_order == '1' else selected
            start = 0 if scenario == 'duplicate_pages' else (page-1)*page_size
            return ordered[start:start+page_size], {'total':len(rows),'origin':'retained_rows_replay_not_network'}
        with patch.object(collector, '_from_em_sector_flow_page', side_effect=page), patch.object(collector.shared_host_limiter,'acquire'):
            result = collector.collect_full_sector_flow(target,day,page_size=page_size,
                max_pages=max_pages,pause_seconds=0,expected_codes=codes,catalogue_version=version)
        with duckdb.connect(str(target),read_only=True) as con:
            state = con.execute("SELECT * FROM multi_source_sector_flow WHERE provider='eastmoney_sector_full' ORDER BY sector_code").fetchall()
            state_hash = identity(json.loads(json.dumps(state,default=str)))
            observations = con.execute('SELECT count(*) FROM multi_source_observation').fetchone()[0]
            if preserved is None:
                assert result['pagination']['promoted'] and len(state)==len(rows)
                preserved = state_hash
                from trade_system.time_utils import CHINA_TZ
                clock = datetime.now(CHINA_TZ).replace(tzinfo=None)
                fresh = _relation_health(con,'multi_source_sector_flow',day,'sector_code',max_age_seconds=7200,now=clock)
                expired = _relation_health(con,'multi_source_sector_flow',day,'sector_code',max_age_seconds=7200,now=clock+timedelta(hours=3))
                assert fresh['recent_codes']==len(rows) and expired['recent_codes']==0
            else:
                assert not result['pagination']['promoted'] and state_hash==preserved
        cases[scenario] = {'status':result['status'],'pagination':result['pagination'],
            'stored_rows':len(state),'preserved_snapshot_sha256':state_hash,'observation_rows':observations}
    return {'input_rows':len(rows),'input_sha256':version,'source_modified':False,
        'scope':'retained_real_rows_through_actual_collector_with_simulated_pages_not_new_provider_qualification',
        'catalogue_basis':'same_frozen_snapshot_for_replay_only_not_independent_live_catalogue',
        'new_provider_requests':0,'cases':cases,
        'freshness':{'accepted_now':fresh['recent_codes'],'accepted_after_3h':expired['recent_codes'],
            'scope':'isolated_replay_write_clock_not_original_historical_arrival'}}


def ths_replay(source, output, day):
    """Retained qualified views and native flow, never a new online catalogue."""
    from scripts.collect_intraday_sector_flow_full import _publish_ths_aggregate
    from trade_system.multi_source_store import MultiSourceStore
    from datetime import timedelta
    from trade_system.review_queries import _apply_qualified_concept_flow
    target = output/'ths-replay.duckdb'
    tables = (
        ('tushare_trade_cal', 'cal_date'),
        ('ths_concept_snapshot_expectation', 'trade_date'),
        ('v_default_concept_daily', 'trade_date'),
        ('v_default_concept_stock_history', 'trade_date'),
        ('multi_source_stock_flow', 'source_date'),
        ('multi_source_sector_flow', 'source_date'),
    )
    retained = {}
    with duckdb.connect(str(source), read_only=True) as original, duckdb.connect(str(target)) as copy:
        days=original.execute("SELECT DISTINCT CAST(cal_date AS DATE) FROM tushare_trade_cal WHERE exchange='SSE' "
            "AND CAST(is_open AS BOOLEAN) AND CAST(cal_date AS DATE)<=CAST(? AS DATE) ORDER BY 1 DESC LIMIT 3",[day]).fetchall()
        if len(days)!=3 or str(days[0][0])!=day:
            raise ValueError('exact three-session calendar required for real history replay')
        first=str(days[-1][0])
        for name, date_column in tables:
            data = original.execute(f'SELECT * FROM {name} WHERE {date_column} BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) LIMIT 500001', [first,day]).fetch_arrow_table()
            if data.num_rows > 500000:
                raise ValueError('THS retained replay row budget exceeded')
            retained[name] = data.num_rows
            copy.register('retained_arrow', data)
            copy.execute(f'CREATE TABLE {name} AS SELECT * FROM retained_arrow')
            copy.unregister('retained_arrow')
    with MultiSourceStore(target) as store:
        con = store.con
        def slice_hash():
            rows = con.execute("SELECT * FROM multi_source_sector_flow WHERE "
                "provider='derived_ths_stock_aggregate' ORDER BY source_date,sector_code").fetchall()
            return hashlib.sha256(json.dumps(rows,default=str).encode()).hexdigest()
        clock = con.execute("SELECT max(fetched_at) FROM multi_source_stock_flow "
            "WHERE source_date=CAST(? AS DATE) AND provider IN ('eastmoney_market','eastmoney_intraday_clist','eastmoney_intraday_clist_delay')",[day]).fetchone()[0]
        if clock is None:
            raise ValueError('retained native flow timestamp required')
        before = slice_hash()
        baseline = _publish_ths_aggregate(store, day, now=clock)
        after = slice_hash()
        if not baseline['promoted']:
            assert before == after, 'rejected retained input changed last good slice'
        consumer = _apply_qualified_concept_flow({},con,day,now=clock)
        subset = consumer['qualified_concept_flow']
        included = {r['sector_code'] for r in subset['rows']}
        excluded = {r['sector_code'] for r in subset['contract'].get('excluded_concepts', [])}
        assert included.isdisjoint(excluded)
        assert len(included) == baseline['complete_concepts']
        assert all(r['sector_code'] in included for r in consumer['sector_inflow']+consumer['sector_outflow'])
        (output/'qualified-concept-flow.json').write_text(json.dumps(consumer,ensure_ascii=False,indent=2),encoding='utf-8')
        stale_consumer = _apply_qualified_concept_flow({},con,day,now=clock+timedelta(hours=4))
        assert not stale_consumer['sector_inflow'] and not stale_consumer['sector_outflow']
        assert slice_hash() == after
        expired = _publish_ths_aggregate(store, day, now=clock+timedelta(hours=4))
        assert not expired['promoted'] and slice_hash() == after
        return dict(scope='retained_qualified_views_and_native_flow_not_new_online_qualification',
                    source_modified=False, new_provider_requests=0, input_rows=retained,
                    replay_clock=str(clock), clock_scope='retained_latest_provider_timestamp_not_claimed_historical_arrival_proof',
                    baseline=baseline, expired=expired, rejected_snapshot_preserved=True,
                    subset_consumer={'eligible':len(included),'excluded':len(excluded),'ranking_is_subset':True,
                                     'history_matched':sum(r['history_status']=='matched' for r in consumer['concept_history']['rows']),
                                     'expired_rankings_empty':True,'no_canonical_writes':True},
                    before_sha256=before, after_sha256=after)


def writer_inventory(root):
    """Static inventory, not a sandbox or proof that every call is covered."""
    import ast
    calls=[]
    for relative, digest in source_files().items():
        if not relative.endswith('.py') or relative.startswith(('tests/', 'tools/')):
            continue
        tree=ast.parse((root/relative).read_text(encoding='utf-8-sig'))
        aliases={'duckdb'}; direct=set()
        for node in ast.walk(tree):
            if isinstance(node,ast.Import):
                aliases.update(a.asname or a.name for a in node.names if a.name=='duckdb')
            if isinstance(node,ast.ImportFrom) and node.module=='duckdb':
                direct.update(a.asname or a.name for a in node.names if a.name=='connect')
        for node in ast.walk(tree):
            if not isinstance(node,ast.Call): continue
            fn=node.func
            connect=(isinstance(fn,ast.Name) and fn.id in direct) or (isinstance(fn,ast.Attribute)
                and fn.attr=='connect' and isinstance(fn.value,ast.Name) and fn.value.id in aliases)
            if connect:
                readonly=any(k.arg=='read_only' and isinstance(k.value,ast.Constant) and k.value.value is True for k in node.keywords)
                target=node.args[0] if node.args else next((k.value for k in node.keywords if k.arg=='database'),None)
                memory=isinstance(target,ast.Constant) and target.value==':memory:'
                review=('read_only' if readonly else 'memory_only' if memory else
                    'guarded_legacy_connector' if relative=='trade_system/db_utils.py' else
                    'v2_owned_store_schema_and_owner_guard' if relative=='trade_system/v2/storage.py' else 'needs_review')
                calls.append(dict(file=relative,line=node.lineno,kind='literal_read_only' if readonly else 'potential_direct_writer',
                    target_review=review,source_sha256=digest))
            if isinstance(fn,ast.Attribute) and fn.attr in {'execute','executemany','sql'} and node.args:
                first=node.args[0]
                text=first.value if isinstance(first,ast.Constant) and isinstance(first.value,str) else ''
                if text.lstrip().upper().startswith(('CREATE ','ALTER ','DROP ')):
                    calls.append(dict(file=relative,line=node.lineno,kind='literal_ddl_site',source_sha256=digest))
    return dict(scope='static_call_inventory_not_full_authority_proof',calls=calls,
        direct_writer_sites=sum(c['kind']=='potential_direct_writer' for c in calls),
        unclassified_connect_sites=sum(c.get('target_review')=='needs_review' for c in calls),
        ddl_sites=sum(c['kind']=='literal_ddl_site' for c in calls),
        guarded_entrypoints=['legacy_connect','init_schema','_refresh_default_concept_views','_ensure_business_indexes'],
        unresolved='literal call inventory excludes tools/tests, dynamic SQL, aliases beyond direct imports and OS permissions; installed core path guard has no Git topology',
        all_writer_authority_closed=False)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',required=True,type=Path)
    parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--date',required=True)
    parser.add_argument('--skip-preview',action='store_true')
    parser.add_argument('--workspace-only',action='store_true',help='Project the shared context; do not generate the retired long-page preview')
    parser.add_argument('--sector-replay',action='store_true')
    parser.add_argument('--ths-replay',action='store_true')
    parser.add_argument('--as-of',help='Explicit local replay evaluation time; never inferred from latest input')
    parser.add_argument('--writer-inventory',action='store_true')
    args=parser.parse_args(); source=args.source.resolve(strict=True); output=args.output.resolve()
    if output.exists() or output in source.parents:
        raise ValueError('new isolated output directory required')
    before=source_files(); stat=source.stat(); output.mkdir(parents=True)
    report={'started_at':datetime.now(timezone.utc).isoformat(),'source_sha256':identity(before),
        'source_database':{'path':str(source),'bytes':stat.st_size,'mtime_ns':stat.st_mtime_ns},
        'execution_ready':False,'production_cutover':False}
    try:
        report['partition']=partition(source,output)
        print(json.dumps({'partition':report['partition']},ensure_ascii=False),flush=True)
        if args.sector_replay:
            report['sector_replay']=sector_replay(source,output,args.date)
        if args.ths_replay:
            report['ths_replay']=ths_replay(source,output,args.date)
        if args.writer_inventory:
            inventory=writer_inventory(Path(__file__).resolve().parents[2])
            (output/'writer-inventory.json').write_text(json.dumps(inventory,ensure_ascii=False,indent=2),encoding='utf-8')
            report['writer_inventory']={k:v for k,v in inventory.items() if k!='calls'}
        if not args.skip_preview:
            report['preview']=preview(source,output,args.date,args.as_of,workspace_only=args.workspace_only)
        report['source_unchanged']=source_files()==before and source.stat().st_mtime_ns==stat.st_mtime_ns and source.stat().st_size==stat.st_size
        report['passed']=report['source_unchanged']
    except Exception as exc:
        report.update(passed=False,error_type=type(exc).__name__,error=str(exc))
    report['artifacts']={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in output.iterdir() if p.is_file()}
    (output/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('passed','execution_ready','production_cutover')},ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
