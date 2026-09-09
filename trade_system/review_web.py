"""Detailed post-market review page renderer (HTML + ECharts).

Consumes the same context as the markdown daily review
(``trade_system.daily_review.build_daily_review_context``) plus one page-facts
payload from ``trade_system.review_facts``.  The renderer is presentation-only:
it renders market emotion, breadth, limit-up ecology, theme mainline,
capital-flow ranks, the candidate funnel, plan execution and data-quality
gates without issuing page-specific database queries.

The page is a self-contained static HTML file (vendored ECharts, no CDN), so
it can be opened offline or served from any static host.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from trade_system.daily_review import (
    build_daily_review_context,
    build_review_narrative,
    _latest_date,
)
from trade_system.review_facts import build_review_page_facts
from trade_system.review_metrics import COMPOSITE_SCORE_STATUS
from trade_system.i18n_labels import (
    CATEGORY_CN,
    PROVIDER_CN,
    SELECTION_STATUS_CN,
    SEVERITY_CN,
    SETUP_TYPE_CN,
    PLAN_STATUS_CN,
    EXECUTION_STATUS_CN,
    WATCHLIST_STATUS_CN,
    cn,
    zh_text,
)


# ---------------------------------------------------------------------------
# HTML building helpers
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# HTML building helpers
# ---------------------------------------------------------------------------

def _e(v: Any) -> str:
    return html.escape(str(v), quote=True)


def _slk(code: str) -> str:
    """Wrap a stock code as a link only when the target artifact exists."""
    if not code:
        return "—"
    c = str(code).strip()
    detail_path = Path(__file__).resolve().parents[1] / "reports" / "stocks" / f"{c}.html"
    if not detail_path.is_file():
        return _e(c)
    return (f"<a class='mono' href='stocks/{c}.html' "
            f"style='color:inherit;text-decoration:none'>{_e(c)}</a>")


def _fmt(v: Any, default: str = "—") -> str:
    if v is None:
        return default
    if isinstance(v, float):
        if abs(v) >= 1e8:
            return f"{v / 1e8:.2f}亿"
        if abs(v) >= 1e4:
            return f"{v / 1e4:.0f}万"
        return f"{v:.2f}"
    return _e(v)


def _pct(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{float(v):.2f}%"
    except (TypeError, ValueError):
        return _e(v)


def _num(v: Any) -> str:
    if v is None:
        return "—"
    try:
        return f"{int(round(float(v))):,}"
    except (TypeError, ValueError):
        return _e(v)


def _sign_class(v: Any) -> str:
    try:
        return "up" if float(v) > 0 else "down" if float(v) < 0 else "flat"
    except (TypeError, ValueError):
        return "flat"


def _status_pill(status: Any) -> str:
    cls = {
        "success": "ok", "success_with_optional_gap": "ok",
        "ready": "ok", "completed": "ok", "true": "ok",
        "partial": "warn", "degraded": "warn", "stale": "warn",
        "blocked": "bad", "error": "bad", "false": "bad", "missing": "bad",
    }.get(str(status).lower(), "warn")
    return f"<span class='pill {cls}'>{_e(status)}</span>"


def _fmt_clock(v: Any) -> str:
    """Turn unix seconds / leftover raw stamps into HH:MM."""
    if v in (None, ""):
        return "封板时间未记录"
    text = str(v).strip()
    if text.isdigit() and len(text) >= 10:
        try:
            return datetime.fromtimestamp(int(text[:10])).strftime("%H:%M")
        except (OSError, OverflowError, ValueError):
            return text
    return text


def _ratio(rise: Any, fall: Any) -> str:
    try:
        r, f = float(rise), float(fall)
        if f > 0:
            return f"{r / f:.2f}"
    except (TypeError, ValueError):
        pass
    return "—"


# ---------------------------------------------------------------------------
# CSS
# ---------------------------------------------------------------------------

_CSS = r"""
:root{
  --bg:#14110c; --paper:#1c1812; --ink:#f3ead8; --muted:#a89880; --dim:#6e6354;
  --line:#3a3226; --rule:#c4a574; --up:#d64545; --down:#2f8f6b; --warn:#d4a017;
  --ok:#2f8f6b; --panel:#211c15;
  --serif:"Songti SC","STSong","Noto Serif SC","Source Han Serif SC","SimSun",serif;
  --sans:-apple-system,"SF Pro Text","PingFang SC","Microsoft YaHei","Noto Sans SC",sans-serif;
  --mono:"Bahnschrift","DIN Alternate",ui-monospace,"Cascadia Code",Consolas,monospace;
}
*{margin:0;padding:0;box-sizing:border-box}
html{color-scheme:dark; scroll-behavior:smooth}
body{
  background:var(--bg); color:var(--ink); font-family:var(--sans); font-size:14px;
  line-height:1.7; min-height:100vh;
  background-image:linear-gradient(180deg,rgba(196,165,116,.04),transparent 220px);
}
.mono{font-family:var(--mono); font-variant-numeric:tabular-nums}
.up{color:var(--up)} .down{color:var(--down)} .amber{color:var(--warn)} .flat{color:var(--muted)}
.dim{color:var(--muted); font-size:12px}

.masthead{position:sticky; top:0; z-index:50; display:flex; align-items:center; justify-content:space-between;
  gap:16px; padding:10px 28px; background:rgba(20,17,12,.92); backdrop-filter:blur(12px);
  border-bottom:1px solid var(--line); flex-wrap:wrap}
.mast-mark{font-family:var(--serif); font-size:20px; letter-spacing:.12em}
.mast-date{font-family:var(--mono); font-size:12px; color:var(--muted); display:flex; gap:10px; align-items:center}
.toc{display:flex; gap:18px; flex-wrap:wrap}
.toc a{color:var(--muted); text-decoration:none; font-size:12px; letter-spacing:.08em; border-bottom:1px solid transparent}
.toc a:hover{color:var(--ink); border-bottom-color:var(--rule)}

.wrap{max-width:1180px; margin:0 auto; padding:28px 28px 72px}
.grid{display:grid; gap:16px}
.g-2{grid-template-columns:repeat(auto-fit,minmax(380px,1fr))}
.g-main{grid-template-columns:minmax(0,1fr) 320px}
.flow-block{min-width:0}
@media (max-width:1100px){.g-main{grid-template-columns:1fr}}
@media (max-width:768px){
  .wrap{padding:12px}
  .coreband{grid-template-columns:repeat(3,1fr)}
  .grid2{grid-template-columns:1fr}
  .table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
  table{min-width:640px}
  .pcard{flex-direction:column;padding:14px}
  .pc-right{grid-template-columns:1fr}
  .topnav{gap:8px;flex-wrap:wrap;padding:10px 12px}
  .tlb-head,.tlb-row{grid-template-columns:100px repeat(10,1fr)}
  .cb-cell{min-width:22px}
  h1{font-size:18px}
}

section, .ledger{padding:22px 0; border-top:1px solid var(--line); scroll-margin-top:64px}
.sec-title{display:flex; align-items:baseline; justify-content:space-between; gap:12px; margin-bottom:14px}
.sec-title strong{font-family:var(--serif); font-size:22px; font-weight:600; letter-spacing:.04em}
.sec-kicker{font-size:11px; letter-spacing:.16em; color:var(--dim); text-transform:uppercase}

.verdict{padding:8px 0 28px; border-top:none}
.verdict-kicker{display:flex; gap:10px; align-items:center; color:var(--dim); font-size:11px; letter-spacing:.14em}
.stance{font-family:var(--mono); font-size:11px; letter-spacing:.12em; padding:2px 0;
  border-bottom:1px solid currentColor}
.stance.blocked{color:var(--up)} .stance.observe{color:var(--warn)} .stance.ready{color:var(--ok)}
.verdict h1{font-family:var(--serif); font-weight:600; font-size:clamp(28px,4vw,44px);
  line-height:1.25; letter-spacing:.02em; margin:12px 0 10px; text-wrap:pretty}
.verdict .lede{color:var(--muted); max-width:46em; font-size:15px; line-height:1.8}
.verdict-meta{display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:0;
  margin-top:22px; border-top:1px solid var(--line); border-bottom:1px solid var(--line)}
.verdict-meta div{padding:12px 14px 12px 0}
.verdict-meta div + div{border-left:1px solid var(--line); padding-left:16px}
.vm-label{font-size:11px; color:var(--dim); letter-spacing:.1em}
.vm-value{font-family:var(--serif); font-size:20px; margin-top:4px}
.story{margin:18px 0 0; display:grid; gap:8px}
.story li{list-style:none; padding-left:14px; position:relative; color:var(--ink); line-height:1.75}
.story li::before{content:""; position:absolute; left:0; top:.7em; width:6px; height:1px; background:var(--rule)}

.ticker{display:grid; grid-template-columns:repeat(6,minmax(0,1fr)); gap:0;
  border-top:1px solid var(--line); border-bottom:1px solid var(--line)}
.kpi{padding:12px 14px 12px 0}
.kpi + .kpi{border-left:1px solid var(--line); padding-left:16px}
.kpi .k-label{font-size:11px; color:var(--dim); letter-spacing:.1em}
.kpi .k-value{font-size:22px; font-weight:600; font-family:var(--mono); margin-top:4px}
.kpi .k-sub{font-size:11px; color:var(--muted); margin-top:2px}
.kpi.hot .k-value{color:var(--up)} .kpi.cold .k-value{color:var(--down)} .kpi.warn .k-value{color:var(--warn)}
.kpi.bad .k-value{color:var(--up)}

.chart{width:100%; min-height:240px}
.chart-sm{min-height:190px}
.chart-lg{min-height:280px}

table{width:100%; border-collapse:collapse; font-size:13px}
th,td{padding:7px 8px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap}
th{color:var(--dim); font-weight:600; font-size:11px; letter-spacing:.08em}
tbody tr:hover{background:rgba(196,165,116,.05)}
td.num,th.num{text-align:right; font-family:var(--mono)}
td.mono{font-family:var(--mono)}
.table-scroll{overflow-x:auto}
.empty{color:var(--dim); padding:18px 0; font-size:13px}

.pill{display:inline-block; font-size:11px; letter-spacing:.06em; border-bottom:1px solid currentColor; padding:0}
.pill.ok{color:var(--ok)} .pill.warn{color:var(--warn)} .pill.bad{color:var(--up)}
.alert{padding:10px 0; font-size:13px; border-bottom:1px solid var(--line)}
.alert.warn{color:var(--warn)} .alert.bad{color:var(--up)}

.flowline{display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:0; border-top:1px solid var(--line)}
.flowline .node{padding:12px 12px 12px 0}
.flowline .node + .node{border-left:1px solid var(--line); padding-left:16px}
.flowline .node .l{font-size:11px; color:var(--dim); letter-spacing:.08em}
.flowline .node .n{font-size:28px; font-family:var(--mono); font-weight:600; margin-top:4px}
.flowline .node.hot .n{color:var(--up)}
.flowline .arrow{display:none}

.detail-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:0}
.detail-item{padding:10px 16px 10px 0; border-bottom:1px solid var(--line)}
.detail-item .d-label{font-size:11px; color:var(--dim); letter-spacing:.08em}
.detail-item .d-value{font-size:14px; margin-top:3px}
.detail-item .d-value small{color:var(--muted); font-size:11px}

.tabs{display:flex; gap:18px; margin-bottom:12px; border-bottom:1px solid var(--line)}
.tabs button{appearance:none; background:none; border:0; color:var(--muted); font:inherit;
  padding:0 0 8px; cursor:pointer; letter-spacing:.08em; font-size:13px; border-bottom:1px solid transparent}
.tabs button.active{color:var(--ink); border-bottom-color:var(--rule)}
.tab-panel{display:none} .tab-panel.active{display:block}

.tomorrow ol{display:grid; gap:10px; padding-left:0}
.tomorrow li{list-style:none; display:grid; grid-template-columns:28px 1fr; gap:10px; align-items:start}
.tomorrow .n{font-family:var(--mono); color:var(--rule); font-size:13px; padding-top:2px}

.appendix{border-top:1px solid var(--line); padding-top:16px; margin-top:8px}
.appendix > summary{cursor:pointer; font-family:var(--serif); font-size:20px; list-style:none; display:flex;
  justify-content:space-between; align-items:baseline}
.appendix > summary::-webkit-details-marker{display:none}
.appendix > summary span{font-family:var(--sans); font-size:12px; color:var(--dim); letter-spacing:.08em}
.appendix[open] > summary{margin-bottom:16px}

.notice{color:var(--muted); font-size:13px; line-height:1.7; max-width:50em}
.metric-note{margin:0 0 10px; padding:8px 10px; border-left:2px solid var(--warn);
  color:var(--muted); font-size:11px; line-height:1.6; background:rgba(212,160,23,.05)}
 .concept-review{display:grid; grid-template-columns:minmax(280px,320px) minmax(0,1fr); gap:24px; min-height:320px; align-items:stretch}
 .concept-list{display:flex; flex-direction:column; gap:0; max-height:480px; overflow:auto}
.concept-tab{display:grid; grid-template-columns:28px minmax(0,1fr) auto; gap:8px; align-items:center;
  width:100%; text-align:left; color:var(--ink); background:transparent; border:0;
  border-bottom:1px solid var(--line); padding:10px 4px; cursor:pointer}
 .concept-tab:hover,.concept-tab.active{color:var(--rule)}
 .concept-more{width:100%; margin-top:8px; padding:8px 4px; border:1px dashed var(--line); background:transparent; color:var(--rule); cursor:pointer; font-size:12px}
