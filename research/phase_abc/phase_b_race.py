r"""PhaseB 公平赛：同快照同池同账本。
基线：simple_mom20（20日可比收益） vs ind_rel20（个股-行业中位数）。
挑战者：LightGBM（PIT特征，只训练窗拟合中位数/均值，推理按(signal_date,code)对齐）。
账本：Top分位等权，每日最多新开2只、单批次20%NAV、10只上限，成本25bps双边，未成交留现金。
产出：ledger.duckdb表 + research_comparison.html + daily_research.html。
运行：.\.venv-qlib\Scripts\python.exe research\phase_abc\phase_b_race.py
"""
from __future__ import annotations
import json, logging
from pathlib import Path
import duckdb
import pandas as pd
import numpy as np

ROOT = Path("D:/accio/stock_data")
SNAP = ROOT / "research" / "phase_abc" / "snapshot"
LEDGER = ROOT / "research" / "phase_abc" / "ledger" / "ledger.duckdb"
REPORTS = ROOT / "research" / "phase_abc" / "reports"
COST_BPS = 25

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("phase_b")

def rank_ic(df, score, label):
    ics = []
    for d, g in df.groupby("signal_date"):
        if len(g) < 10: continue
        r1 = g[score].rank(method="average"); r2 = g[label].rank(method="average")
        if r1.std() == 0 or r2.std() == 0: continue
        ics.append(float(r1.corr(r2, method="pearson")))
    ics = np.array(ics)
    return float(np.nanmean(ics)) if len(ics) else None, len(ics)

