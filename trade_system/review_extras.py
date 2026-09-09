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
from trade_system.kline_access import canonical_daily_kline_relation

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
    # The old query referenced v_kline_daily three times through independent
    # CTEs.  That view includes the canonical/fallback union and can be large;
    # DuckDB may materialize several copies during a post-QLib render.  Resolve
    # the two dates first, then scan the view once for both snapshots.
    kline_relation = canonical_daily_kline_relation(con)
    previous_plan_date = con.execute(
        """
        SELECT max(CAST(trade_date AS DATE))
        FROM trade_plan
        WHERE CAST(trade_date AS DATE) < CAST(? AS DATE)
        """,
        [trade_date],
    ).fetchone()[0]
    previous_kline_date = con.execute(
        f"""
        SELECT max(CAST(trade_date AS DATE))
        FROM {kline_relation}
        WHERE ktype='D' AND CAST(trade_date AS DATE) < CAST(? AS DATE)
        """,
        [trade_date],
    ).fetchone()[0]
    if previous_plan_date is None or previous_kline_date is None:
        return ("<div class='empty'>昨日无交易计划（或今日无行情），"
                "无法生成计划对照。</div>")
    rows = con.execute(
        f"""
        WITH k AS (
            SELECT stock_code,
                   max(CASE WHEN CAST(trade_date AS DATE) = CAST(? AS DATE) THEN close END) AS prev_close,
                   max(CASE WHEN CAST(trade_date AS DATE) = CAST(? AS DATE) THEN open END) AS open,
                   max(CASE WHEN CAST(trade_date AS DATE) = CAST(? AS DATE) THEN high END) AS high,
                   max(CASE WHEN CAST(trade_date AS DATE) = CAST(? AS DATE) THEN change_pct END) AS change_pct
            FROM {kline_relation}
            WHERE ktype='D'
              AND CAST(trade_date AS DATE) IN (CAST(? AS DATE), CAST(? AS DATE))
            GROUP BY stock_code
        )
        SELECT p.stock_code, max(p.stock_name) AS name,
               round((k.open / k.prev_close - 1) * 100, 2) AS gap_pct,
               round(k.change_pct, 2) AS day_pct,
               round((k.high / k.prev_close - 1) * 100, 2) AS max_touch_pct,
               CASE WHEN k.high >= k.prev_close * 1.05 THEN '给了介入点'
                    WHEN k.open > k.prev_close * 1.07 THEN '高开过大，难接'
                    ELSE '未给介入点' END AS verdict
        FROM trade_plan p
        JOIN k ON k.stock_code = p.stock_code
        WHERE CAST(p.trade_date AS DATE) = CAST(? AS DATE)
          AND k.prev_close > 0
          AND k.open IS NOT NULL
        GROUP BY p.stock_code, k.open, k.high, k.change_pct, k.prev_close
        ORDER BY gap_pct DESC NULLS LAST LIMIT 20
        """,
        [previous_kline_date, trade_date, trade_date, trade_date,
         previous_kline_date, trade_date, previous_plan_date],
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


# ------------------------------------------------- ⑥ QLib research snapshot
def render_qlib_screen(con: duckdb.DuckDBPyConnection) -> str:
    day_row = con.execute(
        """SELECT trade_date, model_id FROM qlib_prediction
           ORDER BY trade_date DESC LIMIT 1"""
    ).fetchone()
    if not day_row:
        return ""
    day, model_id = str(day_row[0]), str(day_row[1])
    feature_day = None
    if _xdf_table_exists(con, "qlib_stock_flow_features"):
        feature_day = con.execute(
            "SELECT max(CAST(trade_date AS DATE)) FROM qlib_stock_flow_features"
        ).fetchone()[0]
        feature_day = str(feature_day)[:10] if feature_day else None
    candidate_day = None
    pool_rows = []
    if _xdf_table_exists(con, "qlib_candidate_pool"):
        candidate_day_row = con.execute(
            "SELECT max(CAST(trade_date AS DATE)) FROM qlib_candidate_pool WHERE model_id=?",
            [model_id],
        ).fetchone()
        candidate_day = str(candidate_day_row[0])[:10] if candidate_day_row and candidate_day_row[0] else None
        if candidate_day == day:
            pool_rows = con.execute(
                """
                SELECT stock_code, qlib_score, qlib_rank, combined_score,
                       stock_flow_5d, sector_flow_5d, candidate_status, blocker_reason
                FROM qlib_candidate_pool
                WHERE model_id=? AND trade_date=?
                ORDER BY combined_score DESC NULLS LAST, qlib_rank NULLS LAST, stock_code
                LIMIT 10
                """,
                [model_id, day],
            ).fetchall()

    # Prefer the fused pool when it exists: this makes the QLib contribution
    # visible alongside stock/sector flow instead of presenting an isolated
    # model score that the operator cannot audit.
    if pool_rows:
        limit_up_codes = set()
        if _xdf_table_exists(con, "official_limit_pool"):
            limit_up_codes = {
                str(row[0])
                for row in con.execute(
                    "SELECT DISTINCT stock_code FROM official_limit_pool WHERE CAST(trade_date AS VARCHAR)=?",
                    [day],
                ).fetchall()
            }
        rows = []
        for symbol, score, rank, combined, stock_flow, sector_flow, status, blocker in pool_rows:
            rows.append((symbol, score, rank, combined, stock_flow, sector_flow, status, blocker, str(symbol) in limit_up_codes))
    else:
        if _xdf_table_exists(con, "official_limit_pool"):
            rows = con.execute(
                """
                SELECT p.symbol, p.score, p."rank", NULL AS combined_score,
                       NULL AS stock_flow_5d, NULL AS sector_flow_5d,
                       'prediction_only' AS candidate_status, NULL AS blocker_reason,
                       max(CASE WHEN o.trade_date IS NOT NULL THEN 1 ELSE 0 END) AS in_pool
                FROM qlib_prediction p
                LEFT JOIN official_limit_pool o
                  ON o.stock_code = p.symbol
                 AND CAST(o.trade_date AS VARCHAR) = ?
                WHERE p.model_id = ? AND p.trade_date = ?
                GROUP BY p.symbol, p.score, p."rank"
                ORDER BY p.score DESC LIMIT 10
                """,
                [day, model_id, day],
            ).fetchall()
        else:
            rows = con.execute(
                """
                SELECT p.symbol, p.score, p."rank", NULL AS combined_score,
                       NULL AS stock_flow_5d, NULL AS sector_flow_5d,
                       'prediction_only' AS candidate_status, NULL AS blocker_reason,
                       0 AS in_pool
                FROM qlib_prediction p
                WHERE p.model_id = ? AND p.trade_date = ?
                ORDER BY p.score DESC LIMIT 10
                """,
                [model_id, day],
            ).fetchall()
    if not rows:
        return ""

    evaluation = None
    if _xdf_table_exists(con, "qlib_shadow_evaluation"):
        evaluation = con.execute(
            """
            SELECT sample_end, sample_count, ic, rank_ic, top_bottom_spread
            FROM qlib_shadow_evaluation WHERE model_id=?
            ORDER BY updated_at DESC NULLS LAST, sample_end DESC NULLS LAST LIMIT 1
            """,
            [model_id],
        ).fetchone()

    def _cell(value, digits: int = 2) -> str:
        if value is None:
            return "—"
        try:
            return f"{float(value):.{digits}f}"
        except (TypeError, ValueError):
            return _esc(value)

    trs = "".join(
        f"<tr><td class='num'>{_esc(rank)}</td><td class='mono'>{_esc(symbol)}</td>"
        f"<td class='num'>{_cell(score, 4)}</td><td class='num'>{_cell(combined, 2)}</td>"
        f"<td class='num'>{_cell(stock_flow)}</td><td class='num'>{_cell(sector_flow)}</td>"
        f"<td>{_esc(status or '—')} {'🔥 涨停池' if in_pool else ''}</td></tr>"
        for symbol, score, rank, combined, stock_flow, sector_flow, status, blocker, in_pool in rows
    )
    evaluation_text = "无后验评估"
    if evaluation:
        evaluation_text = (
            f"后验截至 {str(evaluation[0])[:10]} · n={evaluation[1] or 0} · "
            f"IC={_cell(evaluation[2], 3)} · RankIC={_cell(evaluation[3], 3)} · "
            f"Top-Bottom={_cell(evaluation[4], 2)}"
        )
    return (
        "<div class='sec-title'><strong>QLib 研究模型快照 Top10</strong>"
        f"<span class='sec-kicker'>预测日 {day} · 特征截止 {feature_day or '—'} · "
        f"候选截止 {candidate_day or '—'} · 模型 {model_id} · {evaluation_text} · 非当日执行信号 · research-only</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>QLib排名</th><th>代码</th>"
        "<th class='num'>模型分</th><th class='num'>融合分</th><th class='num'>个股资金5日</th>"
        "<th class='num'>概念资金5日</th><th>状态/标记</th></tr></thead>"
        f"<tbody>{trs}</tbody></table></div>"
    )


# ------------------------------------------------- ⑦ 次日候选筛选
def render_daily_picks(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    rows = con.execute(
        """
        SELECT rank, stock_code, stock_name, board, total_score,
               limit_up_reason, llm_bull_case, llm_risk, llm_watch_condition,
               factor_json, CAST(trade_date AS VARCHAR) AS actual_date
        FROM daily_stock_picks WHERE trade_date = (
            SELECT max(trade_date) FROM daily_stock_picks WHERE trade_date <= ?
        ) ORDER BY rank LIMIT 10
        """,
        [trade_date],
    ).fetchall()
    if not rows:
        return ""
    trs = []
    actual_date = str(rows[0][-1])[:10]
    for rank, code, name, board, score, reason, bull, risk, watch, fj, _actual_date in rows:
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
        f"<span class='sec-kicker'>数据日 {_esc(actual_date)} · 请求日 {_esc(trade_date)} · 规则评分 + QLib + DeepSeek 复核 · research-only</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>#</th><th>代码</th><th>名称</th>"
        "<th>板</th><th class='num'>总分</th><th>涨停原因</th><th>做多逻辑</th>"
        "<th>风险</th><th>明日观察</th></tr></thead>"
        f"<tbody>{''.join(trs)}</tbody></table></div>"
    )


# ------------------------------------------------- ⑧ 题材生命周期时间线
def render_theme_timeline(con: duckdb.DuckDBPyConnection, trade_date: str,
                          days: int = 20, top_n: int = 12) -> str:
    """Per-theme daily limit-up intensity over the recent window.

    Reads the THS membership snapshot and joins the canonical, date-aware
    ``v_limit_pool``.  Counting membership rows alone makes a theme with 900
    constituents look like it had 900 limit-ups; the join keeps the timeline
    aligned with the same limit-up authority used by the drill-down panel.
    """
    has_default = bool(con.execute(
        "SELECT count(*) FROM ("
        "SELECT table_name FROM information_schema.tables WHERE table_name='v_default_concept_stock_history' "
        "UNION ALL SELECT table_name FROM information_schema.views WHERE table_name='v_default_concept_stock_history'"
        ")"
    ).fetchone()[0])
    # Minimal/read-only legacy databases used by the renderer may not have the
    # normalized views yet. Production databases do, and therefore take the
    # quality-gated path; this compatibility branch keeps the renderer usable
    # while migrations are being applied.
    history_relation = "v_default_concept_stock_history" if has_default else "ths_concept_stock_history"
    daily_relation = "v_default_concept_daily" if has_default else "ths_concept_daily"
    verified_filter = "" if has_default else " AND date_verified"
    if not bool(con.execute(
        "SELECT count(*) FROM ("
        "SELECT table_name FROM information_schema.tables WHERE table_name=? "
        "UNION ALL SELECT table_name FROM information_schema.views WHERE table_name=?"
        ")", [history_relation, history_relation]
    ).fetchone()[0]):
        return ""
    dates = [str(r[0]) for r in con.execute(
        f"""SELECT DISTINCT CAST(trade_date AS DATE) FROM {history_relation}
           WHERE CAST(trade_date AS DATE) <= ?{verified_filter}
           ORDER BY 1 DESC LIMIT ?""",
        [trade_date, days]).fetchall()]
    if not dates:
        return ""
    coverage = con.execute(
        f"SELECT count(DISTINCT concept_code) FROM {daily_relation} "
        "WHERE CAST(trade_date AS DATE) = ?", [dates[0]]).fetchone()[0]
    expected_row = None
    if bool(con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_name='ths_concept_snapshot_expectation'"
    ).fetchone()[0]):
        expected_row = con.execute(
            "SELECT expected_concepts FROM ths_concept_snapshot_expectation "
            "WHERE trade_date = CAST(? AS DATE)", [dates[0]]
        ).fetchone()
    expected_concepts = (expected_row[0] if expected_row and expected_row[0] else coverage) or 0
    rows = con.execute(
        f"""
        WITH limit_up AS (
            SELECT CAST(trade_date AS DATE) AS trade_date,
                   regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS stock_code
            FROM v_limit_pool
            WHERE CAST(trade_date AS DATE) IN ({','.join('?' * len(dates))})
            GROUP BY 1, 2
        )
        SELECT h.concept_name, CAST(h.trade_date AS VARCHAR),
               count(DISTINCT lu.stock_code) AS zt
        FROM {history_relation} h
        JOIN limit_up lu
          ON lu.trade_date = CAST(h.trade_date AS DATE)
         AND lu.stock_code = regexp_replace(CAST(h.stock_code AS VARCHAR), '[.].*$', '')
        WHERE CAST(h.trade_date AS DATE) IN
              ({','.join('?' * len(dates))})
        GROUP BY 1, 2
        """,
        [*dates, *dates],
    ).fetchall()
    if not rows:
        return ""
    by_theme: dict[str, dict[str, int]] = {}
    for name, d, zt in rows:
        by_theme.setdefault(name, {})[d] = zt
    totals = {name: sum(v.values()) for name, v in by_theme.items()}
    top = sorted(totals, key=lambda n: -totals[n])[:top_n]
    max_zt = max((by_theme[n].get(d, 0) for n in top for d in dates), default=1)
    head_cells = "".join(
        f"<div class='tl-date'>{d[5:]}</div>" for d in reversed(dates))
    body_rows = []
    for name in top:
        cells = "".join(
            f"<div class='tl-cell' title='{_esc(name)} {d[5:]}：{by_theme[name].get(d, 0)} 只涨停'"
            f" style='background:rgba(214,69,69,{0.15 + 0.85 * by_theme[name].get(d, 0) / max_zt:.2f})'>"
            f"{by_theme[name].get(d, 0) or ''}</div>"
            for d in reversed(dates))
        body_rows.append(
            f"<div class='tl-row'><div class='tl-name'>{_esc(name)}</div>{cells}</div>")
    warn = (" ⚠️ 当日快照不完整" if int(coverage or 0) < int(expected_concepts) else "")
    return (
        "<div class='sec-title'><strong>题材生命周期时间线</strong>"
        "<span class='sec-kicker'>近 "
        f"{len(dates)} 日 · 颜色深浅=当日涨停家数 · Top{top_n} 题材</span></div>"
        f"<div class='tl-head'><div class='tl-name'></div>{head_cells}</div>"
        + "".join(body_rows)
        + f"<div class='concept-footnote'>最新快照概念覆盖 {coverage}/{expected_concepts}{warn}；"
          "一只股票可属多个题材。research-only。</div>"
    )


