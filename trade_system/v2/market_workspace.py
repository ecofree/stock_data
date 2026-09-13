"""Bounded read-only canonical market projection shared by daily publications.

Historical rows are evaluated as received now, not certified point-in-time data.
No raw fallback, source rewriting, portfolio advice or model ranking is allowed.
"""
from datetime import date, datetime
from pathlib import Path
import math

import duckdb

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
    return {'status':'source_conflict' if conflicts else 'available' if rows else 'source_missing',
        'rows':list(valid.values()),'conflicts':sorted(set(conflicts)),
        'ladder':[{'height':h,'count':n} for h,n in sorted(counts.items())],
        'scope':'canonical_limit_pool_not_exchange_total'}


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
    prices=con.execute("""SELECT trade_date,stock_code,change_pct,provider,adjustment,volume_unit,amount_unit,close
        FROM v_kline_daily WHERE trade_date IN (?,?) AND fetched_at<=? AND close>0
        ORDER BY trade_date,stock_code LIMIT 20001""",[day,prior or day,clock]).fetchall()
    if len(prices)>20000:raise ValueError('market price budget exceeded')
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
    snap=con.execute('SELECT max(trade_date) FROM v_default_concept_daily WHERE trade_date<=?',[day]).fetchone()[0]
    themes=[];stocks={c:{'stock_code':c,'stock_name':'','research_covered':c in research_codes,
        'change_pct':r['pct'],'close':r['close'],'price_contract':r['contract']} for c,r in current.items()}
    excluded=[];membership_status='missing';catalog=[]
    if snap is not None and 0<=(date.fromisoformat(day)-snap).days<=10:
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
        membership_status='complete' if len(themes)==len(catalog) and set(groups)==seen else 'partial'
    result={'schema':2,'scope':'read_only_market_review_not_execution','trade_date':day,'as_of':as_of,
        'breadth':breadth(r['pct'] for r in current.values()),'regime':'未评级（价格广度不替代情绪模型）',
        'breadth_scope':'available_canonical_price_rows_not_exchange_total','matched_previous':matched,
        'themes':themes,'stocks':stocks,'theme_scope':'all_quality_gated_declared_members_not_all_market_coverage',
        'membership_status':membership_status,'membership_date':str(snap) if snap else None,
        'membership_time_scope':'retained_quality_gated_snapshot_not_historical_arrival_certification',
        'source_groups':len(catalog),'excluded_broad_or_unknown_members':len(excluded),'excluded_membership':excluded,
        'account_state':'unknown','missing':['account_snapshot','verified_intraday_quotes','point_in_time_membership'],
        'execution_ready':False}
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
