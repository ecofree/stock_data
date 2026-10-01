"""Bounded read-only canonical market projection shared by daily publications.

Historical rows are evaluated as received now, not certified point-in-time data.
No raw fallback, source rewriting, portfolio advice or model ranking is allowed.
"""
from datetime import date, datetime
from pathlib import Path
import math

import duckdb
from trade_system.ths_quality import qualified_membership_snapshot, THS_MEMBERSHIP_MAX_AGE_DAYS

from .domain import identity


def latest_snapshot(source, as_of, research_codes=()):
    from zoneinfo import ZoneInfo
    clock=datetime.fromisoformat(as_of)
    if clock.tzinfo is None:raise ValueError('aware market clock required')
    local=clock.astimezone(ZoneInfo('Asia/Shanghai'))
    with duckdb.connect(str(Path(source).resolve(strict=True)),read_only=True) as con:
        con.execute('BEGIN TRANSACTION')
        today=local.date()
        flags=con.execute('SELECT exchange,is_open FROM tushare_trade_cal WHERE cal_date=? AND exchange IN (\'SSE\',\'SZSE\') ORDER BY exchange',[today]).fetchall()
        expected=[('SSE',0),('SZSE',0)]
        state='closed' if flags==expected else 'open_session' if flags==[('SSE',1),('SZSE',1)] else 'calendar_unknown'
        end=today.isoformat()
        rows=con.execute("SELECT max(cal_date) FROM tushare_trade_cal WHERE exchange='SSE' AND is_open=1 AND (cal_date<? OR (cal_date=? AND ?>=16))",[end,end,local.hour]).fetchone()
        if not rows[0]:raise ValueError('no certified closed market session')
        result=project(con,str(rows[0])[:10],as_of,set(research_codes))
        result.pop('snapshot_id')
        result['session_state']=state
        result['calendar_checked_date']=end
        return dict(result,snapshot_id=identity(result))


def limit_facts(con, day, clock):
    """Consume canonical source-qualified identities once; missing is not zero."""
    tables={r[0] for r in con.execute('SHOW TABLES').fetchall()}
    if 'v_limit_pool' not in tables:return {'status':'source_missing','rows':[],'ladder':[]}
    rows=con.execute('SELECT stock_code,stock_name,board_level,source,fetched_at FROM v_limit_pool WHERE trade_date=? AND fetched_at<=? ORDER BY stock_code LIMIT 2001',[day,clock]).fetchall()
    if len(rows)>2000:raise ValueError('limit pool budget exceeded')
    valid={};conflicts=[]
    for code,name,board,source,received in rows:
        code=str(code).split('.')[0]
        if code in valid or not board or board<1:
            conflicts.append(code);continue
        valid[code]={'stock_code':code,'stock_name':name,'board_level':int(board),
                     'source':source,'fetched_at':received.isoformat()}
    for code in conflicts:valid.pop(code,None)
    from collections import Counter
    counts=Counter(r['board_level'] for r in valid.values())
    complete_empty=False
    if not rows and 'history_fetch_checkpoint' in tables:
        columns={r[0] for r in con.execute('DESCRIBE history_fetch_checkpoint').fetchall()}
        if 'last_error' in columns:
            receipt=con.execute("SELECT rows_written,last_error FROM history_fetch_checkpoint WHERE "
                "dataset='hithink_limit_pool' AND trade_date=? AND page_no=0 AND status='success' AND updated_at<=?",
                [day,clock]).fetchone()
            if receipt and receipt[0]==0:
                import json,hashlib
                from zoneinfo import ZoneInfo
                try:
                    evidence=json.loads(receipt[1])
                    complete_empty=(evidence.get('path')=='/api/a-share/special-data/limit-up-pool'
                        and evidence.get('params',{}).get('date_ms')==int(datetime.fromisoformat(day).replace(tzinfo=ZoneInfo('Asia/Shanghai')).timestamp()*1000)
                        and type(evidence.get('total')) is int and evidence['total']==0
                        and type(evidence.get('pages')) is int and evidence['pages']>=1
                        and evidence.get('items_sha256')==hashlib.sha256(b'[]').hexdigest())
                except (ValueError,TypeError,AttributeError):
                    complete_empty=False
    return {'status':'source_conflict' if conflicts else 'available' if rows or complete_empty else 'source_missing',
        'complete_empty':complete_empty,
        'rows':list(valid.values()),'conflicts':sorted(set(conflicts)),
        'ladder':[{'height':h,'count':n} for h,n in sorted(counts.items())],
        'scope':'canonical_limit_pool_not_exchange_total'}