# ------------------------------------------------- ⑨ 首次涨停时点分布
def render_first_seal_distribution(con: duckdb.DuckDBPyConnection,
                                   trade_date: str) -> str:
    if not _xdf_table_exists(con, "v_limit_pool"):
        return ""
    buckets = [
        ("集合竞价秒板", 925, 926),
        ("早盘抢板", 926, 1000),
        ("上午中段", 1000, 1130),
        ("午后", 1300, 1400),
        ("尾盘偷袭", 1400, 1500),
    ]
    counts = []
    for label, lo, hi in buckets:
        n = con.execute(
            """SELECT count(*) FROM v_limit_pool
               WHERE CAST(trade_date AS DATE)=CAST(? AS DATE)
                 AND limit_up_time IS NOT NULL
                 AND TRY_CAST(replace(substr(limit_up_time, 1, 5), ':', '') AS INTEGER)
                     BETWEEN ? AND ?""",
            [trade_date, lo, hi],
        ).fetchone()[0]
        counts.append((label, int(n)))
    total = sum(n for _, n in counts) or 1
    bars = "".join(
        f"<div class='fs-item' title='{_esc(label)} {_esc(n)}只({_esc(round(n / total * 100))}%)'>"
        f"<i style='height:{max(6, int(n / total * 90))}px'></i>"
        f"<span>{_esc(label)}<br><b>{n}</b></span></div>"
        for label, n in counts
    )
    return (
        "<div class='sec-title'><strong>首次涨停时间分布</strong>"
        "<span class='sec-kicker'>越早越强，尾盘板谨慎，通常弱转强</span></div>"
        f"<div class='fs-strip'>{bars}</div>"
    )