.concept-tab .concept-rank{font:600 12px var(--mono); color:var(--dim)}
.concept-tab .concept-name{font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.concept-tab .concept-meta{grid-column:2 / 4; color:var(--muted); font-size:11px}
.concept-tab .concept-lu{font:600 14px var(--mono); color:var(--up)}
.concept-detail{min-width:0}
.concept-detail-head{display:flex; align-items:flex-start; justify-content:space-between; gap:12px; margin-bottom:12px}
.concept-detail-title{font-family:var(--serif); font-size:26px; font-weight:600}
.concept-detail-sub{color:var(--muted); font-size:12px; margin-top:4px}
 .concept-stock-grid{display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px 14px; max-height:360px; overflow:auto; width:100%; padding:2px 2px 2px 0; box-sizing:border-box}
 .concept-stock-card{border:1px solid var(--line); border-radius:8px; min-width:0; min-height:68px; box-sizing:border-box; padding:11px 12px; display:grid; grid-template-columns:minmax(0,1fr) auto; gap:4px 10px; background:var(--paper)}
 .concept-stock-card .stock-name{font-weight:600; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
 .concept-stock-card .stock-code{color:var(--muted); font:12px var(--mono)}
 .concept-stock-card .stock-board{color:var(--warn); font:600 13px var(--mono); grid-column:2; grid-row:1 / 3; align-self:center}
 .concept-stock-card .stock-time{color:var(--dim); font:11px var(--mono)}
.concept-footnote{margin-top:10px; color:var(--dim); font-size:11px}
.trail-source-note{margin:8px 0 12px; color:var(--muted); font-size:11px; line-height:1.6}
 .trail-window-note{margin:-4px 0 12px; color:var(--rule); font:11px var(--mono)}
 .trail-scroll{display:flex; gap:14px; overflow-x:auto; padding:2px 2px 10px 0; width:100%; box-sizing:border-box}
.day-card{flex:0 0 400px; min-width:400px; border-top:1px solid var(--line); display:flex; flex-direction:column}
.day-card.wide{flex-basis:400px; min-width:400px}
.day-title{font-family:var(--serif); font-size:16px; padding:8px 0 10px; letter-spacing:.04em}
.day-title small{display:block; font-family:var(--sans); font-size:11px; color:var(--dim); letter-spacing:0; margin-top:2px}
.day-rows{max-height:420px; overflow:auto}
.day-head,.day-row{display:grid; grid-template-columns:22px minmax(0,1fr) 36px 56px; gap:6px; align-items:center}
 .day-head.cols5,.day-row.cols5{grid-template-columns:28px minmax(0,1fr) 52px 68px 72px}
 .day-head.cols5.stock-detail-head,.day-row.cols5.stock-detail-row{grid-template-columns:28px minmax(0,1fr) 54px 70px 88px; gap:7px}
 .stock-detail-row{padding:9px 10px; border:1px solid var(--line); border-radius:7px; margin-top:6px; background:var(--paper)}
 .stock-detail-head span{min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
 .stock-detail-row .n{min-width:0; display:flex; align-items:baseline; gap:7px}
 .stock-detail-row .n .dim{font-size:11px; font-family:var(--mono)}
 .stock-detail-row .num{white-space:nowrap}
 .stock-detail-row .num small{display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; color:var(--dim); font:10px var(--mono); margin-top:2px}
.trail-more{margin-top:6px; font-size:11px; color:var(--dim); cursor:pointer; background:none; border:0; padding:4px 0; font:inherit; color:var(--muted)}
.sort-head{cursor:pointer; user-select:none}
.sort-head:hover{color:var(--rule)}
.day-head{font-size:11px; color:var(--dim); letter-spacing:.08em; padding:4px 0; position:sticky; top:0; background:var(--bg)}
.day-row{width:100%; text-align:left; background:none; border:0; border-top:1px solid var(--line); color:inherit; padding:7px 0; cursor:pointer; font:inherit}
.day-row:hover,.day-row.active{color:var(--rule)}
.day-row .n{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
.trail-detail{margin-top:16px; border-top:1px solid var(--line); padding-top:12px}
.trail-spark{display:flex; gap:8px; flex-wrap:wrap; margin:8px 0 12px}
.trail-spark span{font-family:var(--mono); font-size:12px; color:var(--muted)}
.trail-spark .hot{color:var(--up)}
@media (max-width:900px){
  .verdict-meta,.ticker,.flowline{grid-template-columns:1fr 1fr}
  .verdict-meta div + div,.kpi + .kpi,.flowline .node + .node{border-left:0; padding-left:0}
   .concept-review{grid-template-columns:1fr}
   .concept-stock-grid{grid-template-columns:1fr}
   .concept-list{max-height:240px}
   .day-card,.day-card.wide{flex-basis:min(92vw,400px); min-width:min(92vw,400px)}
   .day-head.cols5.stock-detail-head,.day-row.cols5.stock-detail-row{grid-template-columns:26px minmax(0,1fr) 48px 60px 76px; gap:5px}
}
.footer{margin-top:36px; text-align:center; color:var(--dim); font-size:11px; letter-spacing:.08em}
.review-band{margin:18px 0 0; padding:0 18px; border:1px solid rgba(58,50,38,.72);
  border-radius:10px; background:rgba(28,24,18,.22)}
.band-label{padding:14px 0 0; color:var(--rule); font:11px var(--mono); letter-spacing:.14em}
.review-band > section{border-top:1px solid var(--line)}
.review-band > section:first-of-type{border-top:0}
@media (max-width:768px){.review-band{padding:0 10px}}
"""


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_kpis(ctx: dict[str, Any]) -> str:
    market = ctx.get("market_context", {})
    breadth = market.get("breadth") or []
    # breadth is a list of rows from different sources; the daily_summary row
    # carries rise_count/fall_count while the market_rise_fall row carries the
    # limit-up counts.  Pick the value from whichever row provides it.
    row = breadth[0] if breadth else {}
    limit_up = row.get("limit_up_count")
    limit_down = row.get("limit_down_count")
    rise = fall = None
    for item in breadth:
        if item.get("rise_count") is not None:
            rise = item.get("rise_count")
        if item.get("fall_count") is not None:
            fall = item.get("fall_count")
    blown_rate = None
    limit_summary = market.get("limit_summary") or []
    if limit_summary:
        blown_rate = limit_summary[0].get("blown_limit_up_rate")
    emotion_rows = ctx.get("emotion_rows") or []
    emotion = emotion_rows[0].get("cgl") if emotion_rows else None
    regime = ctx.get("regime") or {}
    regime_name = regime.get("regime_name") or regime.get("regime") or "—"
    suggested = regime.get("suggested_position_pct")
    execution_control = ctx.get("execution_control") or {}
    effective_position = execution_control.get("effective_position_pct")
    if effective_position is None:
        effective_position = suggested
    consecutive = ctx.get("consecutive")

    cards = [
        ("涨停 / 跌停", f"{_e(limit_up) if limit_up is not None else '—'} / {_e(limit_down) if limit_down is not None else '—'}",
         "涨跌停家数", "hot" if (limit_up or 0) > (limit_down or 0) * 4 else ""),
        ("上涨 / 下跌家数", f"{_num(rise)} / {_num(fall)}",
         f"涨跌比 {_ratio(rise, fall)}", "hot" if (rise or 0) > (fall or 0) else "cold"),
        ("炸板率", _pct(blown_rate), "炸板 / 涨停", "warn" if (blown_rate or 0) >= 25 else ""),
        ("情绪温度", _fmt(emotion), "赚钱效应 CGL", "hot" if (emotion or 0) >= 50 else "cold"),
        ("市场状态", _e(regime_name), f"有效仓位 {_pct(effective_position)} · 理论 {_pct(suggested)}", "bad" if execution_control.get("override") == "BLOCK" else ""),
        ("最高连板", _e(consecutive if consecutive is not None else "—"), "连板高度", ""),
    ]
    return "\n".join(
        f"<div class='kpi {cls}'><div class='k-label'>{_e(label)}</div>"
        f"<div class='k-value'>{value}</div><div class='k-sub'>{_e(sub)}</div></div>"
        for label, value, sub, cls in cards
    )


def _render_command_summary(ctx: dict[str, Any]) -> str:
    """Render the one-screen decision layer before detailed evidence."""
    story = ctx.get("narrative") or build_review_narrative(ctx)
    missing = story.get("missing") or []
    missing_text = ", ".join(missing[:4]) if missing else "无"
    bullets = "".join(f"<li>{_e(item)}</li>" for item in (story.get("bullets") or [])[:8])
    return (
        f"<section class='verdict' id='s-verdict' data-screen-label='verdict'>"
        f"<div class='verdict-kicker'><span class='stance { _e(story.get('stance') or 'observe') }'>"
        f"{_e(story.get('stance_label') or '观察核验')}</span>"
        f"<span>{_e(story.get('weekday') or '')} · {_e(story.get('trade_date') or '')}</span></div>"
        f"<h1>{_e(story.get('headline') or '复盘尚未形成结论')}</h1>"
        f"<p class='lede'>{_e(story.get('lede') or '')}</p>"
        f"<div class='verdict-meta'>"
        f"<div><div class='vm-label'>情绪阶段</div><div class='vm-value'>{_e(story.get('regime') or '—')}</div></div>"
        f"<div><div class='vm-label'>主线</div><div class='vm-value'>{_e(story.get('mainline') or '—')}</div></div>"
        f"<div><div class='vm-label'>有效仓位</div><div class='vm-value'>{_pct(story.get('effective_position_pct'))}</div></div>"
        f"<div><div class='vm-label'>缺失组</div><div class='vm-value' style='font-size:16px'>{_e(missing_text)}</div></div>"
        f"</div>"
        f"<ul class='story'>{bullets}</ul>"
        f"</section>"
    )


def _render_flow_compact(ctx: dict[str, Any]) -> str:
    """Front-of-page flow: confirmation only. Full tables stay in the appendix."""
    flow = ctx.get("capital_flow") or {}
    stock_meta = flow.get("stock_flow_meta") or {}
    sector_meta = flow.get("sector_flow_meta") or {}
    stock_cols = [
        ("rank", "排名", "num"), ("stock_code", "代码", "mono"),
        ("stock_name", "名称", ""), ("main_net", "主力净额", "num"),
        ("change_pct", "涨幅", "num"),
    ]
    sector_cols = [
        ("rank", "排名", "num"), ("sector_name", "名称", ""),
        ("main_net", "主力净额", "num"), ("change_pct", "涨幅", "num"),
    ]
    stock_persist_cols = [
        ("stock_code", "代码", "mono"),
        ("positive_days", "正流入天数", "num"),
        ("main_net_5d", "5日净流", "num"),
        ("twenty_day_main_net", "20日净流", "num"),
    ]
    sector_persist_cols = [
        ("sector_name", "名称", ""),
        ("positive_days", "正流入天数", "num"),
        ("main_net_5d", "5日净流", "num"),
        ("twenty_day_main_net", "20日净流", "num"),
    ]

    broad = {"融资融券", "沪股通", "深股通", "国企改革", "富时罗素", "标普道琼斯", "融资融券概念"}

    def ranked(key: str, limit: int, skip_names: bool = False) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in flow.get(key) or []:
            if skip_names and str(row.get("sector_name") or "") in broad:
                continue
            rows.append(row)
            if len(rows) >= limit:
                break
        return [{"rank": i + 1, **row} for i, row in enumerate(rows)]

    coverage = (
        f"个股 {stock_meta.get('codes') or 0} 只 · {stock_meta.get('batch_status') or '—'} · "
        f"板块 {sector_meta.get('codes') or 0} 个 · {sector_meta.get('taxonomy') or '—'}"
    )
    return (
        "<div class='tabs' data-tabs='flow'>"
        "<button type='button' class='active' data-tab='in'>流入确认</button>"
        "<button type='button' data-tab='out'>流出确认</button>"
        "<button type='button' data-tab='persist'>持续性</button>"
        "</div>"
        f"<p class='dim' style='margin-bottom:10px'>{_e(coverage)}</p>"
        "<div class='tab-panel active' data-panel='in'><div class='grid g-2'>"
        f"{_flow_table('个股净流入 Top 8', ranked('stock_inflow', 8), stock_cols)}"
        f"{_flow_table('概念净流入 Top 6', ranked('sector_inflow', 6, skip_names=True), sector_cols)}"
        "</div></div>"
        "<div class='tab-panel' data-panel='out'><div class='grid g-2'>"
        f"{_flow_table('个股净流出 Top 8', ranked('stock_outflow', 8), stock_cols)}"
        f"{_flow_table('概念净流出 Top 6', ranked('sector_outflow', 6, skip_names=True), sector_cols)}"
        "</div></div>"
        "<div class='tab-panel' data-panel='persist'><div class='grid g-2'>"
        f"{_flow_table('个股持续流入', ranked('stock_flow_persistence', 8), stock_persist_cols)}"
        f"{_flow_table('概念持续流入', ranked('sector_flow_persistence', 8), sector_persist_cols)}"
        "</div></div>"
    )


def _render_loop(ctx: dict[str, Any]) -> str:
    """Plans, imported outcomes, journal, watchlist — the operator closed loop."""
    plans = ctx.get("plans") or []
    outcomes = ctx.get("outcomes") or []
    journal = ctx.get("journal") or []
    watchlist = ctx.get("watchlist") or []
    picks = (ctx.get("capital_flow") or {}).get("candidate_picks") or []
    execution_ready = bool((ctx.get("execution_control") or {}).get("execution_ready"))
    plans_blocked = not execution_ready
    parts: list[str] = []

    if outcomes:
        rows = []
        for row in outcomes[:15]:
            rows.append(
                f"<tr><td>{_e(row.get('stock_name') or row.get('stock_code'))}</td>"
                f"<td>{_status_pill(zh_text(cn(EXECUTION_STATUS_CN, row.get('execution_status'))))}</td>"
                f"<td class='num'>{_pct(row.get('position_pct'))}</td>"
                f"<td class='num {_sign_class(row.get('net_return_pct'))}'>{_fmt(row.get('net_return_pct'))}</td>"
                f"<td>{_e(zh_text(row.get('outcome_tag')) or '—')}</td>"
                f"<td class='dim'>{_e(zh_text(row.get('review_note') or row.get('mistake_tag')) or '—')}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>真实成交</strong><span class='dim'>导入结果，不是候选分数</span></div>"
            "<div class='table-scroll'><table><thead><tr><th>标的</th><th>状态</th>"
            "<th class='num'>仓位</th><th class='num'>净收益</th><th>结果</th><th>备注</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    else:
        parts.append(
            "<div class='empty'>今日没有导入成交。没有成交，就不能用候选分数写胜率。"
            "收盘后用 operator_outcomes 导入真实结果、跳过或取消。</div>"
        )

    if plans:
        rows = []
        for plan in plans[:12]:
            plan_status = (
                "研究草案（门禁未开放）"
                if plans_blocked
                else zh_text(cn(PLAN_STATUS_CN, plan.get("status")))
            )
            rows.append(
                f"<tr><td class='mono'>{_slk(plan.get('stock_code'))}</td><td>{_e(plan.get('stock_name'))}</td>"
                f"<td>{_e(cn(SETUP_TYPE_CN, plan.get('setup_type')))}</td><td class='num'>{_pct(plan.get('max_position_pct'))}</td>"
                f"<td>{_status_pill(plan_status)}</td>"
                f"<td class='dim'>{_e(zh_text(plan.get('entry_condition')) or '—')}</td>"
                f"<td class='dim'>{_e(zh_text(plan.get('stop_condition')) or '—')}</td></tr>"
            )
        plan_title = "今日执行计划" if execution_ready else "研究计划草案"
        plan_note = "已通过当前执行门禁" if execution_ready else "门禁开放后才可能进入执行计划"
        parts.append(
            f"<div class='sec-title'><strong>{plan_title}</strong>"
            f"<span class='dim'>{plan_note}</span></div>"
            "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th><th>类型</th>"
            "<th class='num'>研究上限</th><th>状态</th><th>观察条件</th><th>失效条件</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if watchlist:
        rows = []
        for row in watchlist[:10]:
            rows.append(
                f"<tr><td class='mono'>{_slk(row.get('stock_code'))}</td><td>{_e(row.get('stock_name'))}</td>"
                f"<td class='dim'>{_e(zh_text(row.get('thesis')) or '—')}</td>"
                f"<td class='dim'>{_e(zh_text(row.get('invalidation')) or '—')}</td>"
                f"<td>{_e(zh_text(cn(WATCHLIST_STATUS_CN, row.get('status'))))}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>观察池</strong></div>"
            "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th>"
            "<th>逻辑</th><th>失效</th><th>状态</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if journal:
        rows = []
        for row in journal[:10]:
            rows.append(
                f"<tr><td class='mono'>{_e(row.get('action_time'))}</td>"
                f"<td>{_e(row.get('stock_name') or row.get('stock_code'))}</td>"
                f"<td>{_e(row.get('action'))}</td>"
                f"<td class='dim'>{_e(row.get('reason') or '—')}</td>"
                f"<td>{_e(row.get('mistake_tag') or '—')}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>日志与失误标签</strong></div>"
            "<div class='table-scroll'><table><thead><tr><th>时间</th><th>标的</th>"
            "<th>动作</th><th>理由</th><th>标签</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if picks:
        rows = []
        for row in picks[:10]:
            rows.append(
                f"<tr><td class='mono'>{_slk(row.get('stock_code'))}</td><td>{_e(row.get('stock_name'))}</td>"
                f"<td class='num'>{_fmt(row.get('score'))}</td>"
                f"<td class='num {_sign_class(row.get('main_net'))}'>{_fmt(row.get('main_net'))}</td>"
                f"<td>{_e(zh_text(cn(SELECTION_STATUS_CN, row.get('selection_status'))))}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>研究候选</strong><span class='dim'>不是订单</span></div>"
            "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th>"
            "<th class='num'>分数</th><th class='num'>主力净额</th><th>状态</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    return "\n".join(parts)


def _render_sector_trail(
    ctx: dict[str, Any],
    external_lazy: bool = False,
    compact_overview: bool = False,
) -> str:
    trail = ctx.get("sector_trail") or {}
    periods = ctx.get("sector_periods") or {}
    dates = trail.get("dates") or []
    sectors = trail.get("sectors") or []
    if not dates or not sectors:
        message = trail.get("message") or "暂无板块轨迹"
        return f"<div class='empty'>{_e(message)}</div>"
    cards = []
    if compact_overview:
        # The daily page is a decision surface. Keep only the latest 50 theme
        # rows in HTML and send historical/member drill-down users to the
        # dedicated static sector page. Embedding stock_pct plus all dates
        # here previously made one daily page nearly 10 MB.
        day = dates[0]
        ranked = []
        for sector in sectors:
            cell = (sector.get("daily") or {}).get(day)
            if cell:
                ranked.append((sector, cell))
        ranked.sort(
            key=lambda item: (
                -int(item[1].get("limit_up") or 0),
                -float(item[1].get("strength") or -999),
                -float(item[1].get("main_net") or 0),
            )
        )
        rows = []
        for index, (sector, cell) in enumerate(ranked[:50], start=1):
            rows.append(
                f"<div class='day-row cols5'>"
                f"<span class='dim'>{index}</span>"
                f"<span class='n'>{_e(sector.get('name'))}</span>"
                f"<span class='num {_sign_class(cell.get('strength'))}'>{_pct(cell.get('strength'))}</span>"
                f"<span class='num up'>{_e(cell.get('limit_up') if cell.get('limit_up') is not None else '—')}</span>"
                f"<span class='num {_sign_class(cell.get('pct_chg'))}'>{_pct(cell.get('pct_chg'))}</span>"
                "</div>"
            )
        remainder = max(0, len(ranked) - 50)
        empty = "<div class='empty'>暂无概念数据</div>"
        return (
            f"<div class='trail-source-note'>概念源：{_e(trail.get('concept_source') or 'THS完整质量门控快照')}；"
            f"最新可用成分快照：{_e(trail.get('membership_date') or '—')}。"
            "日页只保留最新交易日的前50个概念，历史和个股明细进入全宽专页。</div>"
            f"<div class='trail-window-note'>当前日：{_e(day)} · 共 {len(ranked)} 个可用概念"
            f"{' · 另有 ' + str(remainder) + ' 个概念请进入全宽专页' if remainder else ''}</div>"
            "<div class='trail-scroll compact-trail'><div class='day-card'>"
            f"<div class='day-title'>{_e(day)}</div>"
            "<div class='day-head cols5'><span>#</span><span>概念</span><span class='num'>强度</span>"
            "<span class='num'>涨停</span><span class='num'>涨幅</span></div>"
            f"<div class='day-rows'>{''.join(rows) or empty}</div>"
            "</div></div>"
        )
    # Keep the full summary inline for offline drill-down, but limit the
    # initial HTML to the latest day. Older days are rendered after expansion.
    for day in dates[:1]:
        ranked = []
        for sector in sectors:
            cell = (sector.get("daily") or {}).get(day)
            if not cell:
                continue
            ranked.append((sector, cell))
        ranked.sort(
            key=lambda item: (
                -int(item[1].get("limit_up") or 0),
                -float(item[1].get("strength") or -999),
                -float(item[1].get("main_net") or 0),
            )
        )
        rows = []
        for index, (sector, cell) in enumerate(ranked[:50], start=1):
            rows.append(
                f"<button type='button' class='day-row cols5' data-trail-id='{_e(sector.get('id'))}'>"
                f"<span class='dim'>{index}</span>"
                f"<span class='n'>{_e(sector.get('name'))}</span>"
                f"<span class='num {_sign_class(cell.get('strength'))}'>{_pct(cell.get('strength'))}</span>"
                f"<span class='num up'>{_e(cell.get('limit_up') if cell.get('limit_up') is not None else '—')}</span>"
                f"<span class='num {_sign_class(cell.get('pct_chg'))}'>{_pct(cell.get('pct_chg'))}</span>"
                f"</button>"
            )
        extra = ""
        if len(ranked) > 50:
            extra = f"<button type='button' class='trail-more' data-expand-col='1'>加载全部（剩余 {len(ranked) - 50} 个）</button>"
        cards.append(
            f"<div class='day-card' data-trail-day='{_e(day)}'>"
            f"<div class='day-title'>{_e(day)}</div>"
            f"<div class='day-head cols5'><span>#</span><span>概念</span><span class='num'>强度</span><span class='num'>涨停</span><span class='num'>涨幅</span></div>"
            f"<div class='day-rows'>{''.join(rows)}{extra}</div></div>"
        )
    summary_sectors = []
    daily_details: dict[str, Any] = {}
    period_details: dict[str, dict[str, Any]] = {"week": {}, "month": {}, "quarter": {}}
    stock_names = trail.get("stock_names") or {}
    slim_day_stock = lambda st: {
        "stock_code": st.get("stock_code"),
        "stock_name": st.get("stock_name"),
        "board_level": st.get("board_level"),
        "limit_up_time": st.get("limit_up_time"),
        "pct_chg": st.get("pct_chg"),
    }
    slim_period_stock = lambda st: {
        "stock_code": st.get("stock_code"),
        "stock_name": st.get("stock_name"),
        "limit_up": st.get("limit_up"),
        "pct_chg": st.get("pct_chg"),
        "board_level": st.get("board_level"),
    }
    for sector in sectors:
        daily_summary = {}
        daily_detail = {}
        for day, cell in (sector.get("daily") or {}).items():
            daily_summary[day] = {key: value for key, value in cell.items() if key != "stocks"}
            daily_detail[day] = {
                "stocks": [slim_day_stock(st) for st in (cell.get("stocks") or [])]
            }
        members = sector.get("members")
        summary_sectors.append(
            {
                "id": sector.get("id"),
                "name": sector.get("name"),
                "daily": daily_summary,
                "members": {
                    "snapshot_date": members.get("snapshot_date"),
                    "codes": members.get("codes"),
                } if members else None,
            }
        )
        daily_details[str(sector.get("id"))] = {"daily": daily_detail}
    daily_payload = json.dumps(
        {
            "dates": dates,
            "sectors": summary_sectors,
            "stock_names": stock_names,
            # Shared by every concept so overlapping memberships do not
            # duplicate the same stock/date return arrays in the HTML.
            "stock_pct": trail.get("stock_pct") or {},
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")

    period_summary = {}
    for kind in ("week", "month", "quarter"):
        pack = periods.get(kind) or {}
        summary_period_sectors = []
        for sector in pack.get("sectors") or []:
            period_summary_cells = {}
            period_detail_cells = {}
            for label, cell in (sector.get("periods") or {}).items():
                period_summary_cells[label] = {key: value for key, value in cell.items() if key != "stocks"}
                period_detail_cells[label] = {"stocks": cell.get("stocks") or []}
            summary_period_sectors.append(
                {"id": sector.get("id"), "name": sector.get("name"), "periods": period_summary_cells}
            )
            period_details[kind][str(sector.get("id"))] = {"periods": {
                    label: {"stocks": [slim_period_stock(st) for st in (cell.get("stocks") or [])]}
                    for label, cell in (sector.get("periods") or {}).items()
                }} if not compact_overview else {"periods": {}}
        period_summary[kind] = {
            "periods": pack.get("periods") or [],
            "sectors": summary_period_sectors,
        }
    period_payload = json.dumps(period_summary, ensure_ascii=False).replace("</", "<\\/")
    detail_payload = json.dumps(
        {
            "daily": daily_details,
            "periods": period_details,
            "compact_overview": compact_overview,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).replace("</", "<\\/")
    detail_scripts = "" if external_lazy else (
        f"<script type='application/json' id='trail-details'>{detail_payload}</script>"
    )
    return (
        "<div class='tabs' id='trail-mode-tabs'>"
        "<button type='button' class='active' data-trail-mode='day'>日</button>"
        "<button type='button' data-trail-mode='week'>周</button>"
        "<button type='button' data-trail-mode='month'>月</button>"
        "<button type='button' data-trail-mode='quarter'>季</button>"
        "</div>"
        f"<div class='trail-source-note'>概念源：{_e(trail.get('concept_source') or 'THS完整质量门控快照')}；最新可用成分快照：{_e(trail.get('membership_date') or '—')}。逐交易日只取不晚于该日的最近完整快照；未完成或超时快照不发布。资金流按概念代码关联，缺失显示为 —，不向前填充。周/月/季个股收益为窗口首收至末收。</div>"
        f"<div class='trail-window-note'>当前窗口：{_e(dates[-1] if dates else '—')} 至 {_e(dates[0] if dates else '—')}，共 {_e(len(dates))} 个可用交易日。</div>"
        f"<div class='trail-scroll' id='trail-board'>{''.join(cards)}</div>"
        f"<div class='trail-detail' id='trail-detail'>"
        f"<div class='dim'>点上方概念，下面按同一粒度列出该概念的涨停个股。一只票可以同时出现在多个概念里。</div></div>"
        f"<script type='application/json' id='trail-data'>{daily_payload}</script>"
        f"<script type='application/json' id='trail-periods'>{period_payload}</script>"
        f"{detail_scripts}"
    )


def _render_named_ladder(ctx: dict[str, Any]) -> str:
    groups = ((ctx.get("ecology") or {}).get("ladder_groups")) or []
    if not groups:
        return "<div class='empty'>当日涨停池没有连板名单</div>"
    rows = []
    for group in groups:
        names = "、".join(_e(n) for n in (group.get("names") or [])[:10])
        extra = f"<span class='dim'> 等{group.get('count')}只</span>" if (group.get("count") or 0) > 10 else ""
        rows.append(
            f"<tr><td class='num'><strong>{_e(group.get('height'))}板</strong></td>"
            f"<td class='num'>{_e(group.get('count'))}</td>"
            f"<td>{names}{extra}</td>"
            f"<td class='dim'>{_e(group.get('note') or '')}</td></tr>"
        )
    return (
        "<div class='table-scroll'><table><thead><tr>"
        "<th class='num'>高度</th><th class='num'>家数</th><th>股票</th><th>备注</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>"
    )


def _render_yday_limitup(ctx: dict[str, Any]) -> str:
    yday = ((ctx.get("ecology") or {}).get("yday")) or {}
    if not yday.get("n"):
        return "<div class='empty'>没有可比的昨日涨停样本</div>"
    cells = [
        ("样本", _e(yday.get("n")), f"对比 {yday.get('prev_date') or '—'}"),
        ("平均涨幅", _pct(yday.get("avg_ret")), f"正收益 {_pct(yday.get('pos_rate'))}"),
        ("继续涨停", _e(yday.get("still_limit_up") or 0), f"跌停 {yday.get('limit_down') or 0}"),
        ("首板 / 多板", f"{_pct(yday.get('first_avg'))} / {_pct(yday.get('multi_avg'))}", "打板环境"),
    ]
    return (
        "<div class='ticker'>"
        + "".join(
            f"<div class='kpi'><div class='k-label'>{label}</div>"
            f"<div class='k-value'>{value}</div><div class='k-sub'>{_e(sub)}</div></div>"
            for label, value, sub in cells
        )
        + "</div>"
    )


def _render_broken(ctx: dict[str, Any]) -> str:
    rows = ((ctx.get("ecology") or {}).get("broken")) or []
    if not rows:
        return "<div class='empty'>没有昨 2 板以上今日断板的样本</div>"
    body = []
    for row in rows:
        body.append(
            f"<tr><td>{_e(row.get('stock_name') or row.get('stock_code'))}</td>"
            f"<td class='num'>{_e(row.get('board_level'))}板</td>"
            f"<td class='num {_sign_class(row.get('change_pct'))}'>{_pct(row.get('change_pct'))}</td></tr>"
        )
    return (
        "<div class='table-scroll'><table><thead><tr>"
        "<th>股票</th><th class='num'>昨板</th><th class='num'>今日涨幅</th>"
        "</tr></thead><tbody>" + "".join(body) + "</tbody></table></div>"
        "<div class='concept-footnote'>这是昨日连板今日未封的名单，用来看亏钱效应，不是炸板接口本身。"
        "KPL 炸板表停在 7 月初，所以用涨停池对比来补。</div>"
    )


def _render_tomorrow(ctx: dict[str, Any]) -> str:
    story = ctx.get("narrative") or build_review_narrative(ctx)
    items = story.get("tomorrow") or []
    if not items:
        return "<div class='empty'>次日约束尚未形成</div>"
    lis = "".join(
        f"<li><span class='n'>{index:02d}</span><span>{_e(item)}</span></li>"
        for index, item in enumerate(items, start=1)
    )
    return f"<ol>{lis}</ol>"


def _render_lhb(ctx: dict[str, Any]) -> str:
    rows = (ctx.get("capital_flow") or {}).get("lhb") or []
    if not rows:
        summary = (ctx.get("market_context") or {}).get("lhb_summary") or {}
        latest = summary.get("latest_date") or "无历史批次"
        return (
            "<div class='empty'>当日龙虎榜不可用（不解读为 0 条）；"
            f"本地最新批次 { _e(latest) }</div>"
        )
    body = []
    for row in rows[:12]:
        body.append(
            f"<tr><td>{_e(row.get('stock_name') or row.get('stock_code'))}</td>"
            f"<td class='num {_sign_class(row.get('change_pct'))}'>{_pct(row.get('change_pct'))}</td>"
            f"<td class='dim'>{_e(row.get('reason') or '—')}</td>"
            f"<td class='num'>{_fmt(row.get('buy_amount'))}</td>"
            f"<td class='num'>{_fmt(row.get('sell_amount'))}</td>"
            f"<td class='num {_sign_class(row.get('net_amount'))}'>{_fmt(row.get('net_amount'))}</td></tr>"
        )
    return (
        "<div class='table-scroll'><table><thead><tr><th>标的</th><th class='num'>涨幅</th>"
        "<th>理由</th><th class='num'>买</th><th class='num'>卖</th><th class='num'>净额</th>"
        f"</tr></thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _flow_table(title: str, rows: list[dict[str, Any]], columns: list[tuple[str, str, str]],
                empty: str = "暂无数据") -> str:
    head = "".join(f"<th class='{cls}'>{_e(label)}</th>" for _, label, cls in columns)
    body = ""
    money_keys = {
        "main_net", "super_net", "large_net", "small_net", "mid_net",
        "main_net_3d", "main_net_5d", "main_net_10d", "twenty_day_main_net",
        "flow_acceleration_5d",
    }
    for row in rows:
        tds = []
        for key, _, cls in columns:
            val = row.get(key)
            if key == "provider":
                val = cn(PROVIDER_CN, val)
            elif key in money_keys:
                tds.append(f"<td class='num {_sign_class(val)}'>{_fmt(val)}</td>")
            elif key in {"change_pct", "seal_rate"}:
                tds.append(f"<td class='num {_sign_class(val)}'>{_pct(val)}</td>")
            else:
                tds.append(f"<td class='{cls}'>{_e(val) if val is not None else '—'}</td>")
        body += f"<tr>{''.join(tds)}</tr>"
    if not body:
        body = f"<tr><td colspan='{len(columns)}' class='empty'>{empty}</td></tr>"
    return (
        f"<div class='flow-block'><div class='sec-title'><strong>{_e(title)}</strong></div>"
        f"<div class='table-scroll'><table><thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody></table></div></div>"
    )


def _render_tables(ctx: dict[str, Any]) -> str:
    """Render flow tables using the canonical context field names."""
    flow = ctx.get("capital_flow", {})

    def ranked(key: str, limit: int) -> list[dict[str, Any]]:
        return [{"rank": i + 1, **row} for i, row in enumerate(flow.get(key, [])[:limit])]

    stock_cols = [
        ("rank", "排名", "num"), ("stock_code", "代码", "mono"),
        ("stock_name", "名称", ""), ("main_net", "主力净额", "num"),
        ("super_net", "超大单", "num"), ("large_net", "大单", "num"),
        ("change_pct", "涨幅", "num"), ("provider", "来源", "dim"),
    ]
    sector_cols = [
        ("rank", "排名", "num"), ("sector_name", "名称", ""),
        ("main_net", "主力净额", "num"), ("change_pct", "涨幅", "num"),
        ("provider", "来源", "dim"),
    ]
    industry_cols = [
        ("rank", "排名", "num"), ("sector_name", "名称", ""),
        ("main_net", "主力净额", "num"), ("change_pct", "涨幅", "num"),
        ("provider", "来源", "dim"),
    ]
    blocks = [
        ("个股主力净流入 Top 20", ranked("stock_inflow", 20), stock_cols),
        ("个股主力净流出 Top 20", ranked("stock_outflow", 20), stock_cols),
        ("板块资金净流入 Top 12", ranked("sector_inflow", 12), sector_cols),
        ("板块资金净流出 Top 12", ranked("sector_outflow", 12), sector_cols),
        ("行业主力净流入 Top 10", ranked("industry_inflow", 10), industry_cols),
        ("行业主力净流出 Top 10", ranked("industry_outflow", 10), industry_cols),
    ]
    out = []
    for index in range(0, len(blocks), 2):
        out.append("<div class='grid g-2'>")
        for title, rows, columns in blocks[index:index + 2]:
            out.append(_flow_table(title, rows, columns))
        out.append("</div>")
    persistence_cols = [
        ("stock_code", "代码", "mono"),
        ("sector_name", "概念", ""),
        ("observed_days_20d", "观察天数", "num"),
        ("positive_days", "正流入天数", "num"),
        ("main_net_5d", "5日净流", "num"),
        ("twenty_day_main_net", "20日净流", "num"),
        ("flow_acceleration_5d", "5日加速度", "num"),
    ]
    out.append("<div class='grid g-2'>")
    out.append(_flow_table("个股资金持续流入 Top 20", ranked("stock_flow_persistence", 20), persistence_cols))
    out.append(_flow_table("个股资金持续流出 Top 20", ranked("stock_flow_persistence_outflow", 20), persistence_cols))
    out.append("</div>")
    out.append("<div class='grid g-2'>")
    out.append(_flow_table("THS概念资金持续流入 Top 20", ranked("sector_flow_persistence", 20), persistence_cols))
    out.append(_flow_table("THS概念资金持续流出 Top 20", ranked("sector_flow_persistence_outflow", 20), persistence_cols))
    out.append("</div>")
    return "\n".join(out)


def _render_themes(ctx: dict[str, Any]) -> str:
    themes = ctx.get("theme_mainline", [])
    if not themes:
        return "<div class='empty'>暂无题材主线数据</div>"
    rows = []
    for t in themes[:10]:
        rows.append(
            f"<tr><td class='mono'>{_e(t['code'])}</td><td><strong>{_e(t['name'])}</strong></td>"
            f"<td class='num {_sign_class(t.get('score'))}'>{_fmt(t.get('score'))}</td>"
            f"<td class='num'>{_num(t.get('limit_up_count'))}</td>"
            f"<td class='num'>{_pct(t.get('seal_rate'))}</td>"
            f"<td class='num {_sign_class(t.get('main_net'))}'>{_fmt(t.get('main_net'))}</td>"
            f"<td class='num'>{_num(t.get('components'))}</td>"
            f"<td class='dim'>{_e(t.get('reason') or '—')}</td></tr>"
        )
    return (
        "<div class='metric-note'>题材综合分当前仅作实验排序，状态："
        f"{_e(COMPOSITE_SCORE_STATUS)}；未完成历史验证前，不作为正式口径或交易依据。</div>"
        "<div class='table-scroll'><table><thead><tr>"
        "<th>代码</th><th>题材</th><th class='num'>主线分</th><th class='num'>涨停数</th>"
        "<th class='num'>封板率</th><th class='num'>主力净额</th><th class='num'>成分数</th>"
        "<th>驱动逻辑</th></tr></thead><tbody>" + "\n".join(rows) + "</tbody></table></div>"
    )


def _render_concept_limit_up(ctx: dict[str, Any], inline_stock_limit: int | None = None) -> str:
    """Render the concept selector above the same-date limit-up stocks."""
    review = ctx.get("concept_limit_up") or {}
    groups = review.get("groups") or []
    if not groups:
        message = zh_text(review.get("message") or "暂无概念—涨停个股联动数据")
        if review.get("membership_stale"):
            message += (
                f"；最新成分快照 {review.get('membership_date') or '未知'}。"
                "请在收盘后运行 scripts/backfill_2026_ths_concepts.py 更新同花顺概念成分。"
            )
        return f"<div class='empty'>{_e(message)}</div>"
    tabs = []
    for index, group in enumerate(groups[:50]):
        active = " active" if index == 0 else ""
        tabs.append(
            f"<button type='button' class='concept-tab{active}' data-concept-index='{index}'>"
            f"<span class='concept-rank'>{index + 1:02d}</span>"
            f"<span class='concept-name'>{_e(group.get('concept_name') or '—')}</span>"
            f"<span class='concept-lu'>{_e(group.get('limit_up_count') or 0)}</span>"
            f"<span class='concept-meta'>主线分 {_fmt(group.get('mainline_score'))} · 最高 { _fmt(group.get('max_board'), '—') }板 · 成分 {_num(group.get('member_count'))}</span>"
            f"</button>"
        )
    if len(groups) > 50:
        tabs.append(
            f"<button type='button' class='concept-more' id='concept-more' data-expand-concepts='1'>"
            f"加载全部概念（剩余 {len(groups) - 50} 个）</button>"
        )
    first = groups[0]
    first_stocks = first.get("limit_up_stocks") or []
    cards = []
    display_stocks = first_stocks
    if inline_stock_limit is not None:
        display_stocks = first_stocks[:max(0, inline_stock_limit)]
    for row in display_stocks:
        board = row.get("board_level")
        board_text = f"{_fmt(board)}板" if board is not None else "—"
        cards.append(
            f"<div class='concept-stock-card'>"
            f"<span class='stock-name'>{_e(row.get('stock_name') or '—')}</span>"
            f"<span class='stock-board'>{_e(board_text)}</span>"
            f"<span class='stock-code'>{_e(row.get('stock_code') or '—')}</span>"
            f"<span class='stock-time'>{_e(_fmt_clock(row.get('limit_up_time')))}</span>"
            f"</div>"
        )
    detail_name = _e(first.get("concept_name") or "—")
    detail_meta = f"涨停 {first.get('limit_up_count') or 0} 只 · 成分 {first.get('member_count') or 0} 只 · 快照 {review.get('membership_date') or '—'}"
    empty_card = "<div class='empty'>该概念暂无同日涨停个股</div>"
    footnote_prefix = ""
    if inline_stock_limit is not None and len(first_stocks) > len(display_stocks):
        footnote_prefix = (
            "<div class='concept-footnote'>首屏展示前 "
            + str(inline_stock_limit)
            + " 只，其余点击“加载该概念全部个股”。</div>"
        )
    return (
        "<div class='concept-review'>"
        f"<div><div class='concept-list' id='concept-list'>{''.join(tabs)}</div>"
        f"<div class='concept-footnote'>概念按当日涨停只数排序。一只股票可以同时属于多个概念，点开后列出该概念当天全部涨停股。</div></div>"
        f"<div class='concept-detail' id='concept-detail'>"
        f"<div class='concept-detail-head'><div><div class='concept-detail-title' id='concept-detail-title'>{detail_name}</div>"
        f"<div class='concept-detail-sub' id='concept-detail-sub'>{_e(detail_meta)}</div></div>"
        f"<span class='pill ok'>同日涨停池</span></div>"
        f"<div class='concept-stock-grid' id='concept-stock-grid'>{''.join(cards) or empty_card}</div>"
        f"{footnote_prefix}"
        f"<div class='concept-footnote'>点击左侧概念切换下方个股。个股只表示涨停池成员，不等于买入建议；仍需经过收盘数据、竞价、风险和人工门禁。</div>"
        "</div></div>"
    )


def _render_qlib_research(ctx: dict[str, Any]) -> str:
    """Render QLib as a visibly isolated research-only surface."""
    rows = (ctx.get("data_sources") or {}).get("qlib") or []
    if not rows:
        return (
            "<div class='notice'><strong>QLib shadow</strong>：当前没有可用评估行。"
            "模型、候选和历史回测不构成当日执行证据。</div>"
        )
    table_rows = []
    for row in rows[:8]:
        table_rows.append(
            f"<tr><td class='mono'>{_e(row.get('model_id') or '—')}</td>"
            f"<td>{_status_pill(row.get('stage') or 'shadow')}</td>"
            f"<td class='num'>{_num(row.get('sample_count'))}</td>"
            f"<td class='num'>{_fmt(row.get('hit_rate'))}</td>"
            f"<td class='num'>{_fmt(row.get('avg_forward_return_pct'))}</td>"
            f"<td class='num'>{_fmt(row.get('ic'))}</td>"
            f"<td class='num'>{_fmt(row.get('rank_ic'))}</td>"
            f"<td>{_e(row.get('signal_impact') or 'disabled')}</td></tr>"
        )
    return (
        "<div class='notice'><strong>research_only / shadow</strong>：QLib用于与主线、资金和涨停结构交叉验证。"
        "未通过模型晋级与人工风险审批时，不进入执行计划。</div>"
        "<div class='table-scroll' style='margin-top:12px'><table><thead><tr>"
        "<th>模型</th><th>阶段</th><th class='num'>样本</th><th class='num'>命中率</th>"
        "<th class='num'>前向收益</th><th class='num'>IC</th><th class='num'>RankIC</th><th>影响</th>"
        f"</tr></thead><tbody>{''.join(table_rows)}</tbody></table></div>"
    )


def _render_stages(ctx: dict[str, Any]) -> str:
    stages = ctx.get("candidate_flow", [])
    if not stages:
        stages = [{"stage": "premarket_pool", "total": 0, "actionable": 0, "tradable": 0,
                   "approved": 0, "executable": 0}]
    nodes = []
    stage_labels = {
        "premarket_pool": "盘前池", "auction_confirmation": "竞价确认",
        "intraday_strength": "盘中强度", "close_decision": "收盘决策",
    }
    max_total = max((st.get("total") or 0) for st in stages) or 1
    for st in stages:
        stage = str(st.get("stage", ""))
        total = st.get("total") or 0
        pct = int(round(total * 100.0 / max_total))
        cls = "hot" if stage == "close_decision" and total else ""
        nodes.append(
            f"<div class='node {cls}'><div class='l'>{_e(stage_labels.get(stage, stage))}</div>"
            f"<div class='n'>{total}</div>"
            f"<div style='height:2px;background:var(--line);margin-top:8px;overflow:hidden'>"
            f"<i style='display:block;height:100%;width:{pct}%;background:var(--rule)'></i></div>"
            f"<div class='l'>可行动 {st.get('actionable') or 0} · 风控过 {st.get('approved') or 0} · 可执行 {st.get('executable') or 0}</div></div>"
        )
    return "<div class='flowline'>" + " <div class='arrow'>→</div> ".join(nodes) + "</div>"


def _render_alerts(ctx: dict[str, Any]) -> str:
    alerts = ctx.get("alerts", [])
    if not alerts:
        return ("<div class='empty'>今日无风险告警。"
                "告警按复盘交易日过滤；若确认该日应有告警，请检查 alert_events 表。</div>")
    out = []
    for a in alerts[:10]:
        severity = str(a.get("severity") or "info")
        cls = "bad" if severity in {"error", "critical", "warning"} else "warn"
        sev_cn = zh_text(cn(SEVERITY_CN, severity))
        cat_cn = zh_text(cn(CATEGORY_CN, a.get("category")))
        out.append(
            f"<div class='alert {cls}'><strong>{_e(sev_cn)} / {_e(cat_cn)}</strong>"
            f"<div>{_e(zh_text(a.get('message')))}</div></div>"
        )
    return "\n".join(out)


def _render_plans(ctx: dict[str, Any]) -> str:
    plans = ctx.get("plans", [])
    journal = ctx.get("journal", [])
    risk = ctx.get("risk", {})
    execution_ready = bool((ctx.get("execution_control") or {}).get("execution_ready"))
    parts: list[str] = []
    parts.append(
        "<div class='detail-grid'>"
        f"<div class='detail-item'><div class='d-label'>风控状态</div><div class='d-value'>{_e(risk.get('risk_state') or '—')}</div></div>"
        f"<div class='detail-item'><div class='d-label'>总仓位上限</div><div class='d-value mono'>{_pct(risk.get('total_position_pct'))}</div></div>"
        f"<div class='detail-item'><div class='d-label'>单票上限</div><div class='d-value mono'>{_pct(risk.get('max_single_position_pct'))}</div></div>"
        f"<div class='detail-item'><div class='d-label'>单板块上限</div><div class='d-value mono'>{_pct(risk.get('max_sector_position_pct'))}</div></div>"
        "</div>"
    )
    if plans:
        rows = []
        for p in plans[:15]:
            plan_status = (
                "研究草案（门禁未开放）"
                if not execution_ready
                else zh_text(cn(PLAN_STATUS_CN, p.get("status")))
            )
            rows.append(
                f"<tr><td class='mono'>{_slk(p.get('stock_code'))}</td><td>{_e(p.get('stock_name'))}</td>"
                f"<td>{_e(cn(SETUP_TYPE_CN, p.get('setup_type')))}</td><td class='num'>{_pct(p.get('max_position_pct'))}</td>"
                f"<td>{_status_pill(plan_status)}</td>"
                f"<td class='dim'>{_e(zh_text(p.get('entry_condition')) or '—')}</td>"
                f"<td class='dim'>{_e(zh_text(p.get('stop_condition')) or '—')}</td></tr>"
            )
        plan_title = "今日执行计划" if execution_ready else "研究计划草案"
        plan_note = "已通过当前执行门禁" if execution_ready else "execution_ready=false 时不是订单"
        parts.append(
            f"<div class='sec-title'><strong>{plan_title}</strong>"
            f"<span class='dim'>{plan_note}</span></div>"
            "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th><th>类型</th>"
            "<th class='num'>研究上限</th><th>状态</th><th>观察条件</th><th>失效条件</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if journal:
        rows = []
        for j in journal[:15]:
            rows.append(
                f"<tr><td class='mono'>{_e(j.get('action_time'))}</td><td class='mono'>{_e(j.get('stock_code'))}</td>"
                f"<td>{_e(j.get('stock_name'))}</td><td>{_e(j.get('action'))}</td>"
                f"<td class='dim'>{_e(j.get('reason') or '—')}</td>"
                f"<td>{_e(j.get('mistake_tag') or '—')}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>交易日志</strong></div>"
            "<div class='table-scroll'><table><thead><tr><th>时间</th><th>代码</th><th>名称</th>"
            "<th>动作</th><th>理由</th><th>失误标签</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>"
        )
    if not plans and not journal:
        parts.append("<div class='empty'>暂无计划与日志</div>")
    return "\n".join(parts)


def _render_data_gates(ctx: dict[str, Any]) -> str:
    readiness = ctx.get("readiness", {})
    data_sources = ctx.get("data_sources", {})
    parts: list[str] = []
    # The legacy analysis pill now uses the certified gate; the explicit
    # source/pipeline fields remain available in the readiness payload.
    data_certified = readiness.get("data_certified_ready")
    flow_certified = readiness.get("flow_certified_ready")
    analytics = readiness.get("analysis_ready", readiness.get("certified_ready", readiness.get("analytics_ready")))
    execution = readiness.get("execution_ready")
    operator_status = readiness.get("operator_status", "uncertified")
    missing = readiness.get("missing_groups") or []
    parts.append(
        "<div class='detail-grid'>"
        f"<div class='detail-item'><div class='d-label'>Data certified</div><div class='d-value'>{_status_pill(str(data_certified))}</div></div>"
        f"<div class='detail-item'><div class='d-label'>Flow certified</div><div class='d-value'>{_status_pill('not_assessed' if flow_certified is None else str(flow_certified))}</div></div>"
        f"<div class='detail-item'><div class='d-label'>Operator status</div><div class='d-value mono'>{_e(operator_status)}</div></div>"
        f"<div class='detail-item'><div class='d-label'>分析就绪</div><div class='d-value'>{_status_pill(str(analytics))}</div></div>"
        f"<div class='detail-item'><div class='d-label'>执行就绪</div><div class='d-value'>{_status_pill(str(execution))}</div></div>"
        f"<div class='detail-item'><div class='d-label'>缺失分组</div><div class='d-value'>{_e(', '.join(missing) if missing else 'none')}</div></div>"
        f"<div class='detail-item'><div class='d-label'>候选池</div><div class='d-value mono'>{readiness.get('actionable_candidates', 0)} 可行动 / {readiness.get('executable_candidates', 0)} 可执行</div></div>"
        "</div>"
    )
    checkpoints = data_sources.get("provider_checkpoints") or data_sources.get("checkpoints") or []
    if checkpoints:
        source_rows = []
        for item in checkpoints[:16]:
            source_rows.append(
                f"<tr><td>{_e(item.get('dataset') or item.get('name') or '—')}</td>"
                f"<td>{_status_pill(item.get('status'))}</td>"
                f"<td class='num'>{_num(item.get('rows'))}</td>"
                f"<td class='dim'>{_e(item.get('error') or '—')}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>数据源检查点</strong></div>"
            "<div class='table-scroll'><table><thead><tr><th>数据集</th><th>状态</th>"
            "<th class='num'>行数</th><th>错误</th></tr></thead>"
            f"<tbody>{''.join(source_rows)}</tbody></table></div>"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Chart data / JS
# ---------------------------------------------------------------------------

def _concept_inline_data(ctx: dict[str, Any], stock_limit: int = 50) -> list[dict[str, Any]]:
    """Return concept metadata plus a bounded first-screen stock slice."""
    groups = (ctx.get("concept_limit_up") or {}).get("groups") or []
    inline: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        item = dict(group)
        stocks = list(group.get("limit_up_stocks") or [])
        # Only the initially selected concept needs cards in the initial HTML.
        # Other concepts retain metadata and are hydrated from the sidecar on
        # click, so a broad concept catalog does not recreate the old payload.
        item["limit_up_stocks"] = stocks[:stock_limit] if index == 0 else []
        inline.append(item)
    return inline


def _build_review_lazy_payload(ctx: dict[str, Any]) -> dict[str, Any]:
    """Extract full drill-down data for the optional same-directory sidecar."""
    trail = ctx.get("sector_trail") or {}
    periods = ctx.get("sector_periods") or {}
    payload: dict[str, Any] = {
        "concept_groups": (ctx.get("concept_limit_up") or {}).get("groups") or [],
        "trail_details": {},
        "period_details": {"week": {}, "month": {}, "quarter": {}},
    }
    for sector in trail.get("sectors") or []:
        sector_id = str(sector.get("id") or "")
        daily: dict[str, Any] = {}
        for day, cell in (sector.get("daily") or {}).items():
            daily[str(day)] = {"stocks": cell.get("stocks") or []}
        payload["trail_details"][sector_id] = {
            "window_stocks": sector.get("window_stocks") or [],
            "daily": daily,
        }
    for kind in ("week", "month", "quarter"):
        for sector in (periods.get(kind) or {}).get("sectors") or []:
            sector_id = str(sector.get("id") or "")
            cells: dict[str, Any] = {}
            for label, cell in (sector.get("periods") or {}).items():
                cells[str(label)] = {"stocks": cell.get("stocks") or []}
            payload["period_details"][kind][sector_id] = {"periods": cells}
    return payload


def _build_review_lazy_asset(ctx: dict[str, Any]) -> str:
    payload = json.dumps(
        _build_review_lazy_payload(ctx),
        ensure_ascii=False,
        default=str,
    ).replace("</", "<\\/")
    return f"window.__REVIEW_LAZY_DATA__ = {payload};"


def _chart_js(ctx: dict[str, Any], trend: dict[str, Any], ladder: list[dict[str, Any]],
              rotation: list[dict[str, Any]], concept_limit_up: dict[str, Any] | None = None,
              lazy_asset_name: str | None = None,
              concept_data: list[dict[str, Any]] | None = None) -> str:
    themes = [
        {"name": t["name"], "score": t.get("score"), "main_net": t.get("main_net"),
         "seal_rate": t.get("seal_rate")}
        for t in ctx.get("theme_mainline", [])[:10]
    ]
    d = json.dumps
    concept_data_json = json.dumps(
        concept_data if concept_data is not None else (concept_limit_up or {}).get("groups", []),
        ensure_ascii=False,
        default=str,
    ).replace("</", "<\\/")
    lazy_asset_json = json.dumps(lazy_asset_name or "", ensure_ascii=False)
    return f"""
const trend = {d(trend, ensure_ascii=False)};
const ladder = {d(ladder)};
const rotation = {d(rotation, ensure_ascii=False)};
const themes = {d(themes, ensure_ascii=False)};
let conceptData = {concept_data_json};
const lazyAsset = {lazy_asset_json};

const AXIS = {{axisLine:{{lineStyle:{{color:'#3a3226'}}}}, axisLabel:{{color:'#a89880', fontSize:11}},
  splitLine:{{lineStyle:{{color:'rgba(58,50,38,.28)'}}}}}};

function base(extra) {{
  const c = {{ backgroundColor:'transparent', textStyle:{{color:'#f3ead8'}},
    legend:{{textStyle:{{color:'#a89880'}}, top:0}},
    tooltip:{{trigger:'axis', backgroundColor:'#1c1812', borderColor:'#3a3226',
      textStyle:{{color:'#f3ead8', fontSize:12}}}},
    grid:{{left:44, right:48, top:38, bottom:26}} }};
  return Object.assign(c, extra);
}}

const charts = [];

const shortDates = (trend.dates || []).map(d => String(d).slice(5));

charts.push({{el:'chart-breadth',
  opt: base({{ xAxis:{{type:'category', data:shortDates, ...AXIS}},
    yAxis:[{{type:'value', name:'家数', nameTextStyle:{{color:'#7d8ca6'}}, ...AXIS}},
           {{type:'value', name:'炸板率%', nameTextStyle:{{color:'#7d8ca6'}}, ...AXIS}}],
    series:[
      {{name:'涨停', type:'bar', data:trend.limit_up, itemStyle:{{color:'#d64545'}}, barMaxWidth:14}},
      {{name:'跌停', type:'bar', data:trend.limit_down, itemStyle:{{color:'#2f8f6b'}}, barMaxWidth:14}},
      {{name:'炸板率', type:'line', yAxisIndex:1, data:trend.broken_rate,
        itemStyle:{{color:'#d4a017'}}, lineStyle:{{color:'#d4a017', width:2}},
        symbolSize:5}}
    ] }})
}});

charts.push({{el:'chart-emotion',
  opt: base({{ xAxis:{{type:'category', data:shortDates, ...AXIS}},
    yAxis:[{{type:'value', name:'家数', ...AXIS}}, {{type:'value', name:'CGL', ...AXIS}}],
    series:[
      {{name:'上涨', type:'line', smooth:true, data:trend.rise,
        itemStyle:{{color:'#d64545'}}, lineStyle:{{color:'#d64545', width:2}}, areaStyle:{{opacity:.06}}}},
      {{name:'下跌', type:'line', smooth:true, data:trend.fall,
        itemStyle:{{color:'#2f8f6b'}}, lineStyle:{{color:'#2f8f6b', width:2}}, areaStyle:{{opacity:.06}}}},
      {{name:'情绪温度', type:'line', yAxisIndex:1, smooth:true, data:trend.emotion,
        itemStyle:{{color:'#c4a574'}}, lineStyle:{{color:'#c4a574', width:2, type:'dashed'}},
        symbolSize:4}}
    ] }})
}});

charts.push({{el:'chart-ladder',
  opt: base({{ xAxis:{{type:'category', data:ladder.map(x=>x.height+'板'), ...AXIS}},
    yAxis:{{type:'value', name:'家数', ...AXIS}},
    series:[{{name:'家数', type:'bar', data:ladder.map(x=>x.count),
      itemStyle:{{color:'#d64545'}},
      barMaxWidth:26}}] }})
}});

charts.push({{el:'chart-rotation',
  opt: base({{ xAxis:{{type:'value', ...AXIS}}, yAxis:{{type:'category', inverse:true,
      data:rotation.map(x=>x.name), axisLabel:{{color:'#a89880', fontSize:11}}}},
    series:[{{name:'评分', type:'bar', data:rotation.map(x=>x.score),
      itemStyle:{{color:'#c4a574'}},
      label:{{show:true, position:'right', color:'#a89880', fontSize:10}},
      barMaxWidth:14}}] }})
}});

charts.push({{el:'chart-theme',
  opt: base({{ xAxis:{{type:'value', ...AXIS}}, yAxis:{{type:'category', inverse:true,
      data:themes.map(t=>t.name), axisLabel:{{color:'#a89880', fontSize:11}}}},
    series:[{{name:'主力净额', type:'bar', data:themes.map(t=>t.main_net),
      itemStyle:{{color:'#2f8f6b'}},
      label:{{show:true, position:'right', color:'#a89880', fontSize:10,
        formatter:p=>{{let v=p.value||0; return (v>=1e8?(v/1e8).toFixed(2)+'亿':(v/1e4).toFixed(0)+'万')}}}},
      barMaxWidth:14}}] }})
}});

window.addEventListener('DOMContentLoaded', () => {{
  for (const c of charts) {{
    const el = document.getElementById(c.el);
    if (!el) continue;
    const chart = echarts.init(el);
    chart.setOption(c.opt);
    window.addEventListener('resize', () => chart.resize());
  }}
  let tabs = [...document.querySelectorAll('.concept-tab')];
  const conceptMore = document.getElementById('concept-more');
  const title = document.getElementById('concept-detail-title');
  const sub = document.getElementById('concept-detail-sub');
  const grid = document.getElementById('concept-stock-grid');
  const escHtml = value => String(value == null ? '' : value)
    .replace(/[&<>\"']/g, char => ({{'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}}[char]));
  const conceptTab = (item, index) => `<button type="button" class="concept-tab" data-concept-index="${{index}}">`
    + `<span class="concept-rank">${{String(index + 1).padStart(2, '0')}}</span>`
    + `<span class="concept-name">${{escHtml(item.concept_name || '—')}}</span>`
    + `<span class="concept-lu">${{item.limit_up_count || 0}}</span>`
    + `<span class="concept-meta">最高 ${{item.max_board == null ? '—' : item.max_board}}板 · 成分 ${{item.member_count || 0}}</span></button>`;
  let lazyReviewData = window.__REVIEW_LAZY_DATA__ || null;
  let lazyLoadPromise = null;
  let lazyLoadError = '';
  let fullConceptData = null;
  const loadFullConceptData = () => {{
    if (fullConceptData) return fullConceptData;
    const payload = document.getElementById('review-concept-data');
    if (!payload) return conceptData;
    try {{
      const parsed = JSON.parse(payload.textContent || '[]');
      if (Array.isArray(parsed)) fullConceptData = parsed;
    }} catch (err) {{
      lazyLoadError = '概念详情数据损坏，当前仅显示首屏';
    }}
    return fullConceptData || conceptData;
  }};
  const loadLazyData = () => {{
    if (!lazyAsset) return Promise.resolve(lazyReviewData);
    if (lazyReviewData) return Promise.resolve(lazyReviewData);
    if (lazyLoadPromise) return lazyLoadPromise;
    lazyLoadPromise = new Promise((resolve, reject) => {{
      const script = document.createElement('script');
      script.src = lazyAsset;
      script.async = true;
      script.onload = () => {{
        lazyReviewData = window.__REVIEW_LAZY_DATA__ || null;
        if (!lazyReviewData) {{
          lazyLoadError = '详情数据未加载：侧车文件为空';
          reject(new Error(lazyLoadError));
          return;
        }}
        resolve(lazyReviewData);
      }};
      script.onerror = () => {{
        lazyLoadError = '详情数据未加载：请确认同目录 lazy.js 文件存在';
        reject(new Error(lazyLoadError));
      }};
      document.head.appendChild(script);
    }});
    return lazyLoadPromise;
  }};
  const money = v => v == null ? '—' : (Math.abs(Number(v)) >= 1e8 ? (Number(v) / 1e8).toFixed(2) + '亿' : Number(v).toFixed(2));
  const fmtTime = v => {{
    if (v == null || v === '') return '封板时间未记录';
    const n = Number(v);
    if (Number.isFinite(n) && n > 1e9) {{
      const d = new Date(n > 1e12 ? n : n * 1000);
      return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0');
    }}
    return String(v);
  }};
  const renderConcept = async (index, expand = false) => {{
    if (expand || index > 0) {{
      conceptData = loadFullConceptData();
    }}
    const item = conceptData[index];
    if (!item || !title || !sub || !grid) return;
    tabs.forEach((tab, i) => tab.classList.toggle('active', i === index));
    title.textContent = item.concept_name || '—';
    sub.textContent = `涨停 ${{item.limit_up_count || 0}} 只 · 成分 ${{item.member_count || 0}} 只 · 主线分 ${{money(item.mainline_score)}}`;
    const allStocks = item.limit_up_stocks || [];
    const shown = expand ? allStocks : allStocks.slice(0, 50);
    const cards = shown.length ? shown.map(stock => `
      <div class="concept-stock-card">
        <span class="stock-name">${{stock.stock_name || '—'}}</span>
        <span class="stock-board">${{stock.board_level == null ? '—' : stock.board_level + '板'}}</span>
        <span class="stock-code">${{stock.stock_code || '—'}}</span>
        <span class="stock-time">${{fmtTime(stock.limit_up_time)}}</span>
       </div>`).join('') : '<div class="empty">该概念暂无同日涨停个股</div>';
    const hiddenCount = allStocks.length - shown.length;
    const more = hiddenCount > 0
      ? `<button type="button" class="trail-more" id="concept-stock-more">加载全部个股（剩余 ${{hiddenCount}} 只）</button>`
      : '';
    grid.innerHTML = cards + more;
    const moreButton = document.getElementById('concept-stock-more');
    if (moreButton) moreButton.addEventListener('click', () => renderConcept(index, true));
  }};
  const bindConceptTabs = () => {{
    tabs = [...document.querySelectorAll('.concept-tab')];
    tabs.forEach(tab => {{
      if (tab.dataset.bound) return;
      tab.dataset.bound = '1';
      tab.addEventListener('click', () => renderConcept(Number(tab.dataset.conceptIndex)));
    }});
  }};
  bindConceptTabs();
  if (conceptMore) conceptMore.addEventListener('click', () => {{
    const all = loadFullConceptData();
    const list = document.getElementById('concept-list');
    const current = tabs.length;
    if (list && all.length > current) {{
      list.insertAdjacentHTML('beforeend', all.slice(current).map((item, offset) => conceptTab(item, current + offset)).join(''));
      bindConceptTabs();
    }}
    conceptMore.remove();
  }});
  if (tabs.length) renderConcept(0);

  const trailDataEl = document.getElementById('trail-data');
  const trailPeriodEl = document.getElementById('trail-periods');
  const trailBoard = document.getElementById('trail-board');
  const trailDetail = document.getElementById('trail-detail');
  const trailDetailsEl = document.getElementById('trail-details');
  let trailData = {{dates: [], sectors: []}};
  let trailPeriods = {{week: {{}}, month: {{}}, quarter: {{}}}};
  let trailDetails = {{daily: {{}}, periods: {{week: {{}}, month: {{}}, quarter: {{}}}}, compact_overview: false}};
  if (trailDataEl) {{
    try {{ trailData = JSON.parse(trailDataEl.textContent || '{{}}'); }} catch (err) {{ trailData = {{dates: [], sectors: []}}; }}
  }}
  if (trailPeriodEl) {{
    try {{ trailPeriods = JSON.parse(trailPeriodEl.textContent || '{{}}'); }} catch (err) {{ trailPeriods = {{week: {{}}, month: {{}}, quarter: {{}}}}; }}
  }}
  if (trailDetailsEl) {{
    try {{ trailDetails = JSON.parse(trailDetailsEl.textContent || '{{}}'); }} catch (err) {{ trailDetails = {{daily: {{}}, periods: {{week: {{}}, month: {{}}, quarter: {{}}}}}}; }}
  }}
  const trailCompact = Boolean(trailDetails.compact_overview);
  const hydrateTrailSector = sector => {{
    if (!sector || sector.__trailDetailsLoaded) return sector;
    const sidecarDetail = lazyReviewData && lazyReviewData.trail_details
      ? lazyReviewData.trail_details[String(sector.id || '')] : null;
    if (sidecarDetail) {{
      sector.window_stocks = sidecarDetail.window_stocks || [];
      for (const [day, cell] of Object.entries(sidecarDetail.daily || {{}})) {{
        sector.daily = sector.daily || {{}};
        sector.daily[day] = Object.assign(sector.daily[day] || {{}}, cell);
      }}
      sector.__trailDetailsLoaded = true;
      return sector;
    }}
    const detail = (trailDetails.daily || {{}})[String(sector.id || '')];
    if (!detail) return sector;
    try {{
      sector.window_stocks = detail.window_stocks || [];
      for (const [day, cell] of Object.entries(detail.daily || {{}})) {{
        sector.daily = sector.daily || {{}};
        sector.daily[day] = Object.assign(sector.daily[day] || {{}}, cell);
      }}
      sector.__trailDetailsLoaded = true;
    }} catch (err) {{ console.warn('trail detail load failed', sector.id, err); }}
    return sector;
  }};
  const hydratePeriodSector = (kind, sector) => {{
    if (!sector || sector.__periodDetailsLoaded) return sector;
    const sidecarDetail = lazyReviewData && lazyReviewData.period_details
      && lazyReviewData.period_details[kind]
      ? lazyReviewData.period_details[kind][String(sector.id || '')] : null;
    if (sidecarDetail) {{
      for (const [label, cell] of Object.entries(sidecarDetail.periods || {{}})) {{
        sector.periods = sector.periods || {{}};
        sector.periods[label] = Object.assign(sector.periods[label] || {{}}, cell);
      }}
      sector.__periodDetailsLoaded = true;
      return sector;
    }}
    const detail = ((trailDetails.periods || {{}})[kind] || {{}})[String(sector.id || '')];
    if (!detail) return sector;
    try {{
      for (const [label, cell] of Object.entries(detail.periods || {{}})) {{
        sector.periods = sector.periods || {{}};
        sector.periods[label] = Object.assign(sector.periods[label] || {{}}, cell);
      }}
      sector.__periodDetailsLoaded = true;
    }} catch (err) {{ console.warn('period detail load failed', sector.id, err); }}
    return sector;
  }};
  let trailMode = 'day';
  let trailHistoryExpanded = false;
  let trailSelected = '';
  const trailSort = {{}};
  const TOPN = 50;
  const fmtPctJs = v => v == null || v === '' ? '—' : ((Number(v) > 0 ? '+' : '') + Number(v).toFixed(2) + '%');
  const fmtRateJs = v => v == null || v === '' ? '—' : Number(v).toFixed(2) + '%';
  const fmtFlowJs = v => {{
    if (v == null || v === '') return '—';
    const n = Number(v);
    const abs = Math.abs(n);
    if (abs >= 1e8) return (n / 1e8).toFixed(2) + '亿';
    if (abs >= 1e4) return (n / 1e4).toFixed(0) + '万';
    return n.toFixed(0);
  }};
  const signCls = v => Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : '';
  const numeric = v => v == null || v === '' || Number.isNaN(Number(v)) ? -Infinity : Number(v);
  const sortTitle = key => {{
    const mode = trailSort[key] || 'default';
    return mode === 'desc' ? '涨幅 ↓' : mode === 'asc' ? '涨幅 ↑' : '涨幅';
  }};
  const defaultTrailCompare = (a, b) =>
    numeric(b.cell.limit_up) - numeric(a.cell.limit_up)
      || numeric(b.cell.strength) - numeric(a.cell.strength)
      || numeric(b.cell.main_net) - numeric(a.cell.main_net)
      || String(a.sector.name || '').localeCompare(String(b.sector.name || ''), 'zh-CN');
  const rankedTrail = (items, key) => {{
    const ranked = [...items].sort(defaultTrailCompare);
    const mode = trailSort[key] || 'default';
    if (mode === 'desc') ranked.sort((a, b) => numeric(b.cell.pct_chg) - numeric(a.cell.pct_chg) || defaultTrailCompare(a, b));
    if (mode === 'asc') ranked.sort((a, b) => numeric(a.cell.pct_chg) - numeric(b.cell.pct_chg) || defaultTrailCompare(a, b));
    return ranked;
  }};
  const bindTrailRows = () => {{
    document.querySelectorAll('#trail-board [data-trail-id]').forEach(row => {{
      row.addEventListener('click', () => paintTrail(row.dataset.trailId));
    }});
    document.querySelectorAll('#trail-board [data-expand-col]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const rows = btn.closest('.day-rows');
        if (!rows) return;
        const hidden = rows.querySelectorAll('[data-hidden-row]');
        if (hidden.length) {{
          hidden.forEach(el => el.hidden = false);
          btn.remove();
          return;
        }}
        const card = btn.closest('.day-card');
        const day = card && card.dataset.trailDay;
        const ranked = day
          ? rankedTrail((trailData.sectors || []).map(sector => ({{sector, cell: (sector.daily || {{}})[day]}})).filter(item => item.cell), day)
          : [];
        if (ranked.length > TOPN) {{
          rows.insertAdjacentHTML('beforeend', ranked.slice(TOPN).map((item, i) => conceptRow(item.sector, item.cell, TOPN + i + 1, false, true)).join(''));
        }}
        btn.remove();
      }});
    }});
    document.querySelectorAll('#trail-board [data-trail-sort]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        const key = btn.dataset.trailSort;
        const mode = trailSort[key] || 'default';
        trailSort[key] = mode === 'default' ? 'desc' : mode === 'desc' ? 'asc' : 'default';
        renderTrailBoard();
      }});
    }});
  }};
  const conceptRow = (sector, cell, index, wide, forceVisible = false) => {{
    const hidden = index > TOPN && !forceVisible ? ' hidden data-hidden-row' : '';
    const coverage = cell.coverage_pct == null ? '' : ` · 覆盖 ${{fmtRateJs(cell.coverage_pct)}}`;
    const extra = wide
      ? `<span class="num ${{signCls(cell.main_net)}}">${{fmtFlowJs(cell.main_net)}}</span>`
      : `<span class="num ${{signCls(cell.strength)}}">${{fmtPctJs(cell.strength)}}</span>`;
    const tail = wide
      ? `<span class="num up">${{cell.limit_up ?? '—'}}</span><span class="num ${{signCls(cell.pct_chg)}}">${{fmtPctJs(cell.pct_chg)}}</span>${{extra}}`
      : `${{extra}}<span class="num up">${{cell.limit_up ?? '—'}}</span><span class="num ${{signCls(cell.pct_chg)}}">${{fmtPctJs(cell.pct_chg)}}</span>`;
    return `<button type="button" class="day-row cols5" data-trail-id="${{sector.id}}"${{hidden}}>`
      + `<span class="dim">${{index}}</span><span class="n">${{sector.name || sector.id}}<small class="dim">${{coverage}}</small></span>`
      + tail + `</button>`;
  }};
  const renderTrailBoard = () => {{
    if (!trailBoard) return;
    if (trailMode === 'day') {{
      const days = trailHistoryExpanded
        ? (trailData.dates || [])
        : (trailData.dates || []).slice(0, 1);
      trailBoard.innerHTML = days.map(day => {{
        const ranked = rankedTrail((trailData.sectors || []).map(sector => ({{sector, cell: (sector.daily || {{}})[day]}}))
          .filter(item => item.cell)
          , day);
        const rows = ranked.slice(0, TOPN).map((item, i) => conceptRow(item.sector, item.cell, i + 1, false)).join('');
        const more = ranked.length > TOPN
          ? `<button type="button" class="trail-more" data-expand-col="1">加载全部（剩余 ${{ranked.length - TOPN}} 个）</button>` : '';
        return `<div class="day-card" data-trail-day="${{day}}"><div class="day-title">${{day}}</div>`
          + `<div class="day-head cols5"><span>#</span><span>概念</span><span class="num">强度</span><span class="num">涨停</span><span class="num sort-head" data-trail-sort="${{day}}">${{sortTitle(day)}}</span></div>`
          + `<div class="day-rows">${{rows}}${{more}}</div></div>`;
      }}).join('') + (!trailHistoryExpanded && (trailData.dates || []).length > 1
        ? '<button type="button" class="trail-more" id="trail-history-more">加载历史交易日</button>' : '');
    }} else {{
      const pack = trailPeriods[trailMode] || {{}};
      const frames = pack.periods || [];
      const sectors = pack.sectors || [];
      trailBoard.innerHTML = frames.map(frame => {{
        const ranked = rankedTrail(sectors.map(sector => ({{sector, cell: (sector.periods || {{}})[frame.label]}}))
          .filter(item => item.cell)
          , frame.label);
        const rows = ranked.slice(0, TOPN).map((item, i) => conceptRow(item.sector, item.cell, i + 1, true)).join('');
        const more = ranked.length > TOPN
          ? `<button type="button" class="trail-more" data-expand-col="1">加载全部（剩余 ${{ranked.length - TOPN}} 个）</button>` : '';
        return `<div class="day-card wide"><div class="day-title">${{frame.label}}`
          + `<small>${{frame.start || ''}} ~ ${{frame.end || ''}} · ${{frame.trading_days || 0}}个交易日</small></div>`
          + `<div class="day-head cols5"><span>#</span><span>概念</span><span class="num">涨停</span><span class="num sort-head" data-trail-sort="${{frame.label}}">${{sortTitle(frame.label)}}</span><span class="num">净流入</span></div>`
          + `<div class="day-rows">${{rows}}${{more}}</div></div>`;
      }}).join('') || '<div class="empty">暂无期间数据</div>';
    }}
    bindTrailRows();
    const historyMore = document.getElementById('trail-history-more');
    if (historyMore) historyMore.addEventListener('click', () => {{
      trailHistoryExpanded = true;
      renderTrailBoard();
    }});
    if (trailSelected) paintTrail(trailSelected);
  }};
  const paintTrail = async id => {{
    trailSelected = id;
    document.querySelectorAll('#trail-board [data-trail-id]').forEach(row => row.classList.toggle('active', row.dataset.trailId === id));
    if (!trailDetail) return;
    if (lazyAsset) {{
      try {{ await loadLazyData(); }}
      catch (err) {{
        trailDetail.innerHTML = `<div class="empty">${{lazyLoadError || '详情数据未加载'}}</div>`;
        return;
      }}
    }}
    if (trailMode === 'day') {{
      const sector = (trailData.sectors || []).find(item => item.id === id);
      if (!sector) return;
      hydrateTrailSector(sector);
      // A stock may hit limit-up on multiple dates in the review window.
      // Keying by stock_code alone let a later date overwrite the current
      // day's board/time annotation and rendered a valid value as "—".
      const luByDateCode = {{}};
      for (const [day, cell] of Object.entries(sector.daily || {{}})) {{
        for (const st of (cell.stocks || [])) {{
          luByDateCode[`${{day}}|${{st.stock_code}}`] = {{ board: st.board_level, time: st.limit_up_time }};
        }}
      }}
      const panel = sector.members;
      const renderDetailRows = (day, stocks) => stocks.map((stock, index) => {{
        const lu = luByDateCode[`${{day}}|${{stock.code}}`] || null;
        const board = lu && lu.board != null ? lu.board + '板' : '—';
        const time = lu && lu.time ? lu.time : '—';
        return `<div class="day-row cols5 stock-detail-row" style="cursor:default"><span class="dim">${{index + 1}}</span>`
          + `<span class="n">${{stock.name || stock.code || '—'}} <span class="dim">${{stock.code || ''}}</span></span>`
          + `<span class="num ${{signCls(stock.pct)}}">${{fmtPctJs(stock.pct)}}</span>`
          + `<span class="num">${{board}}</span>`
          + `<span class="num">${{time}}</span></div>`;
      }}).join('') || '<div class="empty">当日无成分数据</div>';
      const memberPanel = panel ? {{
        codes: panel.codes || [],
        pct: trailData.stock_pct || {{}},
      }} : null;
      const namesMap = trailData.stock_names || {{}};
      const dayStocks = day => {{
        if (!memberPanel) return ((sector.daily || {{}})[day] || {{}}).stocks || [];
        if (!Object.prototype.hasOwnProperty.call(memberPanel.pct, day)) {{
          return (((sector.daily || {{}})[day] || {{}}).stocks || []).map(stock => ({{
            code: stock.stock_code,
            name: stock.stock_name || stock.stock_code,
            pct: stock.pct_chg,
          }}));
        }}
        const col = memberPanel.pct[day] || {{}};
        return memberPanel.codes.map(code => ({{
          code,
          name: namesMap[code] || code,
          pct: col[code] == null ? null : col[code] / 100,
        }})).sort((a, b) => (b.pct == null ? -9999 : b.pct) - (a.pct == null ? -9999 : a.pct));
      }};
      const cards = (trailData.dates || []).map(day => {{
        const cell = (sector.daily || {{}})[day] || {{}};
        const stocks = dayStocks(day);
        const limitUpCount = cell.limit_up == null ? ((cell.stocks || []).length) : cell.limit_up;
        const rows = renderDetailRows(day, stocks.slice(0, TOPN));
        const more = stocks.length > TOPN
          ? `<button type="button" class="trail-more" data-day-more="${{day}}">加载全部（剩余 ${{stocks.length - TOPN}} 个）</button>` : '';
        return `<div class="day-card"><div class="day-title">${{day}} · 全部成分 ${{stocks.length}}只 · 涨停 ${{limitUpCount}}只</div>`
          + `<div class="day-head cols5 stock-detail-head"><span>#</span><span>个股</span><span class="num">涨幅</span><span class="num">连板</span><span class="num">封板时间</span></div>`
          + `<div class="day-rows" data-day-rows="${{day}}">${{rows}}${{more}}</div></div>`;
      }}).join('');
      const latestCell = (sector.daily || {{}})[(trailData.dates || [])[0]] || {{}};
      const coverage = latestCell.coverage_pct == null ? '' : ` · 成分 K 线覆盖 ${{fmtRateJs(latestCell.coverage_pct)}}`;
      const snapNote = memberPanel ? ` · 成分快照 ${{panel.snapshot_date || '—'}}` : '';
      const compactNote = trailCompact ? ' · 总览仅内嵌最新日全部成分，历史日保留涨停明细；全量成分请进入全宽专页' : '';
      trailDetail.innerHTML = `<div class="sec-title"><strong>${{sector.name || id}}</strong>`
        + `<span class="dim">成分按涨幅排序，涨停股标注连板与封板时间 · 一只票可以同时属于多个概念${{coverage}}${{snapNote}}${{compactNote}}</span></div>`
        + `<div class="trail-scroll">${{cards}}</div>`;
      document.querySelectorAll('#trail-detail [data-day-more]').forEach(button => button.addEventListener('click', () => {{
        const day = button.dataset.dayMore;
        const stocks = dayStocks(day);
        const rows = document.querySelector(`#trail-detail [data-day-rows="${{day}}"]`);
        if (rows) rows.innerHTML = renderDetailRows(day, stocks);
        button.remove();
      }}));
      return;
    }}
    const pack = trailPeriods[trailMode] || {{}};
    const sector = (pack.sectors || []).find(item => item.id === id);
    if (!sector) {{
      trailDetail.innerHTML = `<div class="empty">该概念在${{trailMode === 'week' ? '周' : trailMode === 'month' ? '月' : '季'}}度没有进入前排</div>`;
      return;
    }}
    hydratePeriodSector(trailMode, sector);
    const cards = (pack.periods || []).map(frame => {{
      const cell = (sector.periods || {{}})[frame.label] || {{}};
      const stocks = [...(cell.stocks || [])];
      const rows = stocks.length ? stocks.map((stock, index) => {{
        const hidden = index >= TOPN ? ' hidden data-hidden-row' : '';
        return `<div class="day-row cols5 stock-detail-row" style="cursor:default"${{hidden}}><span class="dim">${{index + 1}}</span>`
          + `<span class="n">${{stock.stock_name || stock.stock_code || '—'}} <span class="dim">${{stock.stock_code || ''}}</span></span>`
          + `<span class="num">${{stock.limit_up ?? '—'}}</span>`
          + `<span class="num ${{signCls(stock.pct_chg)}}">${{fmtPctJs(stock.pct_chg)}}</span>`
          + `<span class="num">${{stock.board_level == null ? '—' : stock.board_level + '板'}}</span></div>`;
      }}).join('') : (trailCompact
        ? '<div class="empty">总览页仅保留周/月/季汇总；期间个股明细请点击上方“进入全宽专页”。</div>'
        : '<div class="empty">该期无涨停</div>');
      const more = stocks.length > TOPN
        ? `<button type="button" class="trail-more" data-expand-col="1">加载全部（剩余 ${{stocks.length - TOPN}} 个）</button>` : '';
      return `<div class="day-card wide"><div class="day-title">${{frame.label}} · ${{cell.limit_up || 0}}次`
        + `<small>区间涨幅 ${{fmtPctJs(cell.pct_chg)}} · 净流入 ${{fmtFlowJs(cell.main_net)}}</small></div>`
        + `<div class="day-head cols5 stock-detail-head"><span>#</span><span>个股</span><span class="num">涨停次数</span><span class="num">区间收益</span><span class="num">最高板</span></div>`
        + `<div class="day-rows">${{rows}}${{more}}</div></div>`;
    }}).join('');
    trailDetail.innerHTML = `<div class="sec-title"><strong>${{sector.name || id}}</strong>`
      + `<span class="dim">期间内涨停个股 · 涨停=该窗口内涨停次数，区间收益=窗口首收至末收</span></div>`
      + `<div class="trail-scroll">${{cards}}</div>`;
    document.querySelectorAll('#trail-detail [data-expand-col]').forEach(btn => {{
      btn.addEventListener('click', () => {{
        btn.closest('.day-rows').querySelectorAll('[data-hidden-row]').forEach(el => el.hidden = false);
        btn.remove();
      }});
    }});
  }};
  document.querySelectorAll('#trail-mode-tabs [data-trail-mode]').forEach(btn => {{
    btn.addEventListener('click', () => {{
      trailMode = btn.dataset.trailMode;
      document.querySelectorAll('#trail-mode-tabs [data-trail-mode]').forEach(item => item.classList.toggle('active', item === btn));
      renderTrailBoard();
    }});
  }});
  bindTrailRows();
  renderTrailBoard();

  document.querySelectorAll('[data-tabs]').forEach(group => {{
    const buttons = [...group.querySelectorAll('[data-tab]')];
    const root = group.parentElement;
    if (!root) return;
    buttons.forEach(button => button.addEventListener('click', () => {{
      const key = button.dataset.tab;
      buttons.forEach(item => item.classList.toggle('active', item === button));
      root.querySelectorAll(':scope > .tab-panel').forEach(panel => {{
        panel.classList.toggle('active', panel.dataset.panel === key);
      }});
    }}));
  }});
}});
"""


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def _top_nav(active: str) -> str:
    """Shared top navigation across the multi-page report set."""
    items = [
        ("daily_review_latest.html", "总览", "overview"),
        ("sector_trail_latest.html", "板块轨迹", "trail"),
        ("trading_dashboard_latest.html", "仪表盘", "dashboard"),
        ("trading_terminal_latest.html", "终端", "terminal"),
    ]
    links = "".join(
        f"<a href='{href}' class='tn-link{' active' if key == active else ''}'"
        f"{' target=_blank rel=noopener' if key in ('dashboard', 'terminal') else ''}"
        f">{label}</a>"
        for href, label, key in items
    )
    return f"<nav class='topnav'>{links}</nav>"


_TRAIL_PAGE_CSS = """
/* standalone overrides on top of the main design system (_CSS loads first) */
.wrap{max-width:1840px}
body{background:#0f1420;color:#dfe6f2;font-family:'Segoe UI','Microsoft YaHei',sans-serif;margin:0}
.topnav{display:flex;gap:18px;align-items:center;padding:14px 28px;background:#131a2a;
position:sticky;top:0;z-index:50;border-bottom:1px solid #26304a}
.topnav .brand{font-weight:800;letter-spacing:2px;color:#7fb2ff;margin-right:12px}
.tn-link{color:#9fb0cc;text-decoration:none;font-size:14px;padding:4px 10px;border-radius:6px}
.tn-link.active{color:#fff;background:#223055}
.wrap{max-width:1840px;margin:0 auto;padding:20px 28px}
.page-title{font-size:24px;font-weight:700;margin:6px 0 2px}
.page-sub{color:#8fa1c0;font-size:13px;margin-bottom:16px}
.sec-title{font-size:17px;font-weight:700;margin:10px 0 10px}
.detail-item{margin-top:10px}
.d-label{color:#8fa1c0;font-size:12px}
.d-value{font-size:15px}
.amber{color:#ffb454}
.concept-footnote{color:#7484a3;font-size:11px;line-height:1.7;margin-top:14px;
border-top:1px dashed #26304a;padding-top:12px}
"""


def _render_sector_trail_standalone(
    ctx: dict[str, Any], trade_date: str, echarts_src: str,
    trend: dict[str, Any], ladder: list[dict[str, Any]],
    rotation: list[dict[str, Any]],
    concept_limit_up: dict[str, Any] | None = None,
    extras_html: str = "",
) -> str:
    """Desktop-width standalone 板块轨迹 page.

    Reuses the same ``_render_sector_trail`` fragment and ``_chart_js``
    pipeline as the overview page so every interaction stays identical —
    only the layout (full-width, dark, desktop grid) is its own.
    """
    from trade_system.cycle import PHASE_CN

    trail_html = _render_sector_trail(ctx)
    phase_row = ctx.get("cycle_phase") or {}
    chart_js = _chart_js(ctx, trend, ladder, rotation, concept_limit_up)
    phase_txt = (
        f"{PHASE_CN.get(phase_row.get('phase'), phase_row.get('phase', '—'))}"
        f" · 温度 {phase_row.get('score', '—')}"
        if isinstance(phase_row, dict) and phase_row else "—"
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>板块轨迹 · {_e(trade_date)}</title>
<style>{_CSS}</style>
<style>{_TRAIL_PAGE_CSS}</style>
</head>
<body>
{_top_nav("trail")}
<main class="wrap">
  <div class="page-title">板块轨迹 <span style="color:#8fa1c0;font-size:14px">· {_e(trade_date)}</span></div>
  <div class="page-sub">当前相位：{phase_txt} ｜ 日 / 周 / 月 / 季 四个窗口自由切换 · 全部数据来自本地 DuckDB · research-only</div>
  <section id="s-trail" data-screen-label="trail">
    {trail_html}
    <div class="concept-footnote">口径：当日涨停且属于该概念/行业/同花顺板块的个股；涨停时间早的排前。一只股票可同时属于多个概念。历史窗口内某日缺失表示当日无该板块涨停或数据未覆盖，不代表没有行情。</div>
  </section>
  {extras_html}
</main>
<script>{echarts_src}</script>
<script>{chart_js}</script>
</body>
</html>
"""


def _render_support_page(
    ctx: dict[str, Any],
    trade_date: str,
    extras_html: str,
    staleness_banner: str = "",
) -> str:
    """Render machine-facing evidence and historical support sections.

    The daily page is intentionally small and decision-oriented.  This page
    keeps the detailed tables, QLib shadow output, source checkpoints and
    auxiliary history available as a separate self-contained artifact.
    """
    lhb = _render_lhb(ctx)
    tables = _render_tables(ctx)
    qlib_research = _render_qlib_research(ctx)
    gates = _render_data_gates(ctx)
    plans = _render_plans(ctx)
    generated = str(ctx.get("page_generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    data_as_of = str(
        ctx.get("data_as_of")
        or (ctx.get("readiness") or {}).get("as_of")
        or trade_date
    )
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>复盘证据与系统附录 · {_e(trade_date)}</title>
<style>{_CSS}</style>
</head>
<body>
{_top_nav("support")}
<main class="wrap">
  <header class="support-head">
    <div class="page-title">复盘证据与系统附录 · {_e(trade_date)}</div>
    <div class="page-sub">生成 { _e(generated) } · 数据截止 { _e(data_as_of) } · 详细证据不直接参与交易门禁</div>
  </header>
  {staleness_banner}
  <div class="notice">本页用于核对数据来源、辅助历史、QLib shadow、龙虎榜和操作记录。若与日复盘摘要冲突，以当前日期、来源和门禁状态为准。</div>
  <section><div class="sec-title"><strong>龙虎榜</strong></div>{lhb}</section>
  <section><div class="sec-title"><strong>资金明细</strong><span class="dim">个股 / 板块 / 行业 / 持续性</span></div>{tables}</section>
  <section><div class="sec-title"><strong>QLib 研究区</strong><span class="dim">shadow / research-only</span></div>{qlib_research}</section>
  <section><div class="sec-title"><strong>数据门禁</strong></div>{gates}</section>
  <section><div class="sec-title"><strong>操作计划</strong></div>{plans}</section>
  {extras_html}
  <div class="footer">复盘证据与系统附录 · {_e(trade_date)} · 静态自包含 HTML</div>
</main>
</body>
</html>
"""


def _staleness_banner(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    """Red banner when core sources lag the review date.

    A missing block must never look like a quiet zero.  This is the page's
    single place where stale/missing upstream data is made loud.
    """
    core_checks = [
        ("涨停池", "v_limit_pool", "trade_date"),
        ("K线", "v_kline_daily", "trade_date"),
        ("个股资金流", "multi_source_stock_flow", "source_date"),
        ("板块资金流", "multi_source_sector_flow", "source_date"),
        ("同花顺概念快照", "v_default_concept_daily", "trade_date"),
    ]
    supplemental_checks = [
        ("竞价证据", "auction_evidence_snapshot", "trade_date"),
        ("龙虎榜", "v_lhb_daily", "trade_date"),
        ("指数行情", "v_index_state", "trade_date"),
        ("官方筹码", "xdf_cyq_perf", "trade_date"),
        ("两融", "xdf_margin_summary", "trade_date"),
        ("盘前数据", "xdf_stk_premarket", "trade_date"),
    ]
    def stale_items(checks: list[tuple[str, str, str]]) -> list[str]:
        stale: list[str] = []
        for label, table, col in checks:
            try:
                row = con.execute(
                    f"SELECT max(CAST({col} AS DATE)) FROM {table}"
                ).fetchone()
                latest = row[0] if row else None
                if latest is None or str(latest) < trade_date:
                    have = str(latest) if latest else "无数据"
                    stale.append(f"{label}（最新 {have}）")
            except Exception:
                stale.append(f"{label}（表缺失）")
        return stale

    core_stale = stale_items(core_checks)
    supplemental_stale = stale_items(supplemental_checks)
    banners: list[str] = []
    if core_stale:
        items = "、".join(core_stale)
        banners.append(
            "<div class='stale-banner' style='background:#5a1a1a;border:1px solid #d64545;"
            "color:#ffd7d7;border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13px'>"
            "<strong>⚠ 核心数据断档：</strong>以下数据未覆盖复盘日，相关结论不可作为当日事实"
            f" —— {items}</div>"
        )
    if supplemental_stale:
        items = "、".join(supplemental_stale)
        banners.append(
            "<div class='stale-banner' style='background:#4a3512;border:1px solid #b7832f;"
            "color:#ffe3ad;border-radius:8px;padding:10px 14px;margin:14px 0;font-size:13px'>"
            "<strong>△ 补充模块降级：</strong>下列模块仅展示其最新已持久化批次，不参与当日执行门禁"
            f" —— {items}</div>"
        )
    return "".join(banners)


def _page_html(ctx: dict[str, Any], trade_date: str, echarts_src: str,
               trend: dict[str, Any], ladder: list[dict[str, Any]],
               rotation: list[dict[str, Any]],
               lazy_asset_name: str | None = None,
               staleness_banner: str = "") -> str:
    ctx["narrative"] = ctx.get("narrative") or build_review_narrative(ctx)
    kpis = _render_kpis(ctx)
    verdict = _render_command_summary(ctx)
    themes = _render_themes(ctx)
    concept_limit_up = ctx.get("concept_limit_up") or {}
    concept_drilldown = _render_concept_limit_up(ctx, inline_stock_limit=50)
    stages = _render_stages(ctx)
    alerts = _render_alerts(ctx)
    loop = _render_loop(ctx)
    flow_compact = _render_flow_compact(ctx)
    tomorrow = _render_tomorrow(ctx)
    named_ladder = _render_named_ladder(ctx)
    yday_limitup = _render_yday_limitup(ctx)
    broken = _render_broken(ctx)
    sector_trail = _render_sector_trail(
        ctx,
        external_lazy=bool(lazy_asset_name),
        compact_overview=True,
    )
    # Keep only the first 50 members in the live JS object.  The complete
    # concept catalogue is embedded as inert JSON below and parsed only when
    # the user opens a later concept or clicks "加载全部个股".  This preserves
    # the self-contained-file contract without building hundreds of cards at
    # initial load.
    # The server-rendered first card is enough for the initial JS state. The
    # complete concept payload remains inert and is parsed only after the user
    # switches concepts, so metadata is not duplicated in executable JS.
    concept_data = _concept_inline_data(ctx, stock_limit=50)[:1]
    concept_payload_json = json.dumps(
        concept_limit_up.get("groups") or [],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).replace("</", "<\\/")
    consecutive = ctx.get("consecutive")
    generated = str(ctx.get("page_generated_at") or datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    data_as_of = str(
        ctx.get("data_as_of")
        or (ctx.get("readiness") or {}).get("as_of")
        or trade_date
    )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>盘后复盘 · {_e(trade_date)}</title>
<style>{_CSS}</style>
</head>
<body>

{_top_nav("overview")}
<header class="masthead">
  <div class="mast-mark">盘后复盘</div>
  <nav class="toc">
    <a href="#s-decision">结论</a>
    <a href="#s-market">市场</a>
    <a href="#s-themes">题材</a>
    <a href="#s-confirm">确认</a>
    <a href="#s-next">次日</a>
    <a href="#s-appendix">附录</a>
  </nav>
  <div class="mast-date"><span class="mono">{_e(trade_date)}</span><span id="gen-note"></span><small class="dim">数据截止 {_e(data_as_of)}</small></div>
</header>

<main class="wrap">

  {staleness_banner}

  <div class="review-band" id="s-decision" data-band-title="结论">
    <div class="band-label">01 · 结论</div>
    {verdict}
  </div>

  <div class="review-band" id="s-market" data-band-title="市场">
    <div class="band-label">02 · 市场</div>
  <section id="s-weather" data-screen-label="weather">
    <div class="sec-title"><strong>市场天气</strong><span class="sec-kicker">宽度 · 情绪 · 连板</span></div>
    <div class="ticker">{kpis}</div>
    <div class="grid g-2" style="margin-top:18px">
      <div class="chart chart-lg" id="chart-breadth"></div>
      <div class="chart chart-lg" id="chart-emotion"></div>
    </div>
  </section>

  <section id="s-ladder" data-screen-label="ladder">
    <div class="sec-title"><strong>连板梯队</strong><span class="sec-kicker">点出名 · 不是只看柱子</span></div>
    <div class="grid g-main">
      <div>{named_ladder}</div>
      <div>
        <div class="chart chart-sm" id="chart-ladder"></div>
        <div class="detail-item"><div class="d-label">当前最高连板</div>
          <div class="d-value"><span class="amber mono">{_e(consecutive if consecutive is not None else '—')}</span> 板</div></div>
        <div class="detail-item"><div class="d-label">梯队怎么读</div>
          <div class="d-value"><small>1 板到最高板逐级递减，接力还在；中间断档，情绪已经受伤。</small></div></div>
      </div>
    </div>
  </section>

  <section id="s-yday" data-screen-label="yday">
    <div class="sec-title"><strong>昨日涨停今日表现</strong><span class="sec-kicker">打板环境 · 亏钱来源</span></div>
    {yday_limitup}
    <div class="sec-title" style="margin-top:22px"><strong>接力失败</strong><span class="dim">昨 2 板+ 今日未封</span></div>
    {broken}
  </section>
  </div>

  <div class="review-band" id="s-themes" data-band-title="题材">
    <div class="band-label">03 · 题材</div>
  <section id="s-trail" data-screen-label="trail">
    <div class="sec-title"><strong>板块轨迹</strong><span class="sec-kicker">日 / 周 / 月 / 季 · 四个窗口看轮动</span>
      <a href="sector_trail_latest.html" target="_blank" rel="noopener"
         style="float:right;font-size:13px;color:#7fb2ff;text-decoration:none">
        ↗ 进入全宽专页（桌面布局）</a></div>
    {sector_trail}
    <div class="concept-footnote">日：当日涨停只数。周/月/季：窗口内涨停次数、概念资金涨幅、资金净流入合计；下方个股区间收益为窗口首收至末收。一只票可以同时属于多个概念。过宽概念已排除。</div>
  </section>

  <section id="s-mainline" data-screen-label="mainline">
    <div class="sec-title"><strong>题材 → 涨停</strong><span class="sec-kicker">主线证据 · 同日涨停池</span></div>
    {concept_drilldown}
    <div class="grid g-main" style="margin-top:22px">
      <div>
        <div class="sec-title"><strong>题材评分</strong></div>
        {themes}
      </div>
      <div>
        <div class="sec-title"><strong>轮动 / 净额</strong></div>
        <div class="chart chart-sm" id="chart-rotation"></div>
        <div class="chart chart-sm" id="chart-theme"></div>
      </div>
    </div>
  </section>
  </div>

  <div class="review-band" id="s-confirm" data-band-title="确认">
    <div class="band-label">04 · 确认</div>
  <section id="s-flow" data-screen-label="flow">
    <div class="sec-title"><strong>资金确认</strong><span class="sec-kicker">只看确认，不看全表</span></div>
    {flow_compact}
  </section>

  <section id="s-loop" data-screen-label="loop">
    <div class="sec-title"><strong>操作闭环</strong><span class="sec-kicker">计划 · 成交 · 观察 · 日志</span></div>
    {loop}
    <div class="sec-title" style="margin-top:22px"><strong>四阶段漏斗</strong></div>
    {stages}
  </section>
  </div>

    <section id="s-next" class="tomorrow" data-screen-label="tomorrow">
    <div class="band-label">05 · 次日</div>
    <div class="sec-title"><strong>明天只看这几件事</strong><span class="sec-kicker">约法三章，防止乱开仓</span></div>
    {tomorrow}
    <div class="sec-title" style="margin-top:22px"><strong>风险告警</strong></div>
    {alerts}
  </section>

  <!--EXTRAS-->

  <details class="appendix" id="s-appendix">
    <summary>证据与系统附录<span>龙虎榜 · 资金全表 · QLib · 门禁 · 辅助历史</span></summary>
    <div class="notice">详细证据已从日页移出，避免陈旧辅助数据与当日结论混排。</div>
    <p style="margin-top:10px"><a href="review_support_latest.html" target="_blank" rel="noopener"
      style="color:#7fb2ff;text-decoration:none">↗ 打开复盘证据与系统附录</a></p>
  </details>

  <div class="footer">
    盘后复盘 · {_e(trade_date)} · 静态自包含 HTML · 仅作复盘参考，不构成交易建议
  </div>
</main>

<script>{echarts_src}</script>
<script type="application/json" id="review-concept-data">{concept_payload_json}</script>
<script>{_chart_js(ctx, trend, ladder, rotation, concept_limit_up, lazy_asset_name=lazy_asset_name, concept_data=concept_data)}</script>
<script>document.getElementById('gen-note').textContent = '生成 {generated}';</script>
</body>
</html>
"""


def _render_review_bundle(
    db_path: str | Path,
    trade_date: str | None = None,
    echarts_path: str | Path | None = None,
    lazy_asset_name: str | None = None,
    trail_out: str | Path | None = None,
    context: dict[str, Any] | None = None,
    as_of: datetime | str | None = None,
) -> tuple[str, str, str | None]:
    """Build HTML and, when requested, the optional same-directory sidecar."""
    if echarts_path is None:
        echarts_path = Path(__file__).resolve().parents[1] / "trade_system" / "vendor" / "echarts.min.js"
    try:
        echarts_src = Path(echarts_path).read_text(encoding="utf-8")
    except Exception:
        echarts_src = "/* echarts unavailable */"
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        selected = trade_date or (context or {}).get("trade_date") or _latest_date(con)
        ctx = (
            dict(context)
            if context is not None
            else build_daily_review_context(db_path, selected, con=con, as_of=as_of)
        )
        from trade_system import review_extras
        ctx["extras_sections"] = review_extras.render_all(db_path, selected)
        ctx["page_generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        ctx["data_as_of"] = str((ctx.get("readiness") or {}).get("as_of") or selected)
        page_facts = build_review_page_facts(con, selected)
        trend = page_facts["trend"]
        ladder = page_facts["ladder"]
        rotation = page_facts["rotation"]
        ctx.update({
            "theme_mainline": page_facts["theme_mainline"],
            "candidate_flow": page_facts["candidate_flow"],
            "emotion_rows": page_facts["emotion_rows"],
            "consecutive": page_facts["consecutive"],
            "metric_contract": page_facts["metric_contract"],
        })
        html_out = _page_html(
            ctx,
            selected,
            echarts_src,
            trend,
            ladder,
            rotation,
            lazy_asset_name=lazy_asset_name,
            staleness_banner=_staleness_banner(con, selected),
        ).replace("<!--EXTRAS-->", "")

        # Final-pass i18n: replace internal field names with Chinese labels
        # regardless of which module generated them.
        _FIELD_CN_MAP = {
            "sector_capital_flow": "板块资金流",
            "stock_flow": "个股资金流",
            "kline_data": "K线数据",
            "auction_evidence": "竞价证据",
            "lhb_review_evidence": "龙虎榜",
            "readiness_snapshot": "就绪快照",
        }
        for _en, _cn in _FIELD_CN_MAP.items():
            html_out = html_out.replace(_en, _cn)

        lazy_out = _build_review_lazy_asset(ctx) if lazy_asset_name else None
        if trail_out:
            support_page = _render_support_page(
                ctx,
                selected,
                ctx.get("extras_sections") or "",
                _staleness_banner(con, selected),
            )
            support_path = Path(trail_out).parent / "review_support_latest.html"
            support_path.write_text(support_page, encoding="utf-8")
            from trade_system.cycle import PHASE_CN  # noqa: F401 (page CSS/JS refs)

            phase_row = con.execute(
                """SELECT phase, score FROM market_cycle_phase
                   WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 1""",
                [selected],
            ).fetchone()
            ctx["cycle_phase"] = (
                {"phase": phase_row[0],
                 "score": float(phase_row[1]) if phase_row and phase_row[1] is not None else None}
                if phase_row else {}
            )
            trail_page = _render_sector_trail_standalone(
                ctx, selected, echarts_src, trend, ladder, rotation,
                concept_limit_up=ctx.get("concept_limit_up") or {},
                extras_html=ctx.get("extras_sections") or "",
            )
            Path(trail_out).parent.mkdir(parents=True, exist_ok=True)
            Path(trail_out).write_text(trail_page, encoding="utf-8")
    finally:
        con.close()
    return html_out, selected, lazy_out


def render_review_web(db_path: str | Path, trade_date: str | None = None,
                      echarts_path: str | Path | None = None) -> tuple[str, str]:
    """Build the inline-compatible review page and return (html, trade_date)."""
    html_out, selected, _ = _render_review_bundle(db_path, trade_date, echarts_path)
    return html_out, selected


def write_review_web(db_path: str | Path, out_path: str | Path,
                     trade_date: str | None = None,
                     *, allow_direct_publish: bool = False,
                     context: dict[str, Any] | None = None,
                     as_of: datetime | str | None = None) -> Path:
    """Render the review page and write it to ``out_path``. Returns the path.

    The page is fully self-contained: trail/concept drill-down details are
    inlined instead of loaded from a sidecar script.  The dynamic sidecar
    injection silently blanked the 板块轨迹 stock panel whenever the extra
    file was missing, blocked, or slow — a static local report must not
    depend on a second fetch to render its core sections.
    """
    out = Path(out_path)
    project_reports = Path(__file__).resolve().parents[1] / "reports"
    if (
        not allow_direct_publish
        and out.resolve().parent == project_reports.resolve()
        and out.name in {"daily_review_latest.html", "sector_trail_latest.html"}
    ):
        raise ValueError(
            "latest review files must be written through the integrated pipeline; "
            "use a staging/preview path or explicitly allow direct publish"
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    html_out, selected, _lazy_out = _render_review_bundle(
        db_path,
        trade_date,
        lazy_asset_name=None,
        trail_out=out.parent / "sector_trail_latest.html",
        context=context,
        as_of=as_of,
    )
    out.write_text(html_out, encoding="utf-8")
    return out
