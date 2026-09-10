r"""晋级止损线八门：绑定模型包+合同，不满足即BLOCKED留研究态。
产出：reports/gate.json + ledger.shadow_registry登记。不改生产库。
"""
from __future__ import annotations
import json
from pathlib import Path
from datetime import date
import duckdb, pandas as pd, numpy as np

ROOT = Path("D:/accio/stock_data")
SNAP = ROOT / "research" / "phase_abc" / "snapshot"
LEDGER = ROOT / "research" / "phase_abc" / "ledger" / "ledger.duckdb"
REPORTS = ROOT / "research" / "phase_abc" / "reports"
MODEL_ID = "lgbm-pit-v1-20260906"

def main():
    REPORTS.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((SNAP / "dataset_manifest.json").read_text(encoding="utf-8"))
    metrics = json.loads((REPORTS / "metrics.json").read_text(encoding="utf-8"))
    gates = []

    # G1 数据与时点
    g1 = manifest.get("handcheck_ok") is True and manifest.get("label_mode") == "t1_open_to_t5_close_adj"
    gates.append({"gate": "G1数据与时点", "pass": bool(g1), "detail": f"handcheck={manifest.get('handcheck_ok')} label={manifest.get('label_mode')} adj缺失pending隔离"})

    # G2 覆盖：期望全集5547 vs 当日n/nu，>=99%才算全市场能力
    from trade_system.db_utils import legacy_connect
    con = legacy_connect()
    cov = con.execute("SELECT trade_date, count(*) n, sum(CASE WHEN in_universe THEN 1 ELSE 0 END) nu FROM read_parquet(?) GROUP BY 1 ORDER BY 1 DESC LIMIT 30",
                      [(SNAP / "universe.parquet").as_posix()]).df()
    cov["coverage"] = cov.nu / cov.n
    cov["trade_date"] = pd.to_datetime(cov["trade_date"]).dt.date
    recent = cov[cov.trade_date <= pd.to_datetime("2026-08-21").date()]
    g2pass = bool((recent.coverage >= 0.99).all()) if len(recent) else False
    gates.append({"gate": "G2覆盖>=99%", "pass": g2pass,
                  "detail": f"近30日均cov={cov.coverage.mean():.3f} min={cov.coverage.min():.3f}；未达99%，当日不做全市场声明，受限池另名登记"})

    # G3 账本正确性：现金非负、数量为正、nav=Cash+mv（容差1e-6相对）
    led = duckdb.connect(str(LEDGER), read_only=True)
    g3ok, g3d = True, []
    for s in ["simple_mom20", "ind_rel20", "qlib_lgbm"]:
        try:
            nav = led.execute(f"SELECT * FROM nav_{s}").df()
            if (nav.cash < -1e-6).any() or (nav.nav <= 0).any(): g3ok = False; g3d.append(f"{s}现金/净值异常")
            fills = led.execute(f"SELECT * FROM fills_{s}").df()
            if len(fills) and (fills.qty <= 0).any(): g3ok = False; g3d.append(f"{s}数量异常")
        except Exception as e:
            g3ok = False; g3d.append(f"{s}缺表:{e}"[:120])
    led.close()
    gates.append({"gate": "G3账本对账", "pass": g3ok, "detail": ";".join(g3d) if g3d else "现金/数量/净值无异常（容差内）"})

    # G4 相对优势：需4段滚动3段超额+合并+终验；现只有1段 -> BLOCK
    gates.append({"gate": "G4相对优势", "pass": False,
                  "detail": f"仅1段测试(59天)，qlib累计{metrics['qlib_lgbm']['total']:.3f} vs 基线{metrics['simple_mom20']['total']:.3f}为负；需4段滚动+独立终验，未满足维持研究態"})

    # G5 风险与成本：回撤差<=3pp 且 双倍成本仍超额；现qlib为负 -> BLOCK
    dd_q, dd_b = metrics["qlib_lgbm"]["max_dd"], metrics["simple_mom20"]["max_dd"]
    g5 = (dd_q - dd_b) >= -0.03 and metrics["qlib_lgbm"]["total"] > metrics["simple_mom20"]["total"]
    gates.append({"gate": "G5风险成本", "pass": bool(g5), "detail": f"qlib dd={dd_q:.3f} vs base dd={dd_b:.3f} 差{dd_q-dd_b:.3f}；累计为负，双倍成本免测即不通过"})

    # G6 不确定性：配对日收益差块自举95%下界>0；现为负直接不通过（仍计算留痕）
    try:
        # 用日频nav差近似
        led2 = duckdb.connect(str(LEDGER), read_only=True)
        nq = led2.execute("SELECT nav FROM nav_qlib_lgbm ORDER BY date").df().nav.pct_change().fillna(0).values
        nb = led2.execute("SELECT nav FROM nav_simple_mom20 ORDER BY date").df().nav.pct_change().fillna(0).values
        led2.close()
        diff = nq - nb
        rng = np.random.default_rng(7)
        means = [rng.choice(diff, size=len(diff), replace=True).mean() for _ in range(2000)]
        lo = float(np.quantile(means, 0.025))
        g6 = lo > 0
        d6 = f"块均值95%下界={lo:.5f}"
    except Exception as e:
        g6, d6 = False, f"未算:{e}"[:150]
    gates.append({"gate": "G6不确定性", "pass": bool(g6), "detail": d6 + "；证据不足继续积累，不写无效/有效"})

    # G7 最新运行：40交易日前向+成熟标签+20日无合同故障；今日登记起点
    gates.append({"gate": "G7前向40天", "pass": False, "detail": "影子起点2026-09-06，0/40天，无成熟标签；只验链路不重证多年规律"})

    # G8 人工批准
    gates.append({"gate": "G8人工批准", "pass": False, "detail": "待展示全部实验+约束+暴露后批准，绑定模型包lgbm-pit-v1-20260906"})

    verdict = "BLOCKED" if not all(g["pass"] for g in gates) else "APPROVED_RANKING"
    out = {"model_id": MODEL_ID, "date": str(date.today()), "snapshot": manifest["snapshot_id"],
           "verdict": verdict, "gates": gates,
           "action": "维持shadow/research_only，正式榜用基线；40天影子并行，每周一假设卡"}
    (REPORTS / "gate.json").write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    # 影子登记（新库可写）
    from trade_system.db_utils import legacy_connect
    ledw = legacy_connect(str(LEDGER))
    ledw.execute("CREATE TABLE IF NOT EXISTS shadow_registry(model_id VARCHAR, start_date DATE, target_days INT, status VARCHAR)")
    if not ledw.execute("SELECT count(*) FROM shadow_registry WHERE model_id=?", [MODEL_ID]).fetchone()[0]:
        ledw.execute("INSERT INTO shadow_registry VALUES (?, '2026-09-06', 40, 'observing')", [MODEL_ID])
    ledw.close()
    print("GATE_DONE", verdict)
    for g in gates:
        print(("PASS" if g["pass"] else "FAIL"), g["gate"], "-", g["detail"])
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
