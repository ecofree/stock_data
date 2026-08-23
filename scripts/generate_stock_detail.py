"""Generate a single-stock detail page aggregating all available data.

Usage:
  python scripts/generate_stock_detail.py --code 000001
  python scripts/generate_stock_detail.py --code 000001 --days 60

Aggregates: daily kline chart, concept membership, flow trend, LHB history,
limit-up appearances, holdings position.  Written to reports/stocks/{code}.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.logging_setup import configure  # noqa: E402


def _esc(v) -> str:
    import html as _h
    return _h.escape(str(v)) if v is not None else "—"


def build(con, code: str, days: int = 120) -> str:
    kline = con.execute(
        """SELECT CAST(trade_date AS VARCHAR), open, high, low, close,
                  change_pct, volume
           FROM v_kline_daily WHERE ktype='D' AND stock_code=?
             AND CAST(trade_date AS DATE) >= current_date - ?
           ORDER BY trade_date""",
        [code, days]).fetchall()

    concepts = con.execute(
        """SELECT DISTINCT h.concept_name FROM ths_concept_stock_history h
           WHERE h.stock_code=? AND h.date_verified
             AND CAST(h.trade_date AS DATE) >= current_date - INTERVAL 7 DAY
           ORDER BY 1 LIMIT 20""", [code]).fetchall()

    lhb = con.execute(
        """SELECT CAST(trade_date AS VARCHAR), reason,
                  buy_amount, sell_amount
           FROM v_lhb_review_evidence
           WHERE stock_code=? ORDER BY trade_date DESC LIMIT 10""",
        [code]).fetchall()

    limit_hits = con.execute(
        """SELECT CAST(trade_date AS VARCHAR), continue_day_cnt, limit_up_reason,
                  limit_up_time
           FROM official_limit_pool WHERE stock_code=?
           ORDER BY trade_date DESC LIMIT 15""", [code]).fetchall()

    holdings_row = con.execute(
        """SELECT entry_date, entry_price, shares, stop_loss_price, status
           FROM holdings WHERE stock_code=? AND status='open' LIMIT 1""",
        [code]).fetchone() if con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name='holdings'"
    ).fetchone()[0] else None

    name = (con.execute(
        "SELECT stock_name FROM official_limit_pool WHERE stock_code=? "
        "ORDER BY trade_date DESC LIMIT 1", [code]).fetchone()
        or con.execute(
            """SELECT DISTINCT concept_name FROM ths_concept_stock_history
               WHERE stock_code=? LIMIT 1""", [code]).fetchone())
    stock_name = name[0] if name and name[0] else code

    # --- kline spark data ---
    dates_js = json.dumps([r[0] for r in kline])
    closes_js = json.dumps([r[4] for r in kline])
    vol_js = json.dumps([r[6] or 0 for r in kline])

    concept_tags = " ".join(
        f"<span class='tag'>{_esc(c[0])}</span>" for c in concepts)
    lhb_rows = "".join(
        f"<tr><td>{_esc(d)}</td><td>{_esc(r)}</td>"
        f"<td class='num'>{_esc(b)}</td><td class='num'>{_esc(s)}</td></tr>"
        for d, r, b, s in lhb)
    limit_rows = "".join(
        f"<tr><td>{_esc(d)}</td><td>{_esc(b)}板</td><td>{_esc(reason or '—')}</td>"
        f"<td class='mono'>{_esc(t or '—')}</td></tr>"
        for d, b, reason, t in limit_hits)
    hold_html = ""
    if holdings_row:
        ed, ep, sh, sl, st = holdings_row
        last_close = kline[-1][4] if kline else ep
        pnl = round((last_close / ep - 1) * 100, 2) if ep and last_close else 0
        color = "#ff5b6a" if pnl > 0 else "#2ebd85"
        hold_html = (
            f"<div class='panel'><h3>持仓</h3>"
            f"<p>入场 {ep} × {sh} 股 ({ed}) · 现价 {last_close} · "
            f"<b style='color:{color}'>{pnl}%</b> · 止损 {sl or '—'}</p></div>")
    else:
        hold_html = "<div class='panel dim'>无持仓</div>"

    return f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(stock_name)} {code}</title>
<style>
body{{font-family:'Segoe UI','Microsoft YaHei',sans-serif;background:#0a0f18;
color:#e7edf6;margin:0;padding:20px}}
.wrap{{max-width:1400px;margin:0 auto}}
h1{{font-size:22px}} .tag{{display:inline-block;background:#1b2740;color:#7fb2ff;
padding:3px 10px;border-radius:12px;font-size:12px;margin-right:4px}}
.panel{{background:#101a2a;border:1px solid #1b2740;border-radius:8px;padding:16px;
margin-top:14px}}
.panel h3{{color:#4cc3ff;font-size:13px;margin-bottom:10px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th,td{{padding:4px 8px;border-bottom:1px solid #1b2740;text-align:left}}
.num,.mono{{font-family:monospace}}
.dim{{color:#55647f}} .back{{color:#7fb2ff;text-decoration:none;font-size:13px}}
</style></head><body><div class="wrap">
<a class="back" href="../daily_review_latest.html">← 返回复盘</a>
<h1>{_esc(stock_name)} <span class="mono dim">{code}</span></h1>
<p>{concept_tags or '<span class="dim">无概念标签</span>'}</p>
{hold_html}
<div class="panel"><h3>K线（近{len(kline)}日）</h3>
<div id="kchart" style="height:400px"></div></div>
<div class="panel"><h3>涨停记录</h3>
{('<table><thead><tr><th>日期</th><th>连板</th><th>原因</th><th>封板时间</th>'
 '</tr></thead><tbody>' + limit_rows + '</tbody></table>') if limit_rows else '<p class="dim">近15次无涨停</p>'}
</div>
<div class="panel"><h3>龙虎榜（近10次）</h3>
{(lhb_rows and '<table><thead><tr><th>日期</th><th>原因</th><th>买入</th><th>卖出</th>'
 '</tr></thead><tbody>' + lhb_rows + '</tbody></table>') or '<p class="dim">近期未上榜</p>'}
</div>
<script src="../trade_system/vendor/echarts.min.js"></script>
<script>
var c = echarts.init(document.getElementById('kchart'));
c.setOption({{
  tooltip: {{trigger:'axis'}},
  grid: {{left:60,right:40,top:30,bottom:50}},
  xAxis: {{type:'category', data:{dates_js}}},
  yAxis: [{{type:'value', scale:true}}, {{type:'volume', gridIndex:0, show:false}}],
  series: [
    {{type:'candlestick', data: [], itemStyle:{{color:'#ff5b6a',color0:'#2ebd85'}},
      lineStyle:{{width:0}}}},
    {{type:'line', data:{closes_js}, showSymbol:false, lineStyle:{{width:2,color:'#4cc3ff'}}}},
    {{type:'bar', data:{vol_js}, yAxisIndex:1, itemStyle:{{opacity:.3}}}}
  ]
}});
window.addEventListener('resize', () => c.resize());
</script>
</div></body></html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--code", required=True)
    parser.add_argument("--days", type=int, default=120)
    parser.add_argument("--out-dir", default=str(PROJECT_ROOT / "reports" / "stocks"))
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db, read_only=True)
    try:
        page = build(con, args.code, args.days)
    finally:
        con.close()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{args.code}.html"
    out.write_text(page, encoding="utf-8")
    print(f"stock detail: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())


import json  # noqa: E402  (used inside build via dates_js/closes_js)
