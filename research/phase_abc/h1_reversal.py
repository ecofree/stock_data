r"""W1-H1：反转市分组验证。假设：20日动量在反转市（市场中位数ret_20d<=0）系统性失效。
方法：测试段(2026-06~08-21, 含qlib) + S4段(2026-04~06, 仅基线，按涨跌组分RankIC/Top10近似收益。
通过线：两组RankIC方向差异在两个窗口同时复现（up组>down组），否则毙掉H1。
输出 reports/h1_reversal.json。结论无论输赢封存，不调模型。
"""
from __future__ import annotations
import json
from pathlib import Path
import duckdb, pandas as pd, numpy as np

ROOT = Path("D:/accio/stock_data")
SNAP = ROOT / "research" / "phase_abc" / "snapshot"
REPORTS = ROOT / "research" / "phase_abc" / "reports"

def rank_ic(d, s):
    ics = []
    for _, g in d.groupby("signal_date"):
        if len(g) < 10: continue
        a, b = g[s].rank(method="average"), g.label_fwd_ret.rank(method="average")
        if a.std() == 0 or b.std() == 0: continue
        ics.append(float(a.corr(b)))
    ics = np.array(ics)
    return (float(np.nanmean(ics)) if len(ics) else None), len(ics)

def top_mean(d, s):
    rs = []
    for _, g in d[d.in_universe].groupby("signal_date"):
        top = g.nlargest(10, s)
        rs.append(float((top.close_t5 / top.open_t1 - 1).mean()))
    return (float(np.mean(rs)) if rs else None), len(rs)

def split_state(d):
    st = d.groupby("signal_date").ret_20d.median().rename("mkt_med20")
    d = d.join(st, on="signal_date")
    return d[d.mkt_med20 > 0], d[d.mkt_med20 <= 0]

def main():
    te = pd.read_parquet(SNAP / "test_pred.parquet")
    te["signal_date"] = pd.to_datetime(te["signal_date"]).dt.date
    out = {"hypothesis": "动量在反转市(mkt_med20<=0)失效", "windows": {}}
    up, down = split_state(te)
    w = {"up_days": up.signal_date.nunique(), "down_days": down.signal_date.nunique(), "groups": {}}
    for tag, sub in [("up", up), ("down", down)]:
        g = {}
        for s in ["simple_mom20", "ind_rel20", "qlib_lgbm"]:
            ic, n = rank_ic(sub, s); tm, nd = top_mean(sub, s)
            g[s] = {"rankic": ic, "days": n, "top10_mean": tm}
        w["groups"][tag] = g
    out["windows"]["FINAL_2026-06~08"] = w

    # S4复现（仅基线，快照重建，无模型训练）
    con = duckdb.connect()
    s4 = con.execute("""
    SELECT l.signal_date::DATE AS signal_date, l.code, l.open_t1, l.close_t5, l.ref_close,
           l.label_fwd_ret, l.industry, u.in_universe
    FROM read_parquet(?) l JOIN read_parquet(?) u ON l.signal_date=u.trade_date AND l.code=u.code
    WHERE l.label_status='mature' AND l.signal_date BETWEEN '2026-04-01' AND '2026-05-31'
    """, [(SNAP / "labels.parquet").as_posix(), (SNAP / "universe.parquet").as_posix()]).df()
    s4["signal_date"] = pd.to_datetime(s4["signal_date"]).dt.date
    s4 = s4.sort_values(["code", "signal_date"])
    s4["ret_20d"] = s4.groupby("code").ref_close.transform(lambda s: s / s.shift(20) - 1)
    s4 = s4.dropna(subset=["ret_20d", "label_fwd_ret"])
    s4["simple_mom20"] = s4.ret_20d
    s4["ind_med20"] = s4.groupby(["signal_date", "industry"]).ret_20d.transform("median")
    s4["ind_rel20"] = s4.ret_20d - s4.ind_med20
    up4, down4 = split_state(s4)
    w4 = {"up_days": up4.signal_date.nunique(), "down_days": down4.signal_date.nunique(), "groups": {}}
    for tag, sub in [("up", up4), ("down", down4)]:
        g = {}
        for s in ["simple_mom20", "ind_rel20"]:
            ic, n = rank_ic(sub, s); tm, nd = top_mean(sub, s)
            g[s] = {"rankic": ic, "days": n, "top10_mean": tm}
        w4["groups"][tag] = g
    out["windows"]["S4_2026-04~05"] = w4

    # 裁决：两组差异方向在两窗一致才算复现
    def gap(window, s):
        g = out["windows"][window]["groups"]
        a, b = g["up"][s]["rankic"], g["down"][s]["rankic"]
        return (a - b) if a is not None and b is not None else None
    verdict = {}
    for s in ["simple_mom20", "ind_rel20"]:
        g1, g2 = gap("FINAL_2026-06~08", s), gap("S4_2026-04~05", s)
        verdict[s] = {"gap_final": g1, "gap_s4": g2,
                      "replicated": bool(g1 is not None and g2 is not None and g1 > 0 and g2 > 0)}
    q = out["windows"]["FINAL_2026-06~08"]["groups"]
    qgap = (q["up"]["qlib_lgbm"]["rankic"] or 0) - (q["down"]["qlib_lgbm"]["rankic"] or 0)
    verdict["qlib_lgbm"] = {"gap_final": qgap, "note": "S4无qlib列，仅FINAL参考"}
    out["verdict"] = verdict
    out["sealed"] = "结论封存：无论输赢不调模型；复现才进入W3挑战者（反转/波动状态加权）"
    (REPORTS / "h1_reversal.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print("H1_DONE", json.dumps(verdict, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
