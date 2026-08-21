"""Parallel BaoStock fetcher with single-writer DuckDB commits."""
from __future__ import annotations
import argparse
from multiprocessing import get_context
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from base import DuckDBStore
from schema import init_schema

FIELDS = "date,open,high,low,close,volume,amount,turn,pctChg,peTTM,pbMRQ"
_BS = None

def _date(value):
    s=''.join(ch for ch in str(value) if ch.isdigit())
    if len(s)<8: raise ValueError(f'invalid date: {value}')
    return f'{s[:4]}-{s[4:6]}-{s[6:8]}'

def _market(code):
    c = ''.join(ch for ch in str(code) if ch.isdigit()).zfill(6)[-6:]
    return ("sh." if c.startswith(('6','9')) else "bj." if c.startswith(('4','8')) else "sz.") + c

def _num(v):
    try: return float(v) if v not in (None, '', '-') else None
    except (TypeError, ValueError): return None

def _worker_init():
    global _BS
    import baostock as bs
    login = bs.login()
    if getattr(login, 'error_code', '1') != '0': raise RuntimeError(getattr(login, 'error_msg', 'login failed'))
    _BS = bs

def _fetch(item):
    code, start, end = item
    try:
        rs = _BS.query_history_k_data_plus(_market(code), FIELDS, start_date=start, end_date=end, frequency='d', adjustflag='3')
        if getattr(rs, 'error_code', '1') != '0': return code, 'error', [], getattr(rs, 'error_msg', 'query failed')
        rows=[]
        while rs.next():
            v=rs.get_row_data()
            if v and v[0]: rows.append(v)
        return code, ('success' if rows else 'empty'), rows, '' if rows else 'empty BaoStock history response'
    except Exception as exc:
        return code, 'error', [], str(exc)[:500]

def _ensure(con):
    con.execute("""CREATE TABLE IF NOT EXISTS baostock_history_checkpoint (
      source VARCHAR NOT NULL,dataset VARCHAR NOT NULL,stock_code VARCHAR NOT NULL,
      start_date DATE NOT NULL,end_date DATE NOT NULL,status VARCHAR,rows_written INTEGER DEFAULT 0,
      last_error VARCHAR,updated_at TIMESTAMP DEFAULT current_timestamp,
      PRIMARY KEY(source,dataset,stock_code,start_date,end_date))""")

def run(db, start, end, offset, max_stocks, workers):
    start, end = _date(start), _date(end)
    store=DuckDBStore(str(db)); init_schema(store.conn); _ensure(store.conn)
    try:
        codes=[str(r[0]) for r in store.conn.execute("select distinct stock_code from tushare_stock_basic where stock_code is not null order by stock_code").fetchall()]
        selected=codes[max(0,offset):] if max_stocks<=0 else codes[max(0,offset):max(0,offset)+max_stocks]
        todo=[]
        for code in selected:
            row=store.conn.execute("select status from baostock_history_checkpoint where source='baostock' and dataset='daily' and stock_code=? and start_date=? and end_date=?",[code,start,end]).fetchone()
            if not row or row[0] not in ('success','empty'): todo.append(code)
        results=[]
        ctx=get_context('spawn')
        with ctx.Pool(processes=max(1,workers), initializer=_worker_init) as pool:
            for code,status,rows,error in pool.imap_unordered(_fetch,[(c,start,end) for c in todo],chunksize=1):
                daily=[]; basic=[]; ts=_market(code).upper()
                for r in rows:
                    daily.append((ts,code,r[0],_num(r[1]),_num(r[2]),_num(r[3]),_num(r[4]),(_num(r[5]) or 0)/10000,(_num(r[6]) or 0)/10000,_num(r[8])))
                    basic.append((ts,code,r[0],_num(r[7]),None,_num(r[9]),_num(r[10]),None,None))
                if daily:
                    store.conn.executemany("insert into tushare_daily(ts_code,stock_code,date,open,high,low,close,volume,turnover,change_pct) values (?,?,?,?,?,?,?,?,?,?) on conflict(date,ts_code) do nothing",daily)
                    store.conn.executemany("insert into tushare_daily_basic(ts_code,stock_code,date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv) values (?,?,?,?,?,?,?,?,?) on conflict(date,ts_code) do nothing",basic)
                for ds,n in [('daily',len(daily)),('daily_basic',len(basic))]:
                    store.conn.execute("insert into baostock_history_checkpoint values ('baostock',?,?,?,?,?,?,?,current_timestamp) on conflict(source,dataset,stock_code,start_date,end_date) do update set status=excluded.status,rows_written=excluded.rows_written,last_error=excluded.last_error,updated_at=excluded.updated_at",[ds,code,start,end,status,n,error])
                store.conn.commit(); results.append((code,status,n,error))
        return {'requested':len(selected),'todo':len(todo),'success':sum(x[1]=='success' for x in results),'empty':sum(x[1]=='empty' for x in results),'errors':sum(x[1]=='error' for x in results),'results':results}
    finally: store.close()

def main():
    root=Path(__file__).resolve().parents[1]; p=argparse.ArgumentParser(); p.add_argument('--db',default=str(root/'kpl_data.duckdb')); p.add_argument('--start-date',required=True); p.add_argument('--end-date',required=True); p.add_argument('--offset',type=int,default=0); p.add_argument('--max-stocks',type=int,default=40); p.add_argument('--workers',type=int,default=4); a=p.parse_args(); out=run(a.db,a.start_date,a.end_date,a.offset,a.max_stocks,a.workers); print(out); return 0 if out['errors']==0 else 1
if __name__=='__main__': raise SystemExit(main())
