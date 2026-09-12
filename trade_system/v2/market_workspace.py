"""Bounded read-only canonical market projection shared by daily publications.

Historical rows are evaluated as received now, not certified point-in-time data.
No raw fallback, source rewriting, portfolio advice or model ranking is allowed.
"""
from datetime import date, datetime
from pathlib import Path
import math

import duckdb

from .domain import identity


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
    prices=con.execute("""SELECT trade_date,stock_code,change_pct,provider,adjustment,volume_unit,amount_unit
        FROM v_kline_daily WHERE trade_date IN (?,?) AND fetched_at<=? AND close>0
        ORDER BY trade_date,stock_code LIMIT 20001""",[day,prior or day,clock]).fetchall()
    if len(prices)>20000:raise ValueError('market price budget exceeded')
    keyed={}
    for d,code,pct,provider,adjustment,vol,amount in prices:
        d=str(d)[:10];code=str(code)
        if (d,code) in keyed:raise ValueError('duplicate canonical market identity')
        keyed[d,code]={'pct':pct,'contract':[provider,adjustment,vol,amount]}
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
    themes=[];stocks={};excluded=[];membership_status='missing';catalog=[]
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
                'change_pct':current.get(code,{}).get('pct')}
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
                'limit_up_count':None,'max_board':None})
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
    return dict(result,snapshot_id=identity(result))


def snapshot(source,day,as_of,research_codes):
    source=Path(source).resolve(strict=True)
    with duckdb.connect(str(source),read_only=True) as con:
        con.execute('BEGIN TRANSACTION')
        return project(con,day,as_of,set(research_codes))