def theme_facts(con,day,current,research_codes,limits):
    boards={r['stock_code']:r['board_level'] for r in limits['rows']}
    snap, membership_age=qualified_membership_snapshot(con,day)
    themes=[];stocks={c:{'stock_code':c,'stock_name':'','research_covered':c in research_codes,
        'change_pct':r['pct'],'close':r['close'],'price_contract':r['contract']} for c,r in current.items()}
    excluded=[];membership_status='stale' if snap is not None else 'missing';catalog=[]
    if snap is not None and membership_age<=THS_MEMBERSHIP_MAX_AGE_DAYS:
        catalog=con.execute('SELECT concept_code,concept_name,stock_count FROM v_default_concept_daily WHERE trade_date=? ORDER BY concept_code LIMIT 10001',[snap]).fetchall()
        members=con.execute('SELECT concept_code,stock_code,stock_name FROM v_default_concept_stock_history WHERE trade_date=? ORDER BY concept_code,stock_code LIMIT 500001',[snap]).fetchall()
        if len(catalog)>10000 or len(members)>500000:raise ValueError('membership budget exceeded')
        groups={}
        for concept,code,name in members:
            code=str(code).split('.')[0]
            group=groups.setdefault(concept,[])
            if code in group:raise ValueError('duplicate theme member')
            group.append(code)
            stocks[code]={'stock_code':code,'stock_name':name,'research_covered':code in research_codes,
                'change_pct':current.get(code,{}).get('pct'),'close':current.get(code,{}).get('close'),
                'price_contract':current.get(code,{}).get('contract')}
        seen=set()
        for code,name,count in catalog:
            if code in seen:raise ValueError('duplicate theme identity')
            seen.add(code);actual=groups.get(code,[])
            valid=bool(count and len(actual)==count and all(len(c)==6 and c.isascii() and c.isdigit() for c in actual))
            if not valid:
                excluded.append({'concept_code':code,'reason':'declared_members_mismatch','expected':count,'actual':len(actual)})
                continue
            themes.append({'concept_code':code,'concept_name':name,'member_count':count,'member_codes':actual,
                'research_covered':sum(c in research_codes for c in actual),'broad_classification':count>800,
                'limit_up_count':sum(c in boards for c in actual) if limits['status']=='available' else None,
                'max_board':max((boards.get(c,0) for c in actual),default=0) if limits['status']=='available' else None,
                'limit_status':limits['status']})
        membership_status='complete' if catalog and len(themes)==len(catalog) and set(groups)==seen else 'partial'
    return {'themes':themes,'stocks':stocks,'membership_status':membership_status,
        'membership_date':str(snap) if snap else None,'membership_age_days':membership_age,
        'source_groups':len(catalog),'excluded_broad_or_unknown_members':len(excluded),'excluded_membership':excluded}


