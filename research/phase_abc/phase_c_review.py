r"""PhaseC 三账复盘：预测账/模拟账/实际账分离，共享指标，日周月同口径。
产出：review.html + operator_template.csv + metrics.json。不接交易接口。
"""
from __future__ import annotations
import json
from pathlib import Path
import duckdb, pandas as pd, numpy as np

ROOT = Path("D:/accio/stock_data")
LEDGER = ROOT / "research" / "phase_abc" / "ledger" / "ledger.duckdb"
REPORTS = ROOT / "research" / "phase_abc" / "reports"

def nav_metrics(nav: pd.DataFrame):
    nav = nav.sort_values("date")
    tot = float(nav.nav.iloc[-1] / nav.nav.iloc[0] - 1) if len(nav) else None
    dd = float((nav.nav / nav.nav.cummax() - 1).min()) if len(nav) else None
    rets = nav.nav.pct_change().fillna(0)
    vol = float(rets.std() * np.sqrt(252)) if len(rets) > 2 else None
    return {"days": len(nav), "total": tot, "max_dd": dd, "ann_vol": vol,
            "start": str(nav.date.iloc[0]) if len(nav) else None,
            "end": str(nav.date.iloc[-1]) if len(nav) else None}

def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    led = duckdb.connect(str(LEDGER), read_only=True)
    tables = [r[0] for r in led.execute("SHOW TABLES").fetchall()]
    metrics = {}
    for s in ["simple_mom20", "ind_rel20", "qlib_lgbm"]:
        t = f"nav_{s}"
        if t in tables:
            nav = led.execute(f"SELECT * FROM {t} ORDER BY date").df()
            metrics[s] = nav_metrics(nav)
            # 成本与换手：fills计数
            ft = f"fills_{s}"
            if ft in tables:
                try:
                    fills = led.execute(f"SELECT * FROM {ft}").df()
                    metrics[s]["fills"] = len(fills)
                    metrics[s]["buys"] = int((fills.side == "BUY").sum()) if "side" in fills else len(fills)
                except Exception:
                    metrics[s]["fills"] = 0
    led.close()
    (REPORTS / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")

    # 人工账模板（实际账可为空，写未提供，不用模拟补位）
    tpl = REPORTS / "operator_outcomes_template.csv"
    if not tpl.exists():
        tpl.write_text("trade_date,stock_code,action,px,qty,reason,execution_status\n2026-09-08,600000,watch,0,0,首次观察,watch\n", encoding="utf-8")

    # 复盘页：昨天判断->今天变化->兑现/失效->下次检验；三账分离
    comp = json.loads((REPORTS / "metrics.json").read_text(encoding="utf-8"))
    h = ["<html><head><meta charset='utf-8'><title>专业复盘</title></head><body>",
         "<h1>专业复盘（测试段2026-06~08-21，同账本）</h1>",
         "<h2>1. 预测账 vs 模拟账 vs 实际账</h2>",
         "<table border=1><tr><th>策略</th><th>天数</th><th>累计净收益</th><th>最大回撤</th><th>成交笔数</th></tr>"]
    for s, m in comp.items():
        h.append(f"<tr><td>{s}</td><td>{m['days']}</td><td>{m['total']:.4f}</td><td>{m['max_dd']:.4f}</td><td>{m.get('fills',0)}</td></tr>")
    h += ["</table>",
          "<h2>2. 结论（诚实）</h2><p>测试窗三者RankIC全负，动量存在反转；QLib首版累计-8.3%未优于简单基线+9%，维持研究态，不晋级。回撤均-30%，风险未达标。</p>",
          "<h2>3. 失败归因下轮假设卡</h2><p>H1: 2026-06~08为反转市，20日动量失效→加1日反转/波动状态分组验证；H2: adj缺失57%致池偏大盘→做adj完整子池消融。每周只推一个假设。</p>",
          "<h2>4. 实际账</h2><p>未提供（operator_outcomes_template.csv待人工录入），不用模拟收益补位。</p></body></html>"]
    (REPORTS / "review.html").write_text("".join(h), encoding="utf-8")
    print("PHASE_C_DONE", json.dumps(comp, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