# ------------------------------------------------- xiaodefa 官方增量数据
def _xdf_table_exists(con, name: str) -> bool:
    return bool(con.execute(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = ?",
        [name],
    ).fetchone()[0])


def _render_xdf_chips(con, trade_date: str) -> str:
    if not _xdf_table_exists(con, "xdf_cyq_perf"):
        return ""
    row = con.execute(
        """
        SELECT COUNT(*), ROUND(AVG(CAST(winner_rate AS DOUBLE)),1),
               ROUND(MEDIAN(CAST(winner_rate AS DOUBLE)),1)
        FROM xdf_cyq_perf WHERE trade_date = ?
        """,
        [trade_date],
    ).fetchone()
    if not row or not row[0]:
        return ""
    total, avg_win, med_win = row
    tops = con.execute(
        """
        SELECT c.ts_code,
               COALESCE(b.stock_name, c.ts_code) AS name,
               ROUND(CAST(c.winner_rate AS DOUBLE),1),
               CAST(c.cost_50pct AS DOUBLE)
        FROM xdf_cyq_perf c
        LEFT JOIN tushare_stock_basic b ON b.ts_code = c.ts_code
        WHERE c.trade_date = ?
          AND CAST(c.winner_rate AS DOUBLE) BETWEEN 0 AND 100
        ORDER BY CAST(c.winner_rate AS DOUBLE) DESC LIMIT 8
        """,
        [trade_date],
    ).fetchall()
    rows_html = "".join(
        f"<tr><td class='mono'>{_esc(code)}</td><td>{_esc(name)}</td>"
        f"<td class='num'>{_esc(win)}%</td><td class='num'>{_esc(cost50)}</td></tr>"
        for code, name, win, cost50 in tops
    )
    return (
        "<div class='sec-title'><strong>官方筹码分布</strong>"
        "<span class='sec-kicker'>TuShare cyq_perf · 全市场 "
        f"{_esc(total)} 只 · 获利盘中位数 {_esc(med_win)}%</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th>"
        "<th class='num'>获利盘</th><th class='num'>平均成本</th></tr></thead>"
        f"<tbody>{rows_html}</tbody></table></div>"
    )