def collection_diagnostics(con, day, clock):
    """Inline product receipts, independent of the available-price projection.

    Receipt update times are diagnostic clocks, not refreshed source ages.
    Missing tables/denominators do not certify full-market readiness.
    """
    names={r[0] for r in con.execute('SHOW TABLES').fetchall()}
    products=[]
    if 'history_fetch_checkpoint' in names:
        columns={r[0] for r in con.execute('DESCRIBE history_fetch_checkpoint').fetchall()}
        if {'dataset','trade_date','status','rows_written','last_error','updated_at'} <= columns:
            for dataset,status,count,error,updated in con.execute(
                'SELECT dataset,status,sum(rows_written),max(last_error),max(updated_at) '
                'FROM history_fetch_checkpoint WHERE trade_date=? AND updated_at<=? '
                'GROUP BY dataset,status ORDER BY dataset,status LIMIT 101',[day,clock]).fetchall():
                products.append({'product':dataset,'status':status,'committed_upserts':count,
                    'count_scope':'upserts_not_unique_new_rows','reason':str(error or '')[:500],
                    'receipt_updated_at':updated.isoformat() if updated else None})
    taxonomies=[]
    if 'intraday_sector_flow_taxonomy' in names:
        for name,expected,observed,pct,status,error in con.execute(
            'SELECT taxonomy,expected_rows,fetched_rows,coverage_pct,status,last_error '
            'FROM intraday_sector_flow_taxonomy WHERE trade_date=? ORDER BY taxonomy',[day]).fetchall():
            taxonomies.append({'taxonomy':name,'expected_rows':expected if expected else None,
                'observed_rows':observed,'coverage_pct':pct if expected else None,
                'denominator_known':bool(expected),'status':status,'reason':str(error or '')[:500]})
    failed=any(p['status'] not in {'success','complete'} for p in products)
    failed=failed or any(t['status']!='success' or not t['denominator_known'] for t in taxonomies
                         if t['taxonomy'] in {'em_industry','ths_concept'})
    return {'status':'gaps_present' if failed else 'not_certified',
        'products':products,'taxonomies':taxonomies,'full_market_certified':False,
        'scope':'retained_receipts_not_independent_funds_or_observation_acceptance'}


