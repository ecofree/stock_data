"""Detailed post-market review page renderer (HTML + ECharts).

Consumes the same context as the markdown daily review
(``trade_system.daily_review.build_daily_review_context``) and enriches it
with 30-session historical trend series so the review page reads as a
visual post-mortem: market emotion cycle, breadth, limit-up ecology, theme
mainline, capital-flow ranks, the four-stage candidate funnel, plan
execution and data-quality gates.

The page is a self-contained static HTML file (vendored ECharts, no CDN), so
it can be opened offline or served from any static host.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import duckdb

from trade_system.daily_review import build_daily_review_context, _latest_date
from trade_system.quality import table_exists


# ---------------------------------------------------------------------------
# Extra trend-series queries (history beyond the single review day)
# ---------------------------------------------------------------------------

def _trend_series(con: duckdb.DuckDBPyConnection, trade_date: str) -> dict[str, Any]:
    """Build 30-session history used by the emotion/limit-up charts."""
    trend: dict[str, Any] = {
        "dates": [], "limit_up": [], "limit_down": [], "broken_rate": [],
        "rise": [], "fall": [], "emotion": [], "consecutive": [],
    }
    try:
        rows = con.execute(
            """
            WITH latest_r AS (
                SELECT * FROM market_rise_fall
                QUALIFY row_number() OVER (PARTITION BY date ORDER BY updated_at DESC) = 1
            ), latest_s AS (
                SELECT * FROM daily_summary
                QUALIFY row_number() OVER (PARTITION BY date ORDER BY fetched_at DESC) = 1
            ), latest_e AS (
                SELECT * FROM market_emotion_money
                QUALIFY row_number() OVER (PARTITION BY date ORDER BY fetched_at DESC) = 1
            )
            SELECT r.date, r.limit_up_count, r.limit_down_count,
                   r.broken_limit_up_count, r.blown_limit_up_rate,
                   s.rise_count, s.fall_count, s.consecutive_count,
                   e.cgl
            FROM latest_r r
            LEFT JOIN latest_s s USING (date)
            LEFT JOIN latest_e e USING (date)
            WHERE r.date <= CAST(? AS DATE)
            ORDER BY r.date DESC
            LIMIT 30
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        rows = []
    for row in reversed(rows):
        trend["dates"].append(str(row[0]))
        trend["limit_up"].append(row[1])
        trend["limit_down"].append(row[2])
        trend["broken_rate"].append(round(float(row[4]), 2) if row[4] is not None else None)
        trend["rise"].append(row[5])
        trend["fall"].append(row[6])
        trend["consecutive"].append(row[7])
        trend["emotion"].append(round(float(row[8]), 2) if row[8] is not None else None)
    return trend


