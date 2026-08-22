"""Extra review-page sections: cycle band, plan-vs-actual, loss panel,
operator stats card, and the market journal note.

Each renderer opens its own read-only connection (the page is generated
offline once a day; simplicity beats pooling here) and returns an HTML
fragment.  ``render_all`` is wired into ``review_web._render_review_bundle``.
"""
from __future__ import annotations

import html
from pathlib import Path

import duckdb

from trade_system.cycle import PHASE_CN

_PHASE_COLOR = {
    "climax": "#d64545",
    "ferment": "#e8804c",
    "recovery": "#e2b93b",
    "divergence": "#8a8f98",
    "retreat": "#4c7fd6",
    "ice": "#3457d5",
}


def _esc(v) -> str:
    return html.escape(str(v)) if v is not None else "—"


def _connect(db_path):
    return duckdb.connect(str(db_path), read_only=True)


# ------------------------------------------------------------ ① 周期色带
def render_cycle_band(con: duckdb.DuckDBPyConnection, trade_date: str,
                      days: int = 60) -> str:
    rows = con.execute(
        """
        SELECT CAST(trade_date AS VARCHAR), phase, score,
               limit_up_count, premium_pct
        FROM market_cycle_phase
        WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?
        """,
        [trade_date, days],
    ).fetchall()
    if not rows:
        return ""
    cells = []
    for d, phase, score, lu, prem in reversed(rows):
        color = _PHASE_COLOR.get(phase, "#8a8f98")
        tip = f"{d}  {PHASE_CN.get(phase, phase)}  温度{score}  涨停{lu}  溢价{prem}"
        cells.append(
            f"<div class='cb-cell' style='background:{color}' title='{_esc(tip)}'>"
            f"<span>{d[5:]}</span></div>"
        )
    legend = " · ".join(
        f"<span class='cb-lg'><i style='background:{_PHASE_COLOR[p]}'></i>{PHASE_CN.get(p, p)}</span>"
        for p in ("ice", "recovery", "ferment", "climax", "divergence", "retreat")
    )
    latest = rows[0]
    head = (
        f"<div class='cb-head'>当前相位：<b style='color:{_PHASE_COLOR.get(latest[1], '#333')}'>"
        f"{_esc(PHASE_CN.get(latest[1], latest[1]))}</b>（{latest[3] or '—'} 家涨停，"
        f"溢价 {_esc(latest[4])}%）</div>"
    )
    return (
        "<div class='sec-title'><strong>情绪周期日历</strong>"
        "<span class='sec-kicker'>最近 "
        f"{len(rows)} 个交易日 · 鼠标悬停看明细</span></div>"
        + head
        + "<div class='cb-strip'>" + "".join(cells) + "</div>"
        + f"<div class='cb-legend'>{legend}</div>"
    )


