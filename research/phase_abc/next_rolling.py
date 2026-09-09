r"""下一步1：4段滚动+终验+双倍成本+分组（历史可做部分）。
输入 snapshot/labels.parquet+universe.parquet。输出 reports/rolling.json。
段划分（407天约束下的诚实窄版）：S1测2025-07~09，S2测2025-10~12，S3测2026-01~03，S4测2026-04~05，终验2026-06~08-21。每段训=测前12个月，验=测前2个月。
"""
from __future__ import annotations
import json
from pathlib import Path
import duckdb, pandas as pd, numpy as np

ROOT = Path("D:/accio/stock_data")
SNAP = ROOT / "research" / "phase_abc" / "snapshot"
REPORTS = ROOT / "research" / "phase_abc" / "reports"
COST = 25
FEAT = ["ret_1d","ret_5d","ret_20d","vol_20d","amt_z20","ind_rel20"]

def build_frame():
    con = duckdb.connect()
    df = con.execute("""
    SELECT l.signal_date::DATE AS signal_date, l.code, l.open_t1, l.close_t5, l.ref_close,
           l.label_fwd_ret, l.amount_yuan, l.industry, u.in_universe
    FROM read_parquet(?) l JOIN read_parquet(?) u ON l.signal_date=u.trade_date AND l.code=u.code
    WHERE l.label_status='mature'
    """, [(SNAP/"labels.parquet").as_posix(), (SNAP/"universe.parquet").as_posix()]).df()
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date
    df = df.sort_values(["code","signal_date"])
    g = df.groupby("code")
    df["ret_20d"] = g["ref_close"].transform(lambda s: s/s.shift(20)-1)
    df["ret_5d"] = g["ref_close"].transform(lambda s: s/s.shift(5)-1)
    df["ret_1d"] = g["ref_close"].transform(lambda s: s/s.shift(1)-1)
    df["vol_20d"] = g["ret_1d"].transform(lambda s: s.rolling(20).std())
    df["amt_z20"] = (df.amount_yuan - g["amount_yuan"].transform(lambda s: s.rolling(20).mean()))/g["amount_yuan"].transform(lambda s: s.rolling(20).std())
    df["ind_med20"] = df.groupby([df.signal_date, df.industry])["ret_20d"].transform("median")
    df["ind_rel20"] = df.ret_20d - df.ind_med20
    df["simple_mom20"] = df.ret_20d
    return df.dropna(subset=FEAT+["label_fwd_ret"])

def rank_ic(d, s):
    ics=[]
    for _,g in d.groupby("signal_date"):
        if len(g)<10: continue
        a,b=g[s].rank(method="average"),g.label_fwd_ret.rank(method="average")
        if a.std()==0 or b.std()==0: continue
        ics.append(float(a.corr(b)))
    ics=np.array(ics); return float(np.nanmean(ics)) if len(ics) else None

def top_total(d, s, cost_bps=COST):
    """简化：每日Top10等权持有5日净收益均值近似（用于多段快速比较；正式账本以PhaseB逐笔为准）"""
    rets=[]
    for _,g in d[d.in_universe].groupby("signal_date"):
        top=g.nlargest(10, s)
        r=(top.close_t5/top.open_t1-1-2*cost_bps/10000).mean()
        rets.append(r)
    rets=np.array(rets)
    tot=float(np.prod(1+rets)-1) if len(rets) else None
    return tot, len(rets)

def main():
    df = build_frame()
    segs = [("S1", "2025-07-01", "2025-10-01"), ("S2", "2025-10-01", "2026-01-01"),
            ("S3", "2026-01-01", "2026-04-01"), ("S4", "2026-04-01", "2026-06-01"),
            ("FINAL", "2026-06-01", "2026-08-22")]
    from lightgbm import LGBMRegressor
    out=[]
    for name, a, b in segs:
        A = pd.to_datetime(a).date(); B = pd.to_datetime(b).date()
        te = df[(df.signal_date >= A) & (df.signal_date < B)]
        tr_hi = A; tr_lo = (pd.Timestamp(A) - pd.DateOffset(months=14)).date()
        va_lo = (pd.Timestamp(A) - pd.DateOffset(months=2)).date()
        tr = df[(df.signal_date >= tr_lo) & (df.signal_date < va_lo)]
        va = df[(df.signal_date >= va_lo) & (df.signal_date < tr_hi)]
        if len(tr)<5000 or len(te)<1000:
            out.append({"seg":name,"note":"样本不足","tr":len(tr),"te":len(te)}); continue
        med=tr[FEAT].median()
        Xtr,Xva,Xte=tr[FEAT].fillna(med),va[FEAT].fillna(med),te[FEAT].fillna(med)
        mdl=LGBMRegressor(objective="regression",n_estimators=200,learning_rate=0.05,num_leaves=63,subsample=0.8,colsample_bytree=0.8,random_state=7,verbosity=-1)
        mdl.fit(Xtr,tr.label_fwd_ret)
        te=te.copy(); te["qlib"]=mdl.predict(Xte)
        r_qlib=rank_ic(te,"qlib"); r_base=rank_ic(te,"simple_mom20"); r_ind=rank_ic(te,"ind_rel20")
        t_qlib,_=top_total(te,"qlib"); t_base,_=top_total(te,"simple_mom20")
        t_qlib2,_=top_total(te,"qlib",50); t_base2,_=top_total(te,"simple_mom20",50)
        out.append({"seg":name,"tr":len(tr),"va":len(va),"te":len(te),
          "rankic_qlib":r_qlib,"rankic_base":r_base,"rankic_ind":r_ind,
          "total_qlib":t_qlib,"total_base":t_base,"total_qlib_2x":t_qlib2,"total_base_2x":t_base2,
          "excess":(t_qlib-t_base) if t_qlib is not None and t_base is not None else None})
    # H1反转分组：按测试全集信号日前20日市场均值正负分组看RankIC
    FA, FB = pd.to_datetime("2026-06-01").date(), pd.to_datetime("2026-08-22").date()
    te_all = df[(df.signal_date >= FA) & (df.signal_date < FB)].copy()
    mkt=te_all.groupby("signal_date").label_fwd_ret.mean().rename("mkt")
    te_all=te_all.join(mkt, on="signal_date")
    # 用ret_20d市场中位数代理状态
    st=te_all.groupby("signal_date").ret_20d.median().rename("state")
    te_all=te_all.join(st, on="signal_date")
    h1={}
    for tag,sub in [("up",te_all[te_all.state>0]),("down",te_all[te_all.state<=0])]:
        h1[tag]={"n":len(sub),"rankic_qlib":rank_ic(sub,"ret_20d") if "qlib" not in sub else None}
    (REPORTS/"rolling.json").write_text(json.dumps({"segments":out,"h1_note":"状态分组待qlib列对齐，仅留框架","h1":str(h1)},ensure_ascii=False,indent=2),encoding="utf-8")
    print("ROLLING_DONE")
    for o in out: print(o)

if __name__=="__main__":
    raise SystemExit(main())