def _limit_ladder(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    """Board ladder (连板高度分布) for the review day.

    ``ladder_market`` is a normalized per-stock table (board_level /
    consecutive_days); aggregate by height for the distribution chart.  When
    the table predates that schema, fall back to the raw_json ladder payload.
    """
    if not table_exists(con, "ladder_market"):
        return []
    try:
        cols = [r[0] for r in con.execute("DESCRIBE ladder_market").fetchall()]
    except Exception:
        cols = []
    if "consecutive_days" in cols:
        try:
            rows = con.execute(
                """
                SELECT consecutive_days, count(*) FROM ladder_market
                WHERE date = CAST(? AS DATE)
                GROUP BY consecutive_days ORDER BY consecutive_days
                """,
                [trade_date],
            ).fetchall()
            return [{"height": int(r[0]), "count": int(r[1])} for r in rows if r[0] is not None]
        except Exception:
            return []
    try:
        rows = con.execute(
            """
            SELECT raw_json FROM ladder_market
            WHERE date = CAST(? AS DATE)
            ORDER BY fetched_at DESC LIMIT 1
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    if not rows:
        return []
    try:
        payload = json.loads(rows[0][0])
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    ladder = payload.get("ladder") if isinstance(payload, dict) else None
    out: list[dict[str, Any]] = []
    if isinstance(ladder, list):
        for item in ladder:
            if not isinstance(item, dict):
                continue
            height = item.get("height") or item.get("连板数") or item.get("板数")
            count = item.get("count") or item.get("数量") or item.get("total")
            if height is not None and count is not None:
                out.append({"height": int(height), "count": int(count)})
    if not out and isinstance(payload, dict):
        # fallback: ladder keyed by height
        for key, value in payload.items():
            if key in {"date", "is_realtime", "statistics", "broken_stocks", "height_marks"}:
                continue
            try:
                out.append({"height": int(key), "count": int(value)})
            except (TypeError, ValueError):
                continue
    return sorted(out, key=lambda item: item["height"])


def _theme_mainline(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    """Theme mainline evidence (v_theme_mainline_evidence) for the day."""
    if not table_exists(con, "v_theme_mainline_evidence"):
        return []
    try:
        rows = con.execute(
            """
            SELECT sector_code, sector_name, strength_value, seal_rate,
                   main_net_inflow, component_count, boom_reason, mainline_score
            FROM v_theme_mainline_evidence
            WHERE trade_date = CAST(? AS DATE)
            ORDER BY mainline_score DESC NULLS LAST
            LIMIT 12
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    return [
        {
            "code": str(r[0]), "name": str(r[1] or ""),
            "strength": r[2], "seal_rate": r[3],
            "main_net": r[4], "components": r[5],
            "reason": r[6], "score": r[7],
        }
        for r in rows
    ]


def _sector_rotation(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    """Sector rotation scores (top 12) for a bar chart."""
    if not table_exists(con, "sector_rotation_score"):
        return []
    try:
        rows = con.execute(
            """
            SELECT sector_name, score, strength_value, limit_up_count, seal_rate
            FROM sector_rotation_score
            WHERE trade_date = CAST(? AS DATE)
            ORDER BY score DESC LIMIT 12
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    return [
        {"name": str(r[0] or ""), "score": r[1], "strength": r[2],
         "limit_up": r[3], "seal_rate": r[4]}
        for r in rows
    ]


def _candidate_flow(con: duckdb.DuckDBPyConnection, trade_date: str) -> list[dict[str, Any]]:
    """Per-stage candidate counts for the funnel chart."""
    try:
        rows = con.execute(
            """
            SELECT stage,
                   count(*) AS total,
                   sum(CASE WHEN is_actionable THEN 1 ELSE 0 END) AS actionable,
                   sum(CASE WHEN tradable THEN 1 ELSE 0 END) AS tradable,
                   sum(CASE WHEN risk_approved THEN 1 ELSE 0 END) AS approved,
                   sum(CASE WHEN is_executable THEN 1 ELSE 0 END) AS executable
            FROM stock_candidate_stage_signal
            WHERE trade_date = CAST(? AS DATE)
            GROUP BY stage ORDER BY stage
            """,
            [trade_date],
        ).fetchall()
    except Exception:
        return []
    order = {"premarket_pool": 0, "auction_confirmation": 1, "intraday_strength": 2, "close_decision": 3}
    return [
        {"stage": str(r[0]), "total": r[1], "actionable": r[2],
         "tradable": r[3], "approved": r[4], "executable": r[5]}
        for r in sorted(rows, key=lambda item: order.get(str(item[0]), 99))
    ]


# ---------------------------------------------------------------------------
# HTML building helpers
# ---------------------------------------------------------------------------

def _e(v: Any) -> str:
    return html.escape(str(v), quote=True)


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
  --bg:#0a0f18; --bg2:#0d1420; --panel:#101a2a; --panel2:#0d1624;
  --line:#1b2740; --line2:#233150;
  --text:#e7edf6; --muted:#7d8ca6; --dim:#55647f;
  --up:#ff5b6a; --down:#2ebd85; --amber:#f0b90b; --cyan:#4cc3ff; --purple:#b48cff;
  --mono:"Bahnschrift","DIN Alternate",ui-monospace,"Cascadia Code",Consolas,"Courier New",monospace;
  --sans:"Segoe UI","Microsoft YaHei","PingFang SC","Helvetica Neue",Arial,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
html{color-scheme:dark; scroll-behavior:smooth}
body{
  background:var(--bg); color:var(--text); font-family:var(--sans); font-size:14px;
  line-height:1.55; min-height:100vh;
  background-image:
    radial-gradient(1100px 480px at 78% -8%, rgba(76,195,255,.06), transparent 60%),
    radial-gradient(900px 420px at 8% 4%, rgba(255,91,106,.05), transparent 55%);
  background-attachment:fixed;
}
.mono{font-family:var(--mono); font-variant-numeric:tabular-nums; letter-spacing:.01em}
.up{color:var(--up)} .down{color:var(--down)} .amber{color:var(--amber)} .cyan{color:var(--cyan)}
.dim{color:var(--muted); font-size:12px} .flat{color:var(--muted)}

/* top bar */
.cmdbar{position:sticky; top:0; z-index:50; display:flex; align-items:center; gap:16px;
  justify-content:space-between; padding:12px 26px; background:rgba(10,15,24,.9);
  backdrop-filter:blur(10px); border-bottom:1px solid var(--line); flex-wrap:wrap}
.brand-mark{font-family:var(--mono); font-weight:800; font-size:15px; letter-spacing:.08em;
  color:var(--bg); background:linear-gradient(135deg,var(--cyan),var(--purple)); padding:3px 8px; border-radius:6px}
.cmd-date{font-family:var(--mono); font-size:12px; color:var(--muted); display:flex; align-items:center; gap:8px}
.cmd-gen{color:var(--dim)}
.toc{display:flex; gap:6px; flex-wrap:wrap}
.toc a{color:var(--muted); text-decoration:none; font-size:12px; padding:4px 10px;
  border:1px solid var(--line); border-radius:20px; transition:all .15s}
.toc a:hover{color:var(--text); border-color:var(--line2); background:var(--panel)}

/* layout */
.wrap{max-width:1440px; margin:0 auto; padding:22px 26px 60px}
.grid{display:grid; gap:16px}
.g-4{grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.g-2{grid-template-columns:repeat(auto-fit,minmax(420px,1fr))}
.g-main{grid-template-columns:minmax(0,1fr) 360px}
@media (max-width:1100px){.g-main{grid-template-columns:1fr}}
section{background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:18px 20px}
section + section{margin-top:16px}
.sec-title{font-size:12px; letter-spacing:.06em; color:var(--muted); margin-bottom:14px;
  display:flex; align-items:center; gap:8px}
.sec-title::before{content:""; width:3px; height:14px; border-radius:2px;
  background:linear-gradient(180deg,var(--cyan),var(--purple))}
.sec-title strong{color:var(--text); font-size:15px}

/* KPI cards */
.kpi{border:1px solid var(--line); border-radius:10px; padding:14px 16px; background:var(--panel2);
  position:relative; overflow:hidden}
.kpi::after{content:""; position:absolute; inset:0 0 auto 0; height:2px;
  background:linear-gradient(90deg,var(--cyan),transparent)}
.kpi .k-label{font-size:11px; color:var(--dim); letter-spacing:.08em}
.kpi .k-value{font-size:24px; font-weight:800; font-family:var(--mono); margin-top:6px}
.kpi .k-sub{font-size:11px; color:var(--muted); margin-top:4px}
.kpi.hot .k-value{color:var(--up)}
.kpi.cold .k-value{color:var(--down)}
.kpi.warn .k-value{color:var(--amber)}

/* charts */
.chart{width:100%; min-height:260px}
.chart-sm{min-height:200px}
.chart-lg{min-height:320px}

/* tables */
table{width:100%; border-collapse:collapse; font-size:13px}
th,td{padding:8px 10px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; white-space:nowrap}
th{color:var(--muted); font-weight:600; font-size:11px; letter-spacing:.05em; background:rgba(125,140,166,.06)}
tbody tr:hover{background:rgba(76,195,255,.04)}
td.num,th.num{text-align:right; font-family:var(--mono)}
td.mono{font-family:var(--mono)}
.table-scroll{overflow-x:auto; border:1px solid var(--line); border-radius:8px}
.empty{color:var(--dim); text-align:center; padding:22px 0; font-size:13px}

/* pills / alerts */
.pill{display:inline-block; padding:2px 9px; border-radius:20px; font-size:11px; font-weight:600}
.pill.ok{color:#2ebd85; background:rgba(46,189,133,.12)}
.pill.warn{color:var(--amber); background:rgba(240,185,11,.12)}
.pill.bad{color:var(--up); background:rgba(255,91,106,.12)}
.alert{border-radius:8px; padding:10px 14px; font-size:13px; margin-bottom:10px; border:1px solid var(--line)}
.alert.warn{color:var(--amber); background:rgba(240,185,11,.08); border-color:rgba(240,185,11,.3)}
.alert.bad{color:var(--up); background:rgba(255,91,106,.08); border-color:rgba(255,91,106,.3)}

/* stage funnel */
.flowline{display:flex; align-items:stretch; gap:10px; margin-top:6px; flex-wrap:wrap}
.flowline .node{flex:1; min-width:150px; text-align:center; padding:10px 8px; border:1px solid var(--line);
  border-radius:8px; background:var(--panel2); position:relative}
.flowline .node .l{font-size:10px; color:var(--dim); letter-spacing:.06em; margin-top:4px}
.flowline .node .n{font-size:26px; font-weight:800; font-family:var(--mono); color:var(--cyan)}
.flowline .node.hot .n{color:var(--up)}
.flowline .arrow{display:flex; align-items:center; color:var(--dim); font-size:18px}

/* detail rows */
.detail-grid{display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px}
.detail-item{border:1px solid var(--line); border-radius:8px; padding:10px 12px; background:var(--panel2)}
.detail-item .d-label{font-size:10px; color:var(--dim); letter-spacing:.06em}
.detail-item .d-value{font-size:14px; margin-top:3px}
.detail-item .d-value small{color:var(--muted); font-size:11px}
.footer{margin-top:30px; text-align:center; color:var(--dim); font-size:11px; letter-spacing:.05em}
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
    consecutive = ctx.get("consecutive")

    cards = [
        ("涨停 / 跌停", f"{_e(limit_up) if limit_up is not None else '—'} / {_e(limit_down) if limit_down is not None else '—'}",
         "涨跌停家数", "hot" if (limit_up or 0) > (limit_down or 0) * 4 else ""),
        ("上涨 / 下跌家数", f"{_num(rise)} / {_num(fall)}",
         f"涨跌比 {_ratio(rise, fall)}", "hot" if (rise or 0) > (fall or 0) else "cold"),
        ("炸板率", _pct(blown_rate), "炸板 / 涨停", "warn" if (blown_rate or 0) >= 25 else ""),
        ("情绪温度", _fmt(emotion), "赚钱效应 CGL", "hot" if (emotion or 0) >= 50 else "cold"),
        ("市场状态", _e(regime_name), f"建议仓位 {_pct(suggested)}", ""),
        ("最高连板", _e(consecutive if consecutive is not None else "—"), "连板高度", ""),
    ]
    return "\n".join(
        f"<div class='kpi {cls}'><div class='k-label'>{_e(label)}</div>"
        f"<div class='k-value'>{value}</div><div class='k-sub'>{_e(sub)}</div></div>"
        for label, value, sub, cls in cards
    )


def _flow_table(title: str, rows: list[dict[str, Any]], columns: list[tuple[str, str, str]],
                empty: str = "暂无数据") -> str:
    head = "".join(f"<th class='{cls}'>{_e(label)}</th>" for label, _, cls in columns)
    body = ""
    money_keys = {"main_net", "super_net", "large_net", "small_net", "mid_net"}
    for row in rows:
        tds = []
        for key, _, cls in columns:
            val = row.get(key)
            if key in money_keys:
                tds.append(f"<td class='num {_sign_class(val)}'>{_fmt(val)}</td>")
            elif key in {"change_pct", "seal_rate"}:
                tds.append(f"<td class='num {_sign_class(val)}'>{_pct(val)}</td>")
            else:
                tds.append(f"<td class='{cls}'>{_e(val) if val is not None else '—'}</td>")
        body += f"<tr>{''.join(tds)}</tr>"
    if not body:
        body = f"<tr><td colspan='{len(columns)}' class='empty'>{empty}</td></tr>"
    return (
        f"<div class='sec-title'><strong>{_e(title)}</strong></div>"
        f"<div class='table-scroll'><table><thead><tr>{head}</tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_tables(ctx: dict[str, Any]) -> str:
    flow = ctx.get("capital_flow", {})
    parts: list[str] = []

    stock_cols = [("排名", "", "num"), ("代码", "", "mono"), ("名称", "", ""),
                  ("主力净额", "", "num"), ("超大单", "", "num"), ("大单", "", "num"),
                  ("涨幅", "", "num"), ("来源", "", "dim")]
    inflow = [{"排名": i + 1, **r} for i, r in enumerate(flow.get("stock_inflow", [])[:20])]
    outflow = [{"排名": i + 1, **r} for i, r in enumerate(flow.get("stock_outflow", [])[:20])]
    parts.append("<div class='grid g-2'>")
    parts.append(_flow_table("个股主力净流入 Top 20", inflow, stock_cols))
    parts.append(_flow_table("个股主力净流出 Top 20", outflow, stock_cols))
    parts.append("</div>")

    sector_cols = [("排名", "", "num"), ("名称", "", ""), ("主力净额", "", "num"),
                   ("涨幅", "", "num"), ("成分数", "", "num")]
    sector_in = [{"排名": i + 1, **r} for i, r in enumerate(flow.get("sector_inflow", [])[:12])]
    sector_out = [{"排名": i + 1, **r} for i, r in enumerate(flow.get("sector_outflow", [])[:12])]
    parts.append("<div class='grid g-2'>")
    parts.append(_flow_table("板块资金净流入 Top 12", sector_in, sector_cols))
    parts.append(_flow_table("板块资金净流出 Top 12", sector_out, sector_cols))
    parts.append("</div>")

    industry_cols = [("排名", "", "num"), ("名称", "", ""), ("主力净额", "", "num"),
                     ("涨幅", "", "num")]
    industry_in = [{"排名": i + 1, **r} for i, r in enumerate(flow.get("industry_inflow", [])[:10])]
    industry_out = [{"排名": i + 1, **r} for i, r in enumerate(flow.get("industry_outflow", [])[:10])]
    parts.append("<div class='grid g-2'>")
    parts.append(_flow_table("行业主力净流入 Top 10", industry_in, industry_cols))
    parts.append(_flow_table("行业主力净流出 Top 10", industry_out, industry_cols))
    parts.append("</div>")
    return "\n".join(parts)


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
        "<div class='table-scroll'><table><thead><tr>"
        "<th>代码</th><th>题材</th><th class='num'>主线分</th><th class='num'>涨停数</th>"
        "<th class='num'>封板率</th><th class='num'>主力净额</th><th class='num'>成分数</th>"
        "<th>驱动逻辑</th></tr></thead><tbody>" + "\n".join(rows) + "</tbody></table></div>"
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
            f"<div style='height:4px;border-radius:2px;background:var(--line2);margin-top:8px;overflow:hidden'>"
            f"<i style='display:block;height:100%;width:{pct}%;"
            f"background:linear-gradient(90deg,var(--cyan),var(--purple))'></i></div>"
            f"<div class='l'>可行动 {st.get('actionable') or 0} · 风控过 {st.get('approved') or 0} · 可执行 {st.get('executable') or 0}</div></div>"
        )
    return "<div class='flowline'>" + " <div class='arrow'>→</div> ".join(nodes) + "</div>"


def _render_alerts(ctx: dict[str, Any]) -> str:
    alerts = ctx.get("alerts", [])
    if not alerts:
        return "<div class='empty'>无风险告警</div>"
    out = []
    for a in alerts[:10]:
        severity = str(a.get("severity") or "info")
        cls = "bad" if severity in {"error", "critical", "warning"} else "warn"
        out.append(
            f"<div class='alert {cls}'><strong>{_e(severity)} / {_e(a.get('category') or '')}</strong>"
            f"<div>{_e(a.get('message') or '')}</div></div>"
        )
    return "\n".join(out)


def _render_plans(ctx: dict[str, Any]) -> str:
    plans = ctx.get("plans", [])
    journal = ctx.get("journal", [])
    risk = ctx.get("risk", {})
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
            rows.append(
                f"<tr><td class='mono'>{_e(p.get('stock_code'))}</td><td>{_e(p.get('stock_name'))}</td>"
                f"<td>{_e(p.get('setup_type') or '—')}</td><td class='num'>{_pct(p.get('max_position_pct'))}</td>"
                f"<td>{_status_pill(p.get('status'))}</td>"
                f"<td class='dim'>{_e(p.get('entry_condition') or '—')}</td>"
                f"<td class='dim'>{_e(p.get('stop_condition') or '—')}</td></tr>"
            )
        parts.append(
            "<div class='sec-title'><strong>今日交易计划</strong></div>"
            "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th><th>类型</th>"
            "<th class='num'>仓位</th><th>状态</th><th>买入条件</th><th>止损条件</th></tr></thead>"
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
    analytics = readiness.get("analytics_ready")
    execution = readiness.get("execution_ready")
    missing = readiness.get("missing_groups") or []
    parts.append(
        "<div class='detail-grid'>"
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

def _chart_js(ctx: dict[str, Any], trend: dict[str, Any], ladder: list[dict[str, Any]],
              rotation: list[dict[str, Any]]) -> str:
    themes = [
        {"name": t["name"], "score": t.get("score"), "main_net": t.get("main_net"),
         "seal_rate": t.get("seal_rate")}
        for t in ctx.get("theme_mainline", [])[:10]
    ]
    d = json.dumps
    return f"""
const trend = {d(trend, ensure_ascii=False)};
const ladder = {d(ladder)};
const rotation = {d(rotation, ensure_ascii=False)};
const themes = {d(themes, ensure_ascii=False)};

const AXIS = {{axisLine:{{lineStyle:{{color:'#233150'}}}}, axisLabel:{{color:'#7d8ca6', fontSize:11}},
  splitLine:{{lineStyle:{{color:'rgba(35,49,80,.35)'}}}}}};

function base(extra) {{
  const c = {{ backgroundColor:'transparent', textStyle:{{color:'#e7edf6'}},
    legend:{{textStyle:{{color:'#7d8ca6'}}, top:0}},
    tooltip:{{trigger:'axis', backgroundColor:'#0d1624', borderColor:'#233150',
      textStyle:{{color:'#e7edf6', fontSize:12}}}},
    grid:{{left:44, right:48, top:38, bottom:26}} }};
  return Object.assign(c, extra);
}}

const charts = [];

charts.push({{el:'chart-breadth',
  opt: base({{ xAxis:{{type:'category', data:trend.dates, ...AXIS}},
    yAxis:[{{type:'value', name:'家数', nameTextStyle:{{color:'#7d8ca6'}}, ...AXIS}},
           {{type:'value', name:'炸板率%', nameTextStyle:{{color:'#7d8ca6'}}, ...AXIS}}],
    series:[
      {{name:'涨停', type:'bar', data:trend.limit_up, itemStyle:{{color:'#ff5b6a'}}, barMaxWidth:14}},
      {{name:'跌停', type:'bar', data:trend.limit_down, itemStyle:{{color:'#2ebd85'}}, barMaxWidth:14}},
      {{name:'炸板率', type:'line', yAxisIndex:1, data:trend.broken_rate,
        itemStyle:{{color:'#f0b90b'}}, lineStyle:{{color:'#f0b90b', width:2}},
        symbolSize:5}}
    ] }})
}});

charts.push({{el:'chart-emotion',
  opt: base({{ xAxis:{{type:'category', data:trend.dates, ...AXIS}},
    yAxis:[{{type:'value', name:'家数', ...AXIS}}, {{type:'value', name:'CGL', ...AXIS}}],
    series:[
      {{name:'上涨', type:'line', smooth:true, data:trend.rise,
        itemStyle:{{color:'#ff5b6a'}}, lineStyle:{{color:'#ff5b6a', width:2}}, areaStyle:{{opacity:.06}}}},
      {{name:'下跌', type:'line', smooth:true, data:trend.fall,
        itemStyle:{{color:'#2ebd85'}}, lineStyle:{{color:'#2ebd85', width:2}}, areaStyle:{{opacity:.06}}}},
      {{name:'情绪温度', type:'line', yAxisIndex:1, smooth:true, data:trend.emotion,
        itemStyle:{{color:'#b48cff'}}, lineStyle:{{color:'#b48cff', width:2, type:'dashed'}},
        symbolSize:4}}
    ] }})
}});

charts.push({{el:'chart-ladder',
  opt: base({{ xAxis:{{type:'category', data:ladder.map(x=>x.height+'板'), ...AXIS}},
    yAxis:{{type:'value', name:'家数', ...AXIS}},
    series:[{{name:'家数', type:'bar', data:ladder.map(x=>x.count),
      itemStyle:{{color:new echarts.graphic.LinearGradient(0,0,0,1,
        [{{offset:0,color:'#ff5b6a'}},{{offset:1,color:'#b48cff'}}])}},
      barMaxWidth:26}}] }})
}});

charts.push({{el:'chart-rotation',
  opt: base({{ xAxis:{{type:'value', ...AXIS}}, yAxis:{{type:'category', inverse:true,
      data:rotation.map(x=>x.name), axisLabel:{{color:'#7d8ca6', fontSize:11}}}},
    series:[{{name:'评分', type:'bar', data:rotation.map(x=>x.score),
      itemStyle:{{color:new echarts.graphic.LinearGradient(0,0,1,0,
        [{{offset:0,color:'#4cc3ff'}},{{offset:1,color:'#b48cff'}}])}},
      label:{{show:true, position:'right', color:'#7d8ca6', fontSize:10}},
      barMaxWidth:14}}] }})
}});

charts.push({{el:'chart-theme',
  opt: base({{ xAxis:{{type:'value', ...AXIS}}, yAxis:{{type:'category', inverse:true,
      data:themes.map(t=>t.name), axisLabel:{{color:'#7d8ca6', fontSize:11}}}},
    series:[{{name:'主力净额', type:'bar', data:themes.map(t=>t.main_net),
      itemStyle:{{color:new echarts.graphic.LinearGradient(0,0,1,0,
        [{{offset:0,color:'#2ebd85'}},{{offset:1,color:'#4cc3ff'}}])}},
      label:{{show:true, position:'right', color:'#7d8ca6', fontSize:10,
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
}});
"""


# ---------------------------------------------------------------------------
# Page assembly
# ---------------------------------------------------------------------------

def _page_html(ctx: dict[str, Any], trade_date: str, echarts_src: str,
               trend: dict[str, Any], ladder: list[dict[str, Any]],
               rotation: list[dict[str, Any]]) -> str:
    kpis = _render_kpis(ctx)
    tables = _render_tables(ctx)
    themes = _render_themes(ctx)
    stages = _render_stages(ctx)
    alerts = _render_alerts(ctx)
    plans = _render_plans(ctx)
    gates = _render_data_gates(ctx)
    consecutive = ctx.get("consecutive")

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>复盘 · {_e(trade_date)}</title>
<style>{_CSS}</style>
</head>
<body>

<header class="cmdbar">
  <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap">
    <span class="brand-mark">复盘</span>
    <span class="cmd-date">REVIEW · <span class="mono">{_e(trade_date)}</span></span>
    <span class="cmd-gen" id="gen-note"></span>
  </div>
  <nav class="toc">
    <a href="#s-breadth">市场情绪</a>
    <a href="#s-theme">题材主线</a>
    <a href="#s-ladder">连板梯队</a>
    <a href="#s-flow">资金流</a>
    <a href="#s-stage">阶段候选</a>
    <a href="#s-alert">风险告警</a>
    <a href="#s-plan">计划执行</a>
    <a href="#s-gate">数据质量</a>
  </nav>
</header>

<div class="wrap">

  <div class="grid g-4" style="margin-bottom:16px">
    {kpis}
  </div>

  <section id="s-breadth">
    <div class="sec-title"><strong>市场情绪与宽度</strong><span class="dim">近 30 个交易日</span></div>
    <div class="grid g-2">
      <div class="chart chart-lg" id="chart-breadth"></div>
      <div class="chart chart-lg" id="chart-emotion"></div>
    </div>
  </section>

  <section id="s-theme">
    <div class="sec-title"><strong>题材主线与板块轮动</strong></div>
    <div class="grid g-main">
      <div>{themes}</div>
      <div>
        <div class="sec-title" style="margin-bottom:8px"><strong>板块轮动评分</strong></div>
        <div class="chart chart-sm" id="chart-rotation"></div>
        <div class="sec-title" style="margin-top:14px;margin-bottom:8px"><strong>题材主力净额</strong></div>
        <div class="chart chart-sm" id="chart-theme"></div>
      </div>
    </div>
  </section>

  <section id="s-ladder">
    <div class="sec-title"><strong>连板梯队</strong></div>
    <div class="grid g-main">
      <div>
        <div class="chart chart-sm" id="chart-ladder"></div>
      </div>
      <div style="display:flex; flex-direction:column; gap:10px">
        <div class="detail-item"><div class="d-label">当前最高连板</div>
          <div class="d-value"><span class="amber mono">{_e(consecutive if consecutive is not None else '—')}</span> 板</div></div>
        <div class="detail-item"><div class="d-label">解读</div>
          <div class="d-value"><small>梯队完整（1 板→最高板逐级递减）说明接力情绪健康；中间高度断档说明接力情绪受损。</small></div></div>
      </div>
    </div>
  </section>

  <section id="s-flow">
    <div class="sec-title"><strong>资金流全景</strong><span class="dim">个股 / 板块 / 行业</span></div>
    {tables}
  </section>

  <section id="s-stage">
    <div class="sec-title"><strong>四阶段候选漏斗</strong></div>
    {stages}
  </section>

  <section id="s-alert">
    <div class="sec-title"><strong>风险告警</strong></div>
    {alerts}
  </section>

  <section id="s-plan">
    <div class="sec-title"><strong>计划与执行</strong></div>
    {plans}
  </section>

  <section id="s-gate">
    <div class="sec-title"><strong>数据质量门禁</strong></div>
    {gates}
  </section>

  <div class="footer">
    复盘页 · {_e(trade_date)} · 静态自包含 HTML（内嵌 ECharts，可离线打开） · 仅作复盘参考，不构成交易建议
  </div>
</div>

<script>{echarts_src}</script>
<script>{_chart_js(ctx, trend, ladder, rotation)}</script>
<script>document.getElementById('gen-note').textContent = '生成 ' + new Date().toLocaleString('zh-CN');</script>
</body>
</html>
"""


def render_review_web(db_path: str | Path, trade_date: str | None = None,
                      echarts_path: str | Path | None = None) -> tuple[str, str]:
    """Build the review page and return (html, trade_date)."""
    if echarts_path is None:
        echarts_path = Path(__file__).resolve().parents[1] / "trade_system" / "vendor" / "echarts.min.js"
    try:
        echarts_src = Path(echarts_path).read_text(encoding="utf-8")
    except Exception:
        echarts_src = "/* echarts unavailable */"
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        selected = trade_date or _latest_date(con)
        ctx = build_daily_review_context(db_path, selected)
        trend = _trend_series(con, selected)
        ladder = _limit_ladder(con, selected)
        rotation = _sector_rotation(con, selected)
        ctx["theme_mainline"] = _theme_mainline(con, selected)
        ctx["candidate_flow"] = _candidate_flow(con, selected)
        try:
            ctx["emotion_rows"] = [
                {"cgl": r[0]} for r in con.execute(
                    "SELECT cgl FROM market_emotion_money WHERE date=CAST(? AS DATE) LIMIT 1",
                    [selected],
                ).fetchall()
            ]
        except Exception:
            ctx["emotion_rows"] = []
        try:
            r = con.execute(
                "SELECT max(consecutive_days) FROM ladder_market WHERE date=CAST(? AS DATE)",
                [selected],
            ).fetchone()
            ctx["consecutive"] = r[0] if r else None
        except Exception:
            ctx["consecutive"] = None
        html_out = _page_html(ctx, selected, echarts_src, trend, ladder, rotation)
    finally:
        con.close()
    return html_out, selected


def write_review_web(db_path: str | Path, out_path: str | Path,
                     trade_date: str | None = None) -> Path:
    """Render the review page and write it to ``out_path``. Returns the path."""
    html_out, selected = render_review_web(db_path, trade_date)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_out, encoding="utf-8")
    return out