def simulate(df, score_col, tag, initial=1_000_000.0, cost_bps=COST_BPS):
    """日频简化账本：每日信号取in_universe按分排序Top10意图，每日最多新开2只，每只目标10%NAV，持有5交易日，到期按close_t5结算，成本双边cost_bps。无法成交=label非mature则跳过留现金。"""
    d = df[df.in_universe].copy().sort_values(["signal_date", score_col], ascending=[True, False])
    d["signal_date"] = pd.to_datetime(d["signal_date"]).dt.date
    d["date_t5"] = pd.to_datetime(d["date_t5"]).dt.date
    dates = sorted(d.signal_date.unique())
    cash = initial; positions = []  # (code, qty, buy_px, sell_date, buy_date)
    navs = []; fills = []; intents = []
    px = df.set_index(["signal_date", "code"])[["open_t1", "close_t5"]].to_dict("index")
    # 为简化，按信号日分组顺序推进；持有到期自动卖
    from datetime import timedelta
    for sd in dates:
        # 到期卖出（sell_date<=sd）
        for p in [p for p in positions if p["sell_date"] <= sd]:
            key = (p["buy_date"], p["code"])
            # 卖出价用买入信号行存的close_t5
            sell_px = p["sell_px"]; qty = p["qty"]
            proceeds = sell_px * qty * (1 - cost_bps / 10000)
            cash += proceeds
            fills.append({"date": str(sd), "code": p["code"], "side": "SELL", "px": sell_px, "qty": qty})
            positions.remove(p)
        # 新开：Top按序取2
        cands = d[d.signal_date == sd].head(10)
        opened = 0
        held = {p["code"] for p in positions}
        for _, r in cands.iterrows():
            if opened >= 2 or len(positions) >= 10: break
            if r.code in held: continue
            if pd.isna(r.open_t1) or pd.isna(r.close_t5): continue  # 未成交留现金，不补赢家
            target = (cash + sum(p["qty"] * p["sell_px"] for p in positions)) * 0.10
            buy_px = float(r.open_t1) * (1 + cost_bps / 10000)
            qty = int(target // buy_px)
            if qty <= 0 or cash < qty * buy_px: continue
            cash -= qty * buy_px
            # sell_date = 该信号后第5个交易日 = date_t5（标签合同已按交易日历）
            positions.append({"code": r.code, "qty": qty, "sell_px": float(r.close_t5),
                              "sell_date": r.date_t5, "buy_date": sd})
            fills.append({"date": str(sd), "code": r.code, "side": "BUY", "px": float(r.open_t1), "qty": qty})
            intents.append({"signal_date": str(sd), "code": r.code, "score": float(r[score_col])})
            opened += 1
        mv = sum(p["qty"] * p["sell_px"] for p in positions)
        navs.append({"date": str(sd), "cash": cash, "mv": mv, "nav": cash + mv, "strategy": tag})
    nav = pd.DataFrame(navs)
    if len(nav):
        nav["ret"] = nav.nav.pct_change().fillna(0)
        tot = nav.nav.iloc[-1] / initial - 1
        dd = float((nav.nav / nav.nav.cummax() - 1).min())
    else:
        tot, dd = None, None
    return nav, pd.DataFrame(fills), pd.DataFrame(intents), tot, dd

def main(cost_bps: float = COST_BPS, suffix: str = "") -> int:
    REPORTS.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    df = con.execute("""
    SELECT l.signal_date::DATE AS signal_date, l.code, l.ref_close, l.open_t1, l.close_t5,
           l.date_t1, l.date_t5, l.label_fwd_ret, l.amount_yuan, l.avg_amt_20d, l.industry,
           u.in_universe
    FROM read_parquet(?) l JOIN read_parquet(?) u
      ON l.signal_date=u.trade_date AND l.code=u.code
    WHERE l.label_status='mature'
    """, [(SNAP / "labels.parquet").as_posix(), (SNAP / "universe.parquet").as_posix()]).df()
    df["signal_date"] = pd.to_datetime(df["signal_date"]).dt.date
    log.info("rows=%d mature=%d uni=%d", len(df), len(df), df.in_universe.sum())

    m = df.copy()
    m = m.sort_values(["code", "signal_date"])
    g = m.groupby("code")
    m["ret_20d"] = g["ref_close"].transform(lambda s: s / s.shift(20) - 1)
    m["ret_5d"] = g["ref_close"].transform(lambda s: s / s.shift(5) - 1)
    m["ret_1d"] = g["ref_close"].transform(lambda s: s / s.shift(1) - 1)
    m["vol_20d"] = g["ret_1d"].transform(lambda s: s.rolling(20).std())
    m["amt_z20"] = (m["amount_yuan"] - g["amount_yuan"].transform(lambda s: s.rolling(20).mean())) / g["amount_yuan"].transform(lambda s: s.rolling(20).std())
    m["ind_med20"] = m.groupby(["signal_date", "industry"])["ret_20d"].transform("median")
    m["simple_mom20"] = m["ret_20d"]
    m["ind_rel20"] = m["ret_20d"] - m["ind_med20"]
    feat = ["ret_1d", "ret_5d", "ret_20d", "vol_20d", "amt_z20", "ind_rel20"]
    m = m.dropna(subset=feat + ["label_fwd_ret"])

    # 时序切分：训2025-01~2026-03，验2026-04~05，测2026-06~08-21（留末5日未成熟）
    tr = m[m.signal_date < pd.to_datetime("2026-04-01").date()]
    va = m[(m.signal_date >= pd.to_datetime("2026-04-01").date()) & (m.signal_date < pd.to_datetime("2026-06-01").date())]
    te = m[(m.signal_date >= pd.to_datetime("2026-06-01").date()) & (m.signal_date <= pd.to_datetime("2026-08-21").date())]
    log.info("split tr=%d va=%d te=%d", len(tr), len(va), len(te))
    medians = tr[feat].median()
    for d in (tr, va, te):
        d[feat] = d[feat].fillna(medians)

    from lightgbm import LGBMRegressor
    model = LGBMRegressor(objective="regression", n_estimators=300, learning_rate=0.05,
                          num_leaves=63, subsample=0.8, colsample_bytree=0.8, random_state=7, verbosity=-1)
    model.fit(tr[feat], tr["label_fwd_ret"], eval_set=[(va[feat], va["label_fwd_ret"])],
              callbacks=[])
    te = te.copy(); te["qlib_lgbm"] = model.predict(te[feat])
    tr["qlib_lgbm"] = model.predict(tr[feat]); va["qlib_lgbm"] = model.predict(va[feat])
    full = pd.concat([tr, va, te])

    # 信号评估（测试段，同池）
    rows = []
    for name in ["simple_mom20", "ind_rel20", "qlib_lgbm"]:
        ic, n = rank_ic(te[te.in_universe], name, "label_fwd_ret")
        rows.append({"strategy": name, "test_rankic": ic, "test_days": n, "test_n": len(te)})
        log.info("%s RankIC=%.4f days=%d", name, ic if ic else float("nan"), n)

    # 组合账本（测试段）
    navs = {}
    for name in ["simple_mom20", "ind_rel20", "qlib_lgbm"]:
        nav, fills, intents, tot, dd = simulate(te, name, name, cost_bps=cost_bps)
        navs[name] = (nav, fills, intents, tot, dd)
        log.info("%s total=%.4f dd=%.4f trades=%d", name, tot if tot else 0, dd if dd else 0, len(fills))

    # 写ledger.duckdb（新库；非默认成本用独立表名，不覆盖主账本）
    led = duckdb.connect(str(LEDGER))
    rdf = pd.DataFrame(rows)
    rdf["cost_bps"] = cost_bps
    led.execute(f"CREATE OR REPLACE TABLE strategy_test{suffix} AS SELECT * FROM rdf")
    for name, (nav, fills, intents, tot, dd) in navs.items():
        led.execute(f"CREATE OR REPLACE TABLE nav{suffix}_{name} AS SELECT * FROM nav")
        led.execute(f"CREATE OR REPLACE TABLE fills{suffix}_{name} AS SELECT * FROM fills" if len(fills) else f"CREATE OR REPLACE TABLE fills{suffix}_{name} AS SELECT 1 AS empty WHERE 0")
    te.to_parquet(SNAP / "test_pred.parquet", index=False)
    led.close()

    # 双报告（非默认成本只写metrics文件，不覆盖主报告）
    if suffix:
        (REPORTS / f"metrics{suffix}.json").write_text(
            json.dumps({n: {"total": t, "max_dd": d, "trades": len(f)}
                        for n, (_, f, _, t, d) in navs.items()},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        print("PHASE_B_DONE", suffix, f"cost={cost_bps}",
              json.dumps({n: round(t or 0, 4) for n, (_, _, _, t, _) in navs.items()}, ensure_ascii=False))
        return 0
    comp = [f"<html><head><meta charset='utf-8'><title>公平赛</title></head><body><h1>公平赛 base-2025-20260904-r1（测试2026-06~08-21，同池同账本，成本{cost_bps}bps）</h1><table border=1><tr><th>策略</th><th>RankIC</th><th>天数</th><th>累计净收益</th><th>最大回撤</th></tr>"]
    for r in rows:
        _, _, _, tot, dd = navs[r["strategy"]]
        comp.append(f"<tr><td>{r['strategy']}</td><td>{r['test_rankic']:.4f}</td><td>{r['test_days']}</td><td>{tot:.4f}</td><td>{dd:.4f}</td></tr>")
    comp.append("</table><p>口径：T收冻/T+1开买/T+5收卖，未成交留现金，10只/10%/日开2只。M1/M4因资金概念短历史，只做短窗共同赛，不进主表。</p></body></html>")
    (REPORTS / "research_comparison.html").write_text("".join(comp), encoding="utf-8")

    last_day = sorted(te.signal_date.unique())[-1]
    top = te[te.signal_date == last_day].sort_values("qlib_lgbm", ascending=False).head(5)
    cards = [f"<html><head><meta charset='utf-8'><title>今日研究</title></head><body><h1>研究清单 {last_day}（LGBM未晋级，仅研究观察）</h1>"]
    for _, r in top.iterrows():
        cards.append(f"<h3>{r.code} 分数{r.qlib_lgbm:.4f} 5日标签mature</h3><p>反证：样本外RankIC见公平赛；成交看20日均额{r.avg_amt_20d:.0f}；失效条件：双倍成本转负/行业暴露集中即降级。</p>")
    cards.append("</body></html>")
    (REPORTS / "daily_research.html").write_text("".join(cards), encoding="utf-8")
    print("PHASE_B_DONE", json.dumps(rows, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cost-bps", type=float, default=COST_BPS)
    ap.add_argument("--suffix", default="")
    a = ap.parse_args()
    raise SystemExit(main(cost_bps=a.cost_bps, suffix=a.suffix))