def project(con, day, as_of, research_codes):
    clock=datetime.fromisoformat(as_of)
    if clock.tzinfo is not None:
        from zoneinfo import ZoneInfo
        clock=clock.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
    if date.fromisoformat(day)>clock.date():raise ValueError('future market date refused')
    sessions=[str(r[0])[:10] for r in con.execute(
        "SELECT DISTINCT cal_date FROM tushare_trade_cal WHERE exchange='SSE' AND is_open=1 AND cal_date<=? ORDER BY cal_date DESC LIMIT 2",[day]).fetchall()]
    if not sessions or sessions[0]!=day:raise ValueError('exact market session unavailable')
    prior=sessions[1] if len(sessions)>1 else None
    limits=limit_facts(con,day,clock)
    boards={r['stock_code']:r['board_level'] for r in limits['rows']}
    from trade_system.review_metrics import period_bounds, market_period_summary
    week_start,week_end=period_bounds(day,'week')
    quarter_start,_=period_bounds(day,'quarter')
    price_start=min(week_start,prior or day)
    prices=con.execute("""SELECT trade_date,stock_code,change_pct,provider,adjustment,volume_unit,amount_unit,close
        FROM v_kline_daily WHERE trade_date BETWEEN ? AND ? AND fetched_at<=? AND close>0
        ORDER BY trade_date,stock_code LIMIT 80001""",[price_start,day,clock]).fetchall()
    if len(prices)>80000:raise ValueError('market price budget exceeded')
    keyed={}
    for d,code,pct,provider,adjustment,vol,amount,close in prices:
        d=str(d)[:10];code=str(code)
        if (d,code) in keyed:raise ValueError('duplicate canonical market identity')
        keyed[d,code]={'pct':pct,'close':close,'contract':[provider,adjustment,vol,amount]}
    current={c:r for (d,c),r in keyed.items() if d==day and r['pct'] is not None and math.isfinite(r['pct'])}
    if not current:raise ValueError('no current canonical market prices; keep last publication')
    common=[c for c,r in current.items() if (prior,c) in keyed and keyed[prior,c]['pct'] is not None
            and math.isfinite(keyed[prior,c]['pct']) and r['contract']==keyed[prior,c]['contract']
            and all(r['contract'])]
    def breadth(items):
        values=list(items)
        return {'rise':sum(v>0 for v in values),'fall':sum(v<0 for v in values),'flat':sum(v==0 for v in values),'samples':len(values)}
    matched={'status':'matched' if common else 'unavailable','previous_date':prior,'samples':len(common),
        'scope':'same_stock_provider_adjustment_units_retrospective_not_PIT',
        'current':breadth(current[c]['pct'] for c in common),
        'previous':breadth(keyed[prior,c]['pct'] for c in common)}
    theme=theme_facts(con,day,current,research_codes,limits)
    themes=theme['themes']
    result={'schema':2,'scope':'read_only_market_review_not_execution','trade_date':day,'as_of':as_of,
        'breadth':breadth(r['pct'] for r in current.values()),'regime':'未评级（价格广度不替代情绪模型）',
        'breadth_scope':'available_canonical_price_rows_not_exchange_total','matched_previous':matched,
        'collection_diagnostics':collection_diagnostics(con,day,clock),
        **theme,'theme_scope':'all_quality_gated_declared_members_not_all_market_coverage',
        'membership_max_age_days':THS_MEMBERSHIP_MAX_AGE_DAYS,
        'membership_time_scope':'retained_quality_gated_snapshot_not_historical_arrival_certification',
        'account_state':'unknown','missing':['account_snapshot','verified_intraday_quotes','point_in_time_membership'],
        'execution_ready':False}
    calendar={}
    for exchange,opened,d in con.execute("""SELECT exchange,is_open,cal_date
        FROM tushare_trade_cal WHERE exchange IN ('SSE','SZSE')
        AND cal_date BETWEEN ? AND ? ORDER BY cal_date,exchange""",[min(week_start,quarter_start),day]).fetchall():
        calendar.setdefault(str(d)[:10],[]).append((exchange,opened))
    daily={}
    for (d,code),r in keyed.items():
        if d<week_start or r['pct'] is None or not math.isfinite(r['pct']) or not all(r['contract']):continue
        counts=daily.setdefault(d,{'rise':0,'fall':0,'flat':0,'samples':0})
        counts['samples']+=1;counts['rise' if r['pct']>0 else 'fall' if r['pct']<0 else 'flat']+=1
    result['periods']={period:market_period_summary(day,period,daily,calendar) for period in ('day','week')}
    # Period breadth is a bounded SQL aggregation, not an expanded full-market
    # Python row history or a substitute for returns/account performance.
    period_daily={}
    for d,total,distinct,rise,fall,flat in con.execute("""SELECT trade_date,count(*),count(DISTINCT stock_code),
        count(*) FILTER (WHERE change_pct>0),count(*) FILTER (WHERE change_pct<0),count(*) FILTER (WHERE change_pct=0)
        FROM v_kline_daily WHERE trade_date BETWEEN ? AND ? AND fetched_at<=? AND close>0
        AND isfinite(change_pct) AND coalesce(provider,'')<>'' AND coalesce(adjustment,'')<>''
        AND coalesce(volume_unit,'')<>'' AND coalesce(amount_unit,'')<>''
        GROUP BY trade_date ORDER BY trade_date LIMIT 94""",[quarter_start,day,clock]).fetchall():
        if total!=distinct:raise ValueError('duplicate canonical period identity')
        period_daily[str(d)[:10]]={'samples':total,'rise':rise,'fall':fall,'flat':flat}
    result['periods'].update({period:market_period_summary(day,period,period_daily,calendar)
        for period in ('month','quarter')})
    weekly=[]
    for session in result['periods']['week']['expected_sessions']:
        values={c:r for (d,c),r in keyed.items() if d==session and r['pct'] is not None and math.isfinite(r['pct'])}
        pool=limits if session==day else limit_facts(con,session,clock)
        item=theme if session==day else theme_facts(con,session,values,research_codes,pool)
        flows={}
        if 'multi_source_stock_flow' in {r[0] for r in con.execute('SHOW TABLES').fetchall()}:
            from trade_system.concept_flow import qualified_concept_review
            flows={r['sector_code']:r for r in qualified_concept_review(con,session,now=min(clock,datetime.fromisoformat(session+'T18:00:00')))['rows']}
        rows=[]
        for row in item['themes']:
            members=row['member_codes'];valid=[values[c]['pct'] for c in members if c in values and all(values[c]['contract'])]
            rows.append({k:row[k] for k in ('concept_code','concept_name','member_count','limit_up_count','max_board')} |
                {'member_version':identity([item['membership_date'],members]),'member_set_id':identity(sorted(members)),
                 'priced_members':len(valid),
                 'mean_change_pct':sum(valid)/len(valid) if valid else None,
                 'main_flow_cny':flows.get(row['concept_code'],{}).get('main_net'),
                 'flow_status':'qualified' if row['concept_code'] in flows else 'source_missing',
                 'leaders':[r for r in pool['rows'] if r['stock_code'] in members]})
        weekly.append({'date':session,'membership_date':item['membership_date'],'membership_status':item['membership_status'],
                       'limit_status':pool['status'],'ladder':pool['ladder'],'rows':rows})
    result['periods']['week']['themes']=weekly
    result['periods']['week']['theme_scope']='per_session_retained_membership_versions_not_PIT_or_automatic_stage_labels'
    result['limit_pool']=limits
    result['breadth']['limit_up']=len(boards) if limits['status']=='available' else None
    result['highest_board']=max(boards.values(),default=None)
    if 'multi_source_stock_flow' in {r[0] for r in con.execute('SHOW TABLES').fetchall()}:
        from trade_system.concept_flow import qualified_concept_review,matched_concept_history
        evaluation=min(clock,datetime.fromisoformat(day+'T18:00:00'))
        flow=qualified_concept_review(con,day,now=evaluation)
        history=matched_concept_history(con,day,flow,now=evaluation,sessions=3)
        by_code={r['sector_code']:r for r in flow['rows']}
        for theme in themes:
            row=by_code.get(theme['concept_code'])
            theme['main_flow_cny']=row['main_net'] if row else None
            theme['flow_status']='qualified' if row else 'source_missing'
        result['concept_flow']={'evaluation_at':evaluation.isoformat(),
            'scope':'retrospective_same_day_18h_cutoff_not_current_flow_or_PIT',
            'contract':flow['contract'],'history':history,
            'rows':[{k:r[k] for k in ('sector_code','sector_name','main_net','amount_unit','raw')} for r in flow['rows']]}
    result['previous_limit_queue']=limit_facts(con,prior,clock) if prior else {'status':'source_missing','rows':[],'ladder':[]}
    result['previous_limit_queue']['evaluation_scope']='retained_previous_session_pool_not_predeclared_selection'
    for row in result['previous_limit_queue']['rows']:
        row['next_change_pct']=current.get(row['stock_code'],{}).get('pct')
    tables={r[0] for r in con.execute('SHOW TABLES').fetchall()}
    if 'v_market_daily' in tables:
        rows=con.execute('SELECT limit_up_count,limit_down_count,consecutive_count,market_daily_source,fetched_at FROM v_market_daily WHERE trade_date=? AND fetched_at<=?',[day,clock]).fetchall()
        result['market_totals']={'status':'available' if len(rows)==1 else 'source_conflict' if rows else 'source_missing'}
        if len(rows)==1:
            up,down,consecutive,source,received=rows[0]
            result['market_totals'].update(limit_up=up,limit_down=down,consecutive=consecutive,source=source,fetched_at=received.isoformat())
            # Totals and the identified pool are distinct products, never silently interchangeable.
            result['breadth']['limit_down']=down
    columns={r[0] for r in con.execute('DESCRIBE v_kline_daily').fetchall()}
    if 'turnover' in columns:
        sums=con.execute('SELECT amount_unit,sum(turnover),count(*) FROM v_kline_daily WHERE trade_date=? AND fetched_at<=? AND turnover>=0 GROUP BY amount_unit',[day,clock]).fetchall()
        known={'yuan','CNY','thousand_yuan'}
        valid=bool(sums) and all(unit in known for unit,_,_ in sums)
        # v_kline_daily already normalizes amounts. Legacy live view revisions
        # retained the raw unit label after normalization; never convert twice.
        result['turnover']={'status':'available' if valid else 'source_missing',
            'cny':sum(total for _,total,_ in sums) if valid else None,
            'product':'v_kline_daily.normalized_turnover_cny','source_unit_labels':[u for u,_,_ in sums],
            'legacy_unit_label_mismatch':any(u not in ('yuan','CNY') for u,_,_ in sums),
            'samples':sum(n for _,_,n in sums),'scope':'available_canonical_rows_not_exchange_total'}
    return dict(result,snapshot_id=identity(result))


def snapshot(source,day,as_of,research_codes):
    source=Path(source).resolve(strict=True)
    with duckdb.connect(str(source),read_only=True) as con:
        con.execute('BEGIN TRANSACTION')
        return project(con,day,as_of,set(research_codes))