# ------------------------------------------------- ② 昨日计划×今日实际
def render_plan_vs_actual(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    rows = con.execute(
        """
        WITH prev AS (
            SELECT stock_code, max(stock_name) AS stock_name FROM trade_plan
            WHERE CAST(trade_date AS VARCHAR) = (
                SELECT max(CAST(trade_date AS VARCHAR)) FROM trade_plan
                WHERE CAST(trade_date AS VARCHAR) < ?)
            GROUP BY stock_code
        ),
        k0 AS (
            SELECT stock_code, close AS prev_close FROM v_kline_daily
            WHERE ktype='D' AND CAST(trade_date AS DATE) = (
                SELECT max(CAST(trade_date AS DATE)) FROM v_kline_daily
                WHERE CAST(trade_date AS DATE) < ?)
        ),
        k1 AS (
            SELECT stock_code, open, high, close, change_pct FROM v_kline_daily
            WHERE ktype='D' AND CAST(trade_date AS DATE) = ?
        )
        SELECT p.stock_code, max(p.stock_name) AS name,
               round((k1.open / k0.prev_close - 1) * 100, 2) AS gap_pct,
               round(k1.change_pct, 2) AS day_pct,
               round((k1.high / k0.prev_close - 1) * 100, 2) AS max_touch_pct,
               CASE WHEN k1.high >= k0.prev_close * 1.05 THEN '给了介入点'
                    WHEN k1.open > k0.prev_close * 1.07 THEN '高开过大，难接'
                    ELSE '未给介入点' END AS verdict
        FROM prev p
        JOIN k1 ON k1.stock_code = p.stock_code
        LEFT JOIN k0 ON k0.stock_code = p.stock_code
        GROUP BY p.stock_code, k1.open, k1.high, k1.close, k1.change_pct, k0.prev_close
        ORDER BY gap_pct DESC NULLS LAST LIMIT 20
        """,
        [trade_date, trade_date, trade_date],
    ).fetchall()
    if not rows:
        return ("<div class='empty'>昨日无交易计划（或今日无行情），"
                "无法生成计划对照。</div>")
    trs = []
    for code, name, gap, day_pct, touch, verdict in rows:
        color = "var(--up)" if (day_pct or 0) > 0 else "var(--down)"
        trs.append(
            f"<tr><td class='mono'>{_esc(code)}</td><td>{_esc(name)}</td>"
            f"<td class='num'>{_esc(gap)}%</td><td class='num' style='color:{color}'>"
            f"{_esc(day_pct)}%</td><td class='num'>{_esc(touch)}%</td>"
            f"<td>{_esc(verdict)}</td></tr>"
        )
    return (
        "<div class='sec-title'><strong>昨日计划 × 今日实际</strong>"
        "<span class='sec-kicker'>复盘的灵魂三列：竞价高开 / 全天表现 / 是否给机会</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th>"
        "<th class='num'>竞价高开%</th><th class='num'>全天涨幅%</th>"
        "<th class='num'>最高触及%</th><th>结论</th></tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></div>"
    )


# ------------------------------------------------------- ③ 个人统计卡
def render_stats_card(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    row = con.execute(
        """
        SELECT count(*) AS n,
               avg(CASE WHEN net_return_pct > 0 THEN 1.0 ELSE 0.0 END) AS win_rate,
               avg(net_return_pct) AS avg_ret,
               sum(CASE WHEN net_return_pct > 0 THEN net_return_pct ELSE 0 END)
                 / NULLIF(-sum(CASE WHEN net_return_pct <= 0 THEN net_return_pct ELSE 0 END), 0)
                 AS profit_factor
        FROM operator_trade_outcome
        WHERE execution_status IN ('executed','filled')
          AND net_return_pct IS NOT NULL
          AND substr(CAST(created_at AS VARCHAR), 1, 7)
              = substr(?, 1, 7)
        """,
        [trade_date],
    ).fetchone()
    n, win, avg_ret, pf = row if row else (0, None, None, None)
    cards = [
        ("本月执行", n),
        ("胜率", f"{win:.0%}" if win is not None else "—"),
        ("平均收益", f"{avg_ret:.2f}%" if avg_ret is not None else "—"),
        ("盈亏比 PF", f"{pf:.2f}" if pf else "—"),
    ]
    items = "".join(
        f"<div class='metric'><span>{label}</span><strong>{value}</strong></div>"
        for label, value in cards
    )
    return (
        "<div class='sec-title'><strong>我的本月战绩</strong>"
        "<span class='sec-kicker'>来自导入的真实成交，不是候选分数</span></div>"
        f"<div class='ticker'>{items}</div>"
    )


# ------------------------------------------------------- ④ 亏钱效应面板
def render_loss_panel(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    trend = con.execute(
        """
        SELECT CAST(date AS VARCHAR), limit_down_count, blown_limit_up_rate
        FROM market_limit_up_down_summary
        WHERE date <= ? ORDER BY date DESC LIMIT 20
        """,
        [trade_date],
    ).fetchall()
    if not trend:
        return ""
    cells = "".join(
        f"<div class='lp-col' title='{_esc(d)} 跌停{_esc(ld or 0)} 炸板率"
        f"{_esc(round(blown, 1) if blown is not None else '—')}%'>"
        f"<i style='height:{min(100, int((ld or 0) * 4) + 4)}px'></i></div>"
        for d, ld, blown in reversed(trend)
    )
    today = trend[0]
    blown_txt = (f"{round(today[2], 1)}%" if today[2] is not None else "—")
    return (
        "<div class='sec-title'><strong>亏钱效应</strong>"
        "<span class='sec-kicker'>跌停家数趋势 + 当日炸板率（近20日）</span></div>"
        f"<div class='lp-head'>当日：跌停 <b>{today[1] or 0}</b> 家 · "
        f"炸板率 <b>{blown_txt}</b></div>"
        f"<div class='lp-strip'>{cells}</div>"
    )


# ------------------------------------------------------- ⑤ 市场日志
def render_market_note(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    rows = con.execute(
        """SELECT CAST(trade_date AS VARCHAR), note, tags FROM market_journal
           WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT 5""",
        [trade_date],
    ).fetchall()
    if not rows:
        return ("<div class='empty'>暂无市场日志。用 "
                "scripts/add_market_note.py 记录今天的定性一句话，"
                "半年后它就是你的行情字典。</div>")
    items = "".join(
        f"<li><span class='mono'>{_esc(d)}</span> {_esc(note)}"
        + (f" <span class='pill warn'>{_esc(tags)}</span>" if tags else "")
        + "</li>"
        for d, note, tags in rows
    )
    return ("<div class='sec-title'><strong>市场日志</strong>"
            "<span class='sec-kicker'>每天一句定性 + 标签</span></div>"
            f"<ul class='mj-list'>{items}</ul>")


# ------------------------------------------------- ⑥ QLib 最新选股 Top
def render_qlib_screen(con: duckdb.DuckDBPyConnection) -> str:
    day_row = con.execute(
        """SELECT trade_date, model_id FROM qlib_prediction
           ORDER BY trade_date DESC LIMIT 1"""
    ).fetchone()
    if not day_row:
        return ""
    day, model_id = str(day_row[0]), str(day_row[1])
    rows = con.execute(
        f"""
        SELECT p.symbol, p.score, p."rank",
               max(CASE WHEN o.trade_date IS NOT NULL THEN 1 ELSE 0 END) AS in_pool
        FROM qlib_prediction p
        LEFT JOIN official_limit_pool o
          ON o.stock_code = p.symbol
         AND o.trade_date = DATE '{day}'
        WHERE p.model_id = ? AND p.trade_date = ?
        GROUP BY p.symbol, p.score, p."rank"
        ORDER BY p.score DESC LIMIT 10
        """,
        [model_id, day],
    ).fetchall()
    if not rows:
        return ""
    trs = "".join(
        f"<tr><td class='num'>{_esc(rank)}</td><td class='mono'>{_esc(symbol)}</td>"
        f"<td class='num'>{score:.4f}</td>"
        f"<td>{'🔥 涨停池' if in_pool else ''}</td></tr>"
        for symbol, score, rank, in_pool in rows
    )
    return (
        "<div class='sec-title'><strong>QLib 最新选股 Top10</strong>"
        f"<span class='sec-kicker'>模型 {model_id} · 特征日 {day} · research-only</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>排名</th><th>代码</th>"
        "<th class='num'>模型分</th><th>标记</th></tr></thead>"
        f"<tbody>{trs}</tbody></table></div>"
    )


# ------------------------------------------------- ⑦ 次日候选筛选
def render_daily_picks(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    rows = con.execute(
        """
        SELECT rank, stock_code, stock_name, board, total_score,
               limit_up_reason, llm_bull_case, llm_risk, llm_watch_condition,
               factor_json
        FROM daily_stock_picks WHERE trade_date = ? ORDER BY rank LIMIT 10
        """,
        [trade_date],
    ).fetchall()
    if not rows:
        return ""
    trs = []
    for rank, code, name, board, score, reason, bull, risk, watch, fj in rows:
        trs.append(
            f"<tr><td class='num'>{_esc(rank)}</td><td class='mono'>{_esc(code)}</td>"
            f"<td>{_esc(name)}</td><td>{_esc(board or '—')}</td>"
            f"<td class='num'><b>{_esc(score)}</b></td>"
            f"<td class='dim'>{_esc((reason or '')[:26])}</td>"
            f"<td class='dim'>{_esc(bull or '—')}</td>"
            f"<td class='dim'>{_esc(risk or '—')}</td>"
            f"<td class='dim'>{_esc(watch or '—')}</td></tr>"
        )
    return (
        "<div class='sec-title'><strong>次日候选 Top10</strong>"
        "<span class='sec-kicker'>规则评分 + QLib + DeepSeek 复核 · research-only</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>#</th><th>代码</th><th>名称</th>"
        "<th>板</th><th class='num'>总分</th><th>涨停原因</th><th>做多逻辑</th>"
        "<th>风险</th><th>明日观察</th></tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></div>"
    )


# ------------------------------------------------------------------ all
def render_all(db_path: str | Path, trade_date: str) -> str:
    con = _connect(db_path)
    try:
        sections = {
            "cycle_band": render_cycle_band(con, trade_date),
            "plan_vs_actual": render_plan_vs_actual(con, trade_date),
            "stats_card": render_stats_card(con, trade_date),
            "loss_panel": render_loss_panel(con, trade_date),
            "market_note": render_market_note(con, trade_date),
            "daily_picks": render_daily_picks(con, trade_date),
            "qlib_screen": render_qlib_screen(con),
        }
    finally:
        con.close()
    css = """
<style>
.cb-strip{display:flex;gap:2px;overflow-x:auto;padding:6px 0}
.cb-cell{min-width:34px;height:44px;border-radius:4px;display:flex;align-items:flex-end;
justify-content:center;color:#fff;font-size:9px;cursor:default}
.cb-legend{margin-top:8px;font-size:11px;color:#666}
.cb-lg i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:3px;
vertical-align:-1px}
.cb-head{margin-bottom:6px}
.lp-strip{display:flex;align-items:flex-end;gap:3px;height:110px}
.lp-col{flex:0 0 14px;background:#f0f3f8;border-radius:3px 3px 0 0;display:flex;
align-items:flex-end;justify-content:center}
.lp-col i{display:block;width:100%;background:#3a6fd8;border-radius:3px 3px 0 0}
.mj-list{list-style:none;padding:0}
.mj-list li{padding:6px 0;border-bottom:1px dashed #e5e5e5}
</style>
"""
    blocks = "".join(
        f'<section id="s-extra-{key}" data-screen-label="{key}">{html_}</section>'
        for key, html_ in sections.items() if html_
    )
    return css + blocks