def _render_xdf_chip_detail(con, trade_date: str) -> str:
    """Render one bounded detailed distribution when P1 chips were collected.

    ``cyq_chips`` is a price-bin series, not a full-market summary.  Show the
    most completely collected stock and its largest bins so the page remains
    useful without loading thousands of rows into the static artifact.
    """
    if not _xdf_table_exists(con, "xdf_cyq_chips"):
        return ""
    try:
        selected = con.execute(
            """
            SELECT ts_code, COUNT(*) AS bins
            FROM xdf_cyq_chips
            WHERE CAST(trade_date AS VARCHAR) = ?
            GROUP BY ts_code
            ORDER BY bins DESC, ts_code
            LIMIT 1
            """,
            [trade_date],
        ).fetchone()
        if not selected:
            return ""
        code, bins = selected
        rows = con.execute(
            """
            SELECT CAST(price AS DOUBLE), CAST(percent AS DOUBLE)
            FROM xdf_cyq_chips
            WHERE CAST(trade_date AS VARCHAR) = ? AND ts_code = ?
            ORDER BY CAST(percent AS DOUBLE) DESC, CAST(price AS DOUBLE)
            LIMIT 8
            """,
            [trade_date, code],
        ).fetchall()
        if not rows:
            return ""
        name = con.execute(
            "SELECT COALESCE(MAX(stock_name), ?) FROM tushare_stock_basic WHERE ts_code = ?",
            [code, code],
        ).fetchone()[0]
    except Exception:
        return ""
    body = "".join(
        f"<tr><td class='mono'>{_esc(round(price, 3))}</td>"
        f"<td class='num'>{_esc(round(percent, 3))}%</td></tr>"
        for price, percent in rows
    )
    return (
        "<div class='sec-title'><strong>成本分布明细</strong>"
        f"<span class='sec-kicker'>{_esc(name)} · {_esc(code)} · {bins} 个价位桶 · 占比 TOP8</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>价格</th>"
        "<th class='num'>筹码占比</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_xdf_margin(con, trade_date: str) -> str:
    if not _xdf_table_exists(con, "xdf_margin_summary"):
        return ""
    rows = con.execute(
        """
        WITH valid_days AS (
            SELECT trade_date,
                   ROUND(SUM(CAST(rzye AS DOUBLE))/1e12, 3) AS trill,
                   COUNT(DISTINCT UPPER(TRIM(exchange_id))) AS exchange_count
            FROM xdf_margin_summary
            WHERE COALESCE(TRIM(exchange_id), '') <> ''
              AND UPPER(TRIM(exchange_id)) <> 'BSE'
              AND rzye IS NOT NULL
            GROUP BY trade_date
        )
        SELECT CAST(trade_date AS VARCHAR), trill
        FROM valid_days
        WHERE exchange_count >= 2
        ORDER BY trade_date DESC
        LIMIT 10
        """
    ).fetchall()
    if not rows:
        return ""
    items = []
    for index, (day, trill) in enumerate(rows):
        # Rows are newest-first: compare today's valid aggregate with the
        # next older trading day, never with the next newer row.
        older = rows[index + 1][1] if index + 1 < len(rows) else None
        delta = "" if older is None else (
            f"<span style='color:{'#d64545' if trill >= older else '#4c7fd6'}'>"
            f"{'+' if trill >= older else ''}{round((trill - older) * 1000, 1)}亿</span>"
        )
        items.append(f"<div class='mg-item'><small class='dim'>{day[5:]}</small>"
                     f"<b>{trill}万亿</b>{delta}</div>")
    latest_day, latest_val = rows[0]
    note = "（当日）" if latest_day == trade_date else f"（最新 {latest_day[5:]}）"
    return (
        "<div class='sec-title'><strong>两融余额趋势</strong>"
        f"<span class='sec-kicker'>沪深合计（需同时具备沪深两所） · 近10日{note}</span></div>"
        "<div class='mg-strip'>" + "".join(items) + "</div>"
    )


def _render_xdf_unlock(con, trade_date: str) -> str:
    if not _xdf_table_exists(con, "xdf_share_float"):
        return ""
    rows = con.execute(
        """
        SELECT f.ts_code, COALESCE(b.stock_name, f.ts_code) AS name,
               MIN(f.float_date) AS float_date,
               ROUND(SUM(CAST(f.float_share AS DOUBLE))/1e8, 2) AS yi_shares
        FROM xdf_share_float f
        LEFT JOIN tushare_stock_basic b ON b.ts_code = f.ts_code
        WHERE CAST(f.float_date AS DATE) > CAST(? AS DATE)
          AND CAST(f.float_date AS DATE) <= CAST(? AS DATE) + INTERVAL 7 DAY
        GROUP BY f.ts_code, name
        ORDER BY yi_shares DESC LIMIT 10
        """,
        [trade_date, trade_date],
    ).fetchall()
    if not rows:
        return ""
    body = "".join(
        f"<tr><td class='mono'>{_esc(code)}</td><td>{_esc(name)}</td>"
        f"<td class='num'>{_esc(day)}</td><td class='num'>{_esc(yi)}亿股</td></tr>"
        for code, name, day, yi in rows
    )
    return (
        "<div class='sec-title'><strong>未来7日解禁预警</strong>"
        "<span class='sec-kicker'>按解禁股数排序 · 持有相关标的注意</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th>"
        "<th class='num'>解禁日</th><th class='num'>规模</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def _render_xdf_hk_shift(con, trade_date: str) -> str:
    if not _xdf_table_exists(con, "xdf_hk_hold"):
        return ""
    rows = con.execute(
        """
        WITH cur AS (
            SELECT ts_code, max(name) AS name,
                   max(CAST(vol AS DOUBLE)) AS shares,
                   max(CAST(ratio AS DOUBLE)) AS ratio
            FROM xdf_hk_hold WHERE CAST(trade_date AS VARCHAR) = ?
            GROUP BY ts_code
        ),
        prev AS (
            SELECT ts_code, max(CAST(vol AS DOUBLE)) AS shares
            FROM xdf_hk_hold
            WHERE CAST(trade_date AS VARCHAR) = (
                SELECT max(CAST(trade_date AS VARCHAR)) FROM xdf_hk_hold
                WHERE CAST(trade_date AS VARCHAR) < ?
            )
            GROUP BY ts_code
        )
        SELECT cur.ts_code, cur.name,
               ROUND(cur.shares/1e6, 0) AS shares_mln,
               cur.ratio,
               ROUND((cur.shares / NULLIF(prev.shares,0) - 1) * 100, 1) AS chg_pct
        FROM cur JOIN prev ON prev.ts_code = cur.ts_code
        WHERE prev.shares > 0 AND cur.shares > 0
        ORDER BY ABS((cur.shares / NULLIF(prev.shares,0) - 1)) DESC
        LIMIT 12
        """,
        [trade_date, trade_date],
    ).fetchall()
    if not rows:
        return ""
    body = "".join(
        f"<tr><td class='mono'>{_esc(code)}</td><td>{_esc(name)}</td>"
        f"<td class='num'>{_esc(shares)}百万股</td><td class='num'>{_esc(ratio)}%</td>"
        f"<td class='num' style='color:{'#d64545' if (chg or 0) >= 0 else '#4c7fd6'}'>{_esc(chg)}%</td></tr>"
        for code, name, shares, ratio, chg in rows
    )
    return (
        "<div class='sec-title'><strong>港股通持股异动</strong>"
        "<span class='sec-kicker'>南向资金视角 · 单日增减幅TOP</span></div>"
        "<div class='table-scroll'><table><thead><tr><th>代码</th><th>名称</th>"
        "<th class='num'>持股量</th><th class='num'>占比</th><th class='num'>单日变化</th></tr></thead>"
        f"<tbody>{body}</tbody></table></div>"
    )


def render_xiaodefa(con: duckdb.DuckDBPyConnection, trade_date: str) -> str:
    blocks = [
        _render_xdf_chips(con, trade_date),
        _render_xdf_chip_detail(con, trade_date),
        _render_xdf_hk_shift(con, trade_date),
        _render_xdf_margin(con, trade_date),
        _render_xdf_unlock(con, trade_date),
    ]
    html_body = "".join(b for b in blocks if b)
    if not html_body:
        return ""
    source_dates = []
    for table in ("xdf_cyq_perf", "xdf_hk_hold", "xdf_margin_summary", "xdf_share_float"):
        if not _xdf_table_exists(con, table):
            continue
        try:
            latest = con.execute(f"SELECT max(CAST(trade_date AS DATE)) FROM {table}").fetchone()[0]
        except Exception:
            latest = None
        if latest:
            source_dates.append(f"{table.replace('xdf_', '')}={latest}")
    date_note = " · ".join(source_dates) if source_dates else "无可用批次"
    return (
        "<div class='sec-title' id='s-xdf'><strong>xiaodefa 官方数据</strong>"
        f"<span class='sec-kicker'>筹码 · 港资 · 两融 · 解禁 ｜ 批次 {date_note}</span></div>"
        + html_body
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
            "theme_timeline": render_theme_timeline(con, trade_date),
            "first_seal": render_first_seal_distribution(con, trade_date),
            "qlib_screen": render_qlib_screen(con),
            "xiaodefa": render_xiaodefa(con, trade_date),
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
.tl-head,.tl-row{display:grid;grid-template-columns:150px repeat(20,1fr);gap:2px;
margin-bottom:2px;font-size:10px}
.tl-date{color:#888;text-align:center}
.tl-name{font-weight:600;color:#33415c;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.tl-cell{min-height:22px;border-radius:3px;color:#fff;text-align:center;
line-height:22px;font-size:10px}
.fs-strip{display:flex;gap:14px;align-items:flex-end;height:130px}
.fs-item{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:
flex-end;height:100%}
.fs-item i{display:block;width:70%;background:linear-gradient(180deg,#e8804c,#d64545);
border-radius:4px 4px 0 0}
.fs-item span{font-size:10px;color:#666;text-align:center;margin-top:4px;line-height:1.5}
.mg-strip{display:flex;gap:10px;flex-wrap:wrap}
.mg-item{background:#f7f8fa;border-radius:6px;padding:6px 10px;text-align:center;
min-width:86px;display:flex;flex-direction:column;gap:2px}
.mg-item small{font-size:10px;color:#888}
.mg-item b{font-size:13px}
</style>
"""
    blocks = "".join(
        f'<section id="s-extra-{key}" data-screen-label="{key}">{html_}</section>'
        for key, html_ in sections.items() if html_
    )
    return css + blocks
