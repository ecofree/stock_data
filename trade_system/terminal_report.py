# -*- coding: utf-8 -*-
"""Rich "trading war-room" dashboard: data layer.

Builds a self-contained context dict from the DuckDB warehouse (read-only), feeding
the ECharts-based HTML renderer (``render_terminal_html``).  Every fetcher is defensive
-- a missing table or a held write-lock degrades to safe empties rather than raising,
so the page always renders.

Conventions surfaced to the UI: A-share color rule (red = up / inflow, green = down /
outflow) and the project's trust badging (``is_fallback`` / ``source_table`` /
``is_stale``), so every number can show how trustworthy it is.
"""
from __future__ import annotations

import json
import os
from datetime import date, datetime
from pathlib import Path

import duckdb

from trade_system.signals import classify_market_regime
from trade_system.readiness import assess_trade_date_readiness
from trade_system.logging_setup import get_logger
from trade_system.i18n_labels import (
    PLAN_STATUS_CN,
    SETUP_TYPE_CN,
    cn,
    zh_text,
)
from trade_system.cycle import PHASE_CN

logger = get_logger(__name__)

_ECHARTS_CDN = ('<script src="https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js">'
                '</script>')

INDEX_NAMES = {
    "SH000001": "上证指数",
    "SZ399001": "深证成指",
    "SZ399006": "创业板指",
    "SH000688": "科创50",
}


def _connect(db_path: str | Path):
    """Open DuckDB read-only; return None if the pipeline holds the write lock."""
    try:
        return duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return None


def _q(con, sql: str, params: list | None = None) -> list:
    try:
        return con.execute(sql, params or []).fetchall()
    except Exception:
        return []


def _cols(con, table: str) -> set:
    try:
        return {r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except Exception:
        return set()


def _has(con, table: str) -> bool:
    try:
        return bool(con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name=?", [table]).fetchone()[0])
    except Exception:
        return False


def _fnum(v) -> float | None:
    try:
        if v in (None, ""):
            return None
        return float(v)
    except Exception:
        return None


def _name_map(con) -> dict:
    """stock_code -> stock_name, merged from several name-bearing tables."""
    names: dict = {}
    for sql in (
        "SELECT stock_code, stock_name FROM tushare_stock_basic",
        "SELECT stock_code, stock_name FROM v_limit_pool",
        "SELECT stock_code, stock_name FROM stock_candidate_stage_signal",
        "SELECT stock_code, stock_name FROM stock_candidate_score",
    ):
        for code, name in _q(con, sql):
            if code and name and str(code) not in names:
                names[str(code)] = name
    return names


# --------------------------------------------------------------------------- meta
def _market_meta(con, trade_date: str) -> dict:
    meta = {
        "trade_date": trade_date,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "regime": None, "regime_score": None, "position_pct": None,
        "regime_source": None, "regime_is_fallback": None,
    }
    # Prefer the stored regime snapshot; fall back to computing from breadth.
    rows = _q(con,
        "SELECT regime, regime_score, suggested_position_pct FROM market_regime_snapshot "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY generated_at DESC LIMIT 1", [trade_date])
    if rows and rows[0][0]:
        meta["regime"], meta["regime_score"], meta["position_pct"] = rows[0][0], rows[0][1], rows[0][2]
        meta["regime_source"] = "snapshot"
    # Breadth row (latest available up to trade_date) for computing/confirming regime.
    breadth = _q(con,
        "SELECT CAST(trade_date AS VARCHAR), limit_up_count, limit_down_count, rise_count, "
        "fall_count, consecutive_count, earning_effect_score, acute_drop_risk_score, is_fallback "
        "FROM v_market_state_inputs WHERE CAST(trade_date AS VARCHAR)<=? "
        "ORDER BY trade_date DESC LIMIT 1", [trade_date])
    if breadth:
        b = breadth[0]
        meta["breadth_date"] = b[0]
        row = {
            "limit_up_count": b[1], "limit_down_count": b[2], "rise_count": b[3],
            "fall_count": b[4], "consecutive_count": b[5],
            "earning_effect_score": b[6], "acute_drop_risk_score": b[7],
        }
        if not meta["regime"]:
            cls = classify_market_regime(row)
            meta["regime"], meta["regime_score"], meta["position_pct"] = (
                cls["regime"], cls["regime_score"], cls["suggested_position_pct"])
            meta["regime_source"] = "computed"
        meta["regime_is_fallback"] = bool(b[8]) if b[8] is not None else None
    if meta.get("regime_is_fallback"):
        meta["position_pct"] = 0
        meta["position_block_reason"] = "market_state_fallback"
    return meta


def _breadth(con, trade_date: str) -> dict:
    out = {"limit_up": None, "limit_down": None, "broken": None, "blown_rate": None,
           "rise": None, "fall": None, "consecutive_height": None,
           "earning_effect": None, "acute_drop": None, "is_fallback": None}
    rows = _q(con,
        "SELECT limit_up_count, limit_down_count, rise_count, fall_count, consecutive_count, "
        "broken_limit_up_count, blown_limit_up_rate, earning_effect_score, acute_drop_risk_score, "
        "is_fallback FROM v_market_state_inputs WHERE CAST(trade_date AS VARCHAR)=? LIMIT 1",
        [trade_date])
    if rows:
        r = rows[0]
        out.update({
            "limit_up": r[0], "limit_down": r[1], "rise": r[2], "fall": r[3],
            "consecutive_height": r[4], "broken": r[5], "blown_rate": _fnum(r[6]),
            "earning_effect": _fnum(r[7]), "acute_drop": _fnum(r[8]),
            "is_fallback": bool(r[9]) if r[9] is not None else None,
        })
    # Consecutive-board height is often NULL in the breadth row; derive it from the
    # day's limit-up ladder (max board level) so the hero never shows a bare dash.
    if out["consecutive_height"] is None:
        lh = _q(con, "SELECT max(board_level) FROM v_limit_pool "
                     "WHERE CAST(trade_date AS VARCHAR)=?", [trade_date])
        if lh and lh[0][0] is not None:
            out["consecutive_height"] = lh[0][0]
    return out


# ------------------------------------------------------------- emotion history
def _emotion_history(con, trade_date: str, days: int = 380) -> list:
    rows = _q(con,
        "SELECT CAST(trade_date AS VARCHAR), limit_up_count, limit_down_count, rise_count, "
        "fall_count, consecutive_count, earning_effect_score, acute_drop_risk_score, is_fallback, "
        "cgl, yll, success_rate "
        "FROM v_market_state_inputs WHERE CAST(trade_date AS VARCHAR)<=? "
        "ORDER BY trade_date DESC LIMIT ?", [trade_date, days])
    out = []
    for r in reversed(rows):  # chronological
        cls = classify_market_regime({
            "limit_up_count": r[1], "limit_down_count": r[2], "rise_count": r[3],
            "fall_count": r[4], "consecutive_count": r[5],
            "earning_effect_score": r[6], "acute_drop_risk_score": r[7],
        })
        out.append({
            "d": r[0], "lu": r[1] or 0, "ld": r[2] or 0, "rise": r[3] or 0, "fall": r[4] or 0,
            "ch": r[5] or 0, "ee": _fnum(r[6]), "ad": _fnum(r[7]),
            "regime": cls["regime"], "score": cls["regime_score"],
            "fb": bool(r[8]) if r[8] is not None else False,
            "cgl": _fnum(r[9]), "yll": _fnum(r[10]), "sr": _fnum(r[11]),
        })
    return out


# ----------------------------------------------------------------- candidates
def _candidate_funnel(con, trade_date: str) -> dict:
    out = {
        "pool_size": None,
        "evaluated": 0,
        "active_stage": None,
        "actionable": [],
        "executable": [],
        "pending": [],
        "blocked": [],
        "block_reasons": {},
        "trend": [],
        "close_passed": 0,
    }
    pool = _q(con,
        "SELECT stock_count, status FROM realtime_candidate_pool_snapshot "
        "WHERE CAST(trade_date AS VARCHAR)=? LIMIT 1", [trade_date])
    if pool:
        out["pool_size"] = pool[0][0]
        out["pool_status"] = pool[0][1]
    cols = _cols(con, "stock_candidate_stage_signal")
    if "trade_date" in cols:
        active = _q(
            con,
            "SELECT stage FROM stock_candidate_stage_signal "
            "WHERE CAST(trade_date AS VARCHAR)=? "
            "ORDER BY generated_at DESC NULLS LAST, as_of_time DESC NULLS LAST "
            "LIMIT 1",
            [trade_date],
        )
        active_stage = active[0][0] if active else None
        out["active_stage"] = active_stage
        if not active_stage:
            return out
        rows = _q(con,
            "SELECT stage, stock_code, stock_name, score, decision, is_actionable, "
            "is_executable, evidence_json, data_complete, signal_triggered, "
            "tradable, risk_approved FROM stock_candidate_stage_signal "
            "WHERE CAST(trade_date AS VARCHAR)=? AND stage=? "
            "ORDER BY score DESC NULLS LAST", [trade_date, active_stage])
        by_stock: dict = {}
        for r in rows:
            (
                stage,
                code,
                name,
                score,
                decision,
                actionable,
                executable,
                evid,
                data_complete,
                signal_triggered,
                tradable,
                risk_approved,
            ) = r
            reason = None
            try:
                ej = json.loads(evid) if evid else {}
                reason = ej.get("row_block_reason")
            except Exception:
                ej = {}
            st = by_stock.get(code)
            if st is None:
                st = {"code": code, "name": name, "score": None, "stages": [],
                      "actionable": False, "executable": False, "reason": None,
                      "decision": decision, "data_complete": bool(data_complete),
                      "signal_triggered": bool(signal_triggered),
                      "tradable": bool(tradable),
                      "risk_approved": bool(risk_approved)}
                by_stock[code] = st
            st["name"] = name or st["name"]
            sc = _fnum(score)
            if sc is not None and (st["score"] is None or sc > st["score"]):
                st["score"] = sc
            st["stages"].append({"stage": stage, "decision": decision,
                                 "actionable": bool(actionable)})
            st["actionable"] = st["actionable"] or bool(actionable)
            st["executable"] = st["executable"] or bool(executable)
            if not actionable:
                st["reason"] = reason or decision
            if not actionable:
                key = reason or decision or "unknown"
                out["block_reasons"][key] = out["block_reasons"].get(key, 0) + 1
        items = list(by_stock.values())
        out["evaluated"] = len(items)
        out["close_passed"] = sum(
            1 for it in items
            if any(s["stage"] == "close_decision" and s["actionable"] for s in it["stages"]))
        out["actionable"] = sorted((it for it in items if it["actionable"]),
                                   key=lambda x: (x["score"] is None, -(x["score"] or 0)))
        out["executable"] = sorted((it for it in items if it["executable"]),
                                   key=lambda x: (x["score"] is None, -(x["score"] or 0)))
        out["pending"] = sorted(
            (it for it in items if it["actionable"] and not it["executable"]),
            key=lambda x: (x["score"] is None, -(x["score"] or 0)),
        )
        out["blocked"] = sorted((it for it in items if not it["actionable"]),
                                key=lambda x: (x["score"] is None, -(x["score"] or 0)))
        # Funnel trend over available dates.
        trend = _q(con,
            "SELECT CAST(trade_date AS VARCHAR), count(*), "
            "sum(CASE WHEN coalesce(is_actionable,false) THEN 1 ELSE 0 END) "
            "FROM stock_candidate_stage_signal WHERE CAST(trade_date AS VARCHAR)<=? "
            "GROUP BY 1 ORDER BY 1 DESC LIMIT 12", [trade_date])
        out["trend"] = [{"d": t[0], "total": t[1] or 0, "actionable": t[2] or 0}
                        for t in reversed(trend)]
    return out


# --------------------------------------------------------------- capital flow
_SECTOR_KIND = {
    "tushare_dc_sector": "DC概念",
    "em_industry": "东财行业",
    "ths_concept_derived": "同花顺概念",
}


def _concept_limit_up_map(con, trade_date: str) -> tuple:
    """(concept_code -> limit-up member count today, covered concept set).

    THS concept membership ∩ today's limit-up pool.  ``covered`` lists every
    concept code with membership data, so callers can tell "zero limit-up
    members" (0) apart from "code system not covered by membership" (None).
    """
    lu_rows = _q(con,
        "WITH mem AS (SELECT concept_code, "
        "regexp_replace(CAST(stock_code AS VARCHAR), '[.].*$', '') AS sc "
        "FROM v_default_concept_stock_history WHERE trade_date=("
        "SELECT max(trade_date) FROM v_default_concept_stock_history "
        "WHERE trade_date<=CAST(? AS DATE))), "
        "lu AS (SELECT DISTINCT stock_code FROM v_limit_pool "
        "WHERE CAST(trade_date AS VARCHAR)=?) "
        "SELECT m.concept_code, count(DISTINCT m.sc) FROM mem m "
        "JOIN lu ON m.sc=lu.stock_code GROUP BY m.concept_code",
        [trade_date, trade_date])
    cov_rows = _q(con,
        "SELECT DISTINCT concept_code FROM v_default_concept_stock_history "
        "WHERE trade_date=(SELECT max(trade_date) FROM v_default_concept_stock_history "
        "WHERE trade_date<=CAST(? AS DATE))", [trade_date])
    return ({str(c): int(n) for c, n in lu_rows}, {str(c) for (c,) in cov_rows})


def _sector_flow_today(con, trade_date: str, names: dict, lu_map: dict | None = None,
                       lu_covered: set | None = None, top_n: int = 20) -> dict:
    # Canonical per-code row + mega-basket filter (融资融券/深股通 etc.).
    from trade_system.flow_ranking import sector_flow_rank_sql

    ranked_sql = sector_flow_rank_sql(exclude_mega=True)
    rows = _q(
        con,
        f"""
        SELECT sector_code, sector_name, main_net, super_net, change_pct, sector_type
        FROM ({ranked_sql})
        ORDER BY main_net DESC NULLS LAST
        """,
        [trade_date],
    )
    ranked = list(rows)
    top_in = ranked[:top_n]
    top_out = ranked[-top_n:][::-1] if len(ranked) > top_n else list(reversed(ranked))
    lu_map, lu_covered = lu_map or {}, lu_covered or set()
    kinds: dict = {}
    for r in rows:
        kinds[r[5] or "other"] = kinds.get(r[5] or "other", 0) + 1

    def pack(r):
        code = str(r[0])
        lu = lu_map.get(code, 0) if code in lu_covered else None
        return {
            "code": code,
            "name": r[1] or names.get(code, "") or code,
            "main": _fnum(r[2]),
            "super": _fnum(r[3]),
            "lu": lu,
            "kind": _SECTOR_KIND.get(r[5], r[5] or ""),
        }

    return {
        "inflow": [pack(r) for r in top_in],
        "outflow": [pack(r) for r in top_out],
        "count": len(rows),
        "kinds": kinds,
    }


def _stock_flow_today(con, trade_date: str, names: dict, top_n: int = 15) -> dict:
    # One row per stock with full-market providers preferred over KPL focus.
    from trade_system.flow_ranking import stock_flow_rank_sql, stock_provider_rank

    ranked_sql = stock_flow_rank_sql()
    rows = _q(
        con,
        f"""
        SELECT stock_code, main_net, super_net, large_net, change_pct, turnover, provider
        FROM ({ranked_sql})
        """,
        [trade_date],
    )
    items = []
    for r in rows:
        code = str(r[0])
        items.append(
            {
                "code": code,
                "name": names.get(code, ""),
                "main": _fnum(r[1]),
                "super": _fnum(r[2]),
                "large": _fnum(r[3]),
                "chg": _fnum(r[4]),
                "turn": _fnum(r[5]),
                "provider": r[6],
                "_rank": stock_provider_rank(r[6]),
            }
        )
    ranked = sorted(
        items,
        key=lambda x: (x["main"] is None, x["main"] or 0),
        reverse=True,
    )

    def pack(it):
        return {
            k: it[k]
            for k in ("code", "name", "main", "super", "large", "chg", "turn", "provider")
        }

    return {
        "inflow": [pack(it) for it in ranked[:top_n]],
        "outflow": [pack(it) for it in ranked[-top_n:][::-1]],
        "count": len(items),
    }


def _sector_flow_history(con, trade_date: str, top_n: int = 18, days: int = 60) -> dict:
    """Heatmap: top sectors (by today's flow) x recent trading days."""
    top = _q(con,
        "SELECT sector_code, sector_name FROM v_sector_capital "
        "WHERE CAST(trade_date AS VARCHAR)=? AND main_net_inflow IS NOT NULL "
        "ORDER BY main_net_inflow DESC LIMIT ?", [trade_date, top_n])
    if not top:
        return {"dates": [], "sectors": [], "matrix": []}
    codes = [str(t[0]) for t in top]
    names = [t[1] or str(t[0]) for t in top]
    placeholders = ",".join("?" for _ in codes)
    dates_rows = _q(con,
        "SELECT DISTINCT CAST(trade_date AS VARCHAR) FROM v_sector_capital "
        "WHERE CAST(trade_date AS VARCHAR)<=? ORDER BY trade_date DESC LIMIT ?",
        [trade_date, days])
    dates = [d[0] for d in reversed(dates_rows)]
    flow = _q(con,
        f"SELECT CAST(trade_date AS VARCHAR), sector_code, main_net_inflow FROM v_sector_capital "
        f"WHERE sector_code IN ({placeholders}) AND CAST(trade_date AS VARCHAR)<=? "
        f"ORDER BY trade_date", [*codes, trade_date])
    lookup: dict = {}
    for d, code, val in flow:
        lookup[(d, str(code))] = _fnum(val)
    matrix = [[lookup.get((d, c)) for d in dates] for c in codes]
    return {"dates": dates, "sectors": names, "codes": codes, "matrix": matrix}


# --------------------------------------------------------------------- ladder
def _limit_up_ladder(con, trade_date: str) -> list:
    rows = _q(con,
        "SELECT board_level, stock_code, stock_name, limit_up_time FROM v_limit_pool "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY board_level DESC NULLS LAST", [trade_date])
    levels: dict = {}
    for r in rows:
        lvl = int(r[0]) if r[0] is not None else 1
        levels.setdefault(lvl, []).append(
            {"code": r[1], "name": r[2] or "", "time": _fmt_limit_time(r[3])})
    out = [{"level": lvl, "count": len(stocks), "stocks": stocks}
           for lvl, stocks in sorted(levels.items(), reverse=True)]
    return out


# ------------------------------------------------------------------- concepts
def _concepts_today(con, trade_date: str, lu_map: dict | None = None,
                    lu_covered: set | None = None, top_n: int = 20) -> list:
    rows = _q(con,
        "SELECT sector_code, sector_name, mainline_score, strength_value, limit_up_count, "
        "main_net_inflow, component_count, boom_reason FROM v_theme_mainline_evidence "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY mainline_score DESC NULLS LAST LIMIT ?",
        [trade_date, top_n])
    # limit_up_count is normally NULL (v_sector_capital hardcodes NULL); the
    # membership ∩ limit-up-pool map is computed once in build_terminal_context
    # and shared with the sector-flow section.  lu stays None for code systems
    # absent from THS membership (BK/EM codes) -- never a misleading 0.
    lu_map, lu_covered = lu_map or {}, lu_covered or set()
    out = []
    for r in rows:
        code = str(r[0])
        if r[4] is not None:
            lu = r[4]
        elif code in lu_covered:
            lu = lu_map.get(code, 0)  # covered but no limit-up members today
        else:
            lu = None
        members = r[6] if r[6] else None  # 0 == join miss for non-THS codes
        out.append({"code": r[0], "name": r[1] or r[0], "score": _fnum(r[2]),
                    "strength": _fnum(r[3]), "lu": lu, "main": _fnum(r[5]),
                    "members": members, "reason": r[7]})
    return out


# ----------------------------------------------------------------- index kline
def _index_kline(con, days: int = 250) -> dict:
    out = {}
    for code, name in INDEX_NAMES.items():
        rows = _q(con,
            "SELECT CAST(date AS VARCHAR), open, close, low, high, change_pct FROM index_kline "
            "WHERE index_code=? ORDER BY date DESC LIMIT ?", [code, days])
        data = [[r[0], _fnum(r[1]), _fnum(r[2]), _fnum(r[3]), _fnum(r[4]), _fnum(r[5])]
                for r in reversed(rows)]
        # The KPL kline payload carries no change_pct (stored as 0/NULL);
        # derive the latest day's change from consecutive closes so the index
        # card never shows a bogus +0.00%.
        if len(data) >= 2 and not data[-1][5] and data[-1][2] and data[-2][2]:
            data[-1][5] = round((data[-1][2] / data[-2][2] - 1) * 100, 2)
        out[code] = {"name": name, "data": data}
    return out


# -------------------------------------------------------------------- auction
def _auction(con, trade_date: str) -> dict:
    out = {"anomalies": [], "tape": [], "tape_stocks": [],
           "tape_max_points": 0, "anomaly_latest": None, "tape_source": None}
    anom = _q(con,
        "SELECT stock_code, anomaly_type, anomaly_value FROM auction_bidding_anomaly "
        "WHERE CAST(date AS VARCHAR)=? LIMIT 20", [trade_date])
    out["anomalies"] = [{"code": r[0], "type": r[1], "value": _fnum(r[2])} for r in anom]
    # Auction tape: prefer KPL's dense tick series (full 09:15-09:25 sequence,
    # collected after close); fall back to Tencent's sparse quote snapshots.
    tick_stocks = _q(con,
        "SELECT stock_code, count(*) AS n FROM auction_tick "
        "WHERE CAST(date AS VARCHAR)=? GROUP BY 1 ORDER BY n DESC LIMIT 6", [trade_date])
    if tick_stocks:
        for s in tick_stocks:
            code = str(s[0])
            tape = _q(con,
                "SELECT CAST(time AS VARCHAR), price, volume FROM auction_tick "
                "WHERE CAST(date AS VARCHAR)=? AND stock_code=? ORDER BY time",
                [trade_date, code])
            out["tape_stocks"].append(code)
            out["tape"].append([[t[0], _fnum(t[1]), _fnum(t[2]), None] for t in tape])
        out["tape_source"] = "KPL逐笔"
    elif _has(con, "auction_quote_snapshot"):
        stocks = _q(con,
            "SELECT stock_code, max(cumulative_volume) AS v FROM auction_quote_snapshot "
            "WHERE CAST(date AS VARCHAR)=? GROUP BY 1 ORDER BY v DESC NULLS LAST LIMIT 6",
            [trade_date])
        for s in stocks:
            code = str(s[0])
            tape = _q(con,
                "SELECT CAST(quote_time AS VARCHAR), indicative_price, cumulative_volume, "
                "order_imbalance FROM auction_quote_snapshot "
                "WHERE CAST(date AS VARCHAR)=? AND stock_code=? ORDER BY quote_time",
                [trade_date, code])
            out["tape_stocks"].append(code)
            out["tape"].append([[t[0], _fnum(t[1]), _fnum(t[2]), _fnum(t[3])] for t in tape])
        out["tape_source"] = "腾讯快照"
    out["tape_max_points"] = max((len(t) for t in out["tape"]), default=0)
    al = _q(con, "SELECT max(CAST(date AS VARCHAR)) FROM auction_bidding_anomaly")
    out["anomaly_latest"] = al[0][0] if al else None
    return out


# ------------------------------------------------------------------------ lhb
def _lhb(con, trade_date: str, top_n: int = 20) -> list:
    rows = _q(con,
        "SELECT CAST(trade_date AS VARCHAR), stock_code, stock_name, reason, net_amount, "
        "broker_count, youzi_buy_amount, agency_buy_amount, on_lhb_probability "
        "FROM v_lhb_review_evidence WHERE CAST(trade_date AS VARCHAR)<=? "
        "ORDER BY trade_date DESC, net_amount DESC NULLS LAST LIMIT ?", [trade_date, top_n])
    return [{"date": r[0], "code": r[1], "name": r[2] or r[1], "reason": r[3],
             "net": _fnum(r[4]), "brokers": r[5], "youzi": _fnum(r[6]),
             "agency": _fnum(r[7]), "prob": _fnum(r[8])} for r in rows]


# ----------------------------------------------------------------- data health
def _data_health(con, trade_date: str, db_path) -> dict:
    out = {"chains": [], "gaps": [], "reconciliation": {}, "freshness": []}
    try:
        from trade_system.data_chain import assess_data_chains
        out["chains"] = assess_data_chains(db_path)
    except Exception as exc:
        logger.warning("assess_data_chains failed; data-health panel degraded: %s", exc)
    recon = _q(con,
        "SELECT status, reference_rows, primary_provider, reference_provider, overlap_reference_pct "
        "FROM intraday_stock_flow_reconciliation WHERE CAST(trade_date AS VARCHAR)=? LIMIT 1",
        [trade_date])
    if recon:
        r = recon[0]
        out["reconciliation"] = {"status": r[0], "reference_rows": r[1],
                                 "primary": r[2], "reference": r[3], "overlap_pct": _fnum(r[4])}
    else:
        out["reconciliation"] = {"status": "not_run", "reference_rows": 0}
    indep = _q(con,
        "SELECT count(DISTINCT stock_code) FROM multi_source_stock_flow "
        "WHERE source_date=CAST(? AS DATE) AND provider='tushare'", [trade_date])
    out["independent_source_codes"] = indep[0][0] if indep else 0
    # Freshness of a few key tables (latest fetched/source date).
    fresh_specs = [
        ("市场状态", "SELECT max(CAST(date AS VARCHAR)) FROM daily_summary WHERE CAST(date AS VARCHAR)<=?"),
        ("个股资金流", "SELECT max(CAST(source_date AS VARCHAR)) FROM multi_source_stock_flow WHERE source_date<=CAST(? AS DATE)"),
        ("板块资金流", "SELECT max(CAST(source_date AS VARCHAR)) FROM multi_source_sector_flow WHERE source_date<=CAST(? AS DATE)"),
        ("候选信号", "SELECT max(CAST(trade_date AS VARCHAR)) FROM stock_candidate_stage_signal WHERE CAST(trade_date AS VARCHAR)<=?"),
        ("TuShare日线", "SELECT max(CAST(date AS VARCHAR)) FROM tushare_daily WHERE CAST(date AS VARCHAR)<=?"),
        ("指数K线", "SELECT max(CAST(date AS VARCHAR)) FROM index_kline WHERE CAST(date AS VARCHAR)<=?"),
        ("龙虎榜", "SELECT max(CAST(trade_date AS VARCHAR)) FROM v_lhb_daily WHERE CAST(trade_date AS VARCHAR)<=?"),
        ("竞价快照", "SELECT max(CAST(date AS VARCHAR)) FROM auction_quote_snapshot WHERE CAST(date AS VARCHAR)<=?"),
    ]
    for label, sql in fresh_specs:
        rows = _q(con, sql, [trade_date])
        out["freshness"].append({"label": label, "latest": rows[0][0] if rows else None})
    return out


# --------------------------------------------------------------------- alerts
def _alerts(con, trade_date: str) -> list:
    rows = _q(con,
        "SELECT severity, category, message, evidence_json FROM alert_events "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY severity, category", [trade_date])
    return [{"severity": r[0], "category": r[1], "message": r[2], "evidence": r[3]}
            for r in rows]


# ----------------------------------------------------------- stage validation
def _stage_validation(db_path) -> dict:
    """Per-stage signal hit-rate / forward-return statistics (T+1 caliber)."""
    try:
        from trade_system.review_statistics import build_daily_review_statistics
        return build_daily_review_statistics(db_path) or {}
    except Exception:
        return {}


# --------------------------------------------------------------- plan console
def _plan_console(con, trade_date: str) -> dict:
    """Trading plans + watchlist theses + risk state for the review day."""
    plans = _q(con,
        "SELECT stock_code, stock_name, setup_type, max_position_pct, status, "
        "entry_condition, stop_condition FROM trade_plan "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY max_position_pct DESC NULLS LAST",
        [trade_date])
    watch = _q(con,
        "SELECT stock_code, thesis, invalidation, priority FROM watchlist "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY priority", [trade_date])
    risk = _q(con,
        "SELECT risk_state, total_position_pct, max_single_position_pct, "
        "max_sector_position_pct FROM risk_snapshot "
        "WHERE CAST(trade_date AS VARCHAR)=? ORDER BY created_at DESC LIMIT 1",
        [trade_date])
    watch_map = {str(r[0]): {"thesis": r[1], "invalidation": r[2], "priority": r[3]}
                 for r in watch}
    rows = []
    for code, name, setup, maxpos, status, entry, stop in plans:
        w = watch_map.get(str(code), {})
        rows.append({"code": code, "name": name or "",
                     "setup": cn(SETUP_TYPE_CN, setup),
                     "maxpos": _fnum(maxpos),
                     "status": zh_text(cn(PLAN_STATUS_CN, status)),
                     "entry": zh_text(entry), "stop": zh_text(stop),
                     "thesis": zh_text(w.get("thesis")),
                     "invalidation": zh_text(w.get("invalidation"))})
    risk_row = risk[0] if risk else None
    return {"rows": rows,
            "risk": ({"state": risk_row[0], "total": _fnum(risk_row[1]),
                      "single": _fnum(risk_row[2]), "sector": _fnum(risk_row[3])}
                     if risk_row else None)}


# -------------------------------------------------------------- blown history
def _blown_history(con, trade_date: str, days: int = 267) -> list:
    rows = _q(con,
        "SELECT CAST(date AS VARCHAR), broken_limit_up_count, blown_limit_up_count, "
        "blown_limit_up_rate FROM market_rise_fall WHERE CAST(date AS VARCHAR)<=? "
        "ORDER BY date DESC LIMIT ?", [trade_date, days])
    return [{"d": r[0], "broken": r[1], "blown": r[2], "rate": _fnum(r[3])}
            for r in reversed(rows)]


# ------------------------------------------------------------ pipeline matrix
def _pipeline_matrix(db_path, trade_date: str) -> dict:
    try:
        from trade_system.p0_observation import audit_five_day_observation
        ob = audit_five_day_observation(str(db_path), "reports", trade_date) or {}
    except Exception:
        return {}
    daily = ob.get("daily") or []
    return {"sessions": [{"d": s.get("trade_date"), "passed": s.get("passed"),
                          "checks": s.get("checks") or {}} for s in daily],
            "consecutive": ob.get("consecutive_passes"),
            "ready_for_p1": ob.get("ready_for_p1")}


# -------------------------------------------------------------- flow coverage
def _flow_coverage(db_path, trade_date: str) -> dict:
    try:
        from trade_system.capital_flow_health import assess_capital_flow_health
        ch = assess_capital_flow_health(str(db_path), trade_date) or {}
    except Exception:
        return {}
    def brief(block):
        if not isinstance(block, dict):
            return None
        rels = [{"relation": r.get("relation"), "rows": r.get("rows"),
                 "codes": r.get("codes"), "status": r.get("status")}
                for r in (block.get("relations") or [])]
        return {"ready": block.get("ready"), "observed": block.get("observed_codes"),
                "expected": block.get("expected_codes"),
                "coverage": _fnum(block.get("coverage_pct")), "relations": rels}
    return {"ready": ch.get("ready"), "stock_flow": brief(ch.get("stock_flow")),
            "sector_flow": brief(ch.get("sector_flow")),
            "reconciliation": ch.get("reconciliation") or {}}


# ------------------------------------------------------- auction confirmation
def _auction_confirmation(db_path, trade_date: str, names: dict) -> dict:
    try:
        from trade_system.auction_evidence import build_auction_evidence_status
        st = build_auction_evidence_status(str(db_path), trade_date) or {}
    except Exception:
        return {}
    rows = st.get("rows") or []
    for r in rows:
        r["name"] = names.get(str(r.get("stock_code")), "")
    rows = sorted(rows, key=lambda r: (r.get("auction_strength") is None,
                                       -(float(r.get("auction_strength") or 0))))
    return {"rows": rows[:25], "counts": st.get("counts") or {},
            "gaps": st.get("gaps") or []}


# ------------------------------------------------------------- promotion stats
def _promotion_stats(con, trade_date: str, days: int = 15) -> dict:
    """Consecutive-limit-up promotion rates + prior-day limit-up premium."""
    rows = _q(con,
        "SELECT CAST(trade_date AS VARCHAR), stock_code, max(board_level) "
        "FROM v_limit_pool WHERE CAST(trade_date AS VARCHAR)<=? "
        "GROUP BY 1, 2 ORDER BY 1", [trade_date])
    by_date: dict = {}
    for d, code, lvl in rows:
        by_date.setdefault(d, {})[str(code)] = int(lvl or 1)
    dates = sorted(by_date)[-days:]
    series = []
    for i in range(len(dates) - 1):
        d, nd = dates[i], dates[i + 1]
        cur, nxt = by_date[d], by_date[nd]
        l1 = [c for c, l in cur.items() if l == 1]
        l1_up = sum(1 for c in l1 if nxt.get(c, 0) >= 2)
        hi = [(c, l) for c, l in cur.items() if l >= 2]
        hi_up = sum(1 for c, l in hi if nxt.get(c, 0) > l)
        series.append({"d": d,
                       "l1_rate": round(l1_up * 100.0 / len(l1), 1) if l1 else None,
                       "hi_rate": round(hi_up * 100.0 / len(hi), 1) if hi else None,
                       "l1": len(l1), "hi": len(hi)})
    premium = None
    if len(dates) >= 2:
        prev_codes = list(by_date[dates[-2]].keys())
        if prev_codes:
            ph = ",".join("?" for _ in prev_codes)
            pr = _q(con,
                f"SELECT avg(change_pct) FROM multi_source_stock_flow "
                f"WHERE source_date=CAST(? AS DATE) AND change_pct IS NOT NULL "
                f"AND stock_code IN ({ph})", [dates[-1], *prev_codes])
            premium = _fnum(pr[0][0]) if pr else None
    return {"series": series, "premium": premium,
            "latest": series[-1] if series else {}}


# ------------------------------------------------------------------ northbound
def _theme_timeline_data(con, trade_date: str, days: int = 20, top_n: int = 12) -> dict:
    has = con.execute("SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name='ths_concept_stock_history'").fetchone()[0]
    if not has:
        return {"dates": [], "rows": [], "coverage": 0}
    dates = [str(r[0]) for r in con.execute(
        """SELECT DISTINCT CAST(trade_date AS DATE) FROM ths_concept_stock_history
           WHERE date_verified AND CAST(trade_date AS DATE) <= ?
           ORDER BY 1 DESC LIMIT ?""", [trade_date, days]).fetchall()]
    if not dates:
        return {"dates": [], "rows": [], "coverage": 0}
    marks = ",".join("?" * len(dates))
    coverage = con.execute(
        """SELECT count(DISTINCT concept_code) FROM ths_concept_daily
           WHERE CAST(trade_date AS DATE)=?""", [dates[0]]).fetchone()[0]
    rows_raw = con.execute(
        f"""SELECT concept_name, CAST(trade_date AS VARCHAR), count(*) AS zt
            FROM ths_concept_stock_history
            WHERE date_verified AND CAST(trade_date AS DATE) IN ({marks})
            GROUP BY 1, 2""", dates).fetchall()
    by_theme = {}
    for name, d, zt in rows_raw:
        by_theme.setdefault(name, {})[d] = zt
    totals = {n: sum(v.values()) for n, v in by_theme.items()}
    top = sorted(totals, key=lambda n: -totals[n])[:top_n]
    rows = [{"name": n, "cells": [by_theme[n].get(d, 0) for d in reversed(dates)],
             "total": totals[n]} for n in top]
    return {"dates": list(reversed(dates)), "rows": rows,
            "coverage": int(coverage)}


def _northbound(con, trade_date: str) -> dict:
    """Return only structurally aligned THS沪/深股通 minute fields.

    Daily northbound net-buy history is discontinued.  The intraday endpoint
    has also returned mismatched arrays, so an unaligned payload is not shown
    as a trading signal.
    """
    out = {"intraday": None, "intraday_date": None}
    rows = _q(con,
        "SELECT CAST(source_date AS VARCHAR), payload_json FROM multi_source_observation "
        "WHERE data_type='northbound' AND coalesce(status,'') NOT IN ('failed','schema_error') "
        "AND CAST(source_date AS VARCHAR)<=? "
        "ORDER BY source_date DESC, rowid DESC LIMIT 1", [trade_date])
    if not rows:
        return out
    try:
        p = json.loads(rows[0][1]) if isinstance(rows[0][1], str) else rows[0][1]
    except Exception:
        return out
    if not isinstance(p, dict) or not p.get("time"):
        return out
    times = list(p.get("time") or [])
    hgt = [_fnum(v) for v in (p.get("hgt") or [])]
    sgt = [_fnum(v) for v in (p.get("sgt") or [])]
    if not times or len(times) != len(hgt) or len(times) != len(sgt):
        return out
    if any(v is None for v in hgt + sgt):
        return out
    total = [round(h + s, 4) for h, s in zip(hgt, sgt)]
    out["intraday"] = {"time": times, "hgt": hgt, "sgt": sgt, "total": total,
                       "latest_total": total[-1]}
    out["intraday_date"] = rows[0][0]
    return out


# --------------------------------------------------------------------- context
def _cycle_state(con, trade_date: str) -> dict:
    row = con.execute(
        """SELECT CAST(trade_date AS VARCHAR), phase, score,
                  limit_up_count, premium_pct, promotion_rate
           FROM market_cycle_phase WHERE trade_date <= ?
           ORDER BY trade_date DESC LIMIT 1""",
        [trade_date],
    ).fetchone()
    if not row:
        return {}
    return {"trade_date": row[0], "phase": row[1],
            "score": _fnum(row[2]), "limit_up_count": row[3],
            "premium_pct": _fnum(row[4]), "promotion_rate": _fnum(row[5])}


def _advisory_cap(phase: str | None) -> int | None:
    from trade_system.signal_attribution import PHASE_POSITION_CAP_PCT
    return PHASE_POSITION_CAP_PCT.get(phase or "")


def synthesize_strategy(phase: str | None, premium: float | None,
                        promo_first: float | None) -> str:
    """Rule-based one-liner: what kind of day tomorrow likely is."""
    p = PHASE_CN.get(phase or "", None)
    prem = premium if premium is not None else 0.0
    if phase == "ice":
        return "冰点期：以观察为主，等待首板带动情绪修复，严禁接力高位。"
    if phase == "retreat":
        return "退潮期：只做低位首板或空仓休息，高标一律回避。"
    if prem >= 3 and phase == "climax":
        return "高潮期：打板期望值高，可适度参与主线龙头，注意分歧信号随时撤退。"
    if prem >= 1:
        return "发酵期：赚钱效应扩散，优先主线低位补涨与强趋势股低吸。"
    if -1 < prem < 1:
        return f"{'分歧' if phase == 'divergence' else (p or '震荡')}市：控制仓位试错，等方向明朗再加。"
    return "溢价偏弱：降低预期，多看少动，重点跟踪亏钱效应是否收敛。"


def _premium_matrix_section_data(con, trade_date: str) -> tuple[str, list]:
    row = con.execute(
        """SELECT max(CAST(prev_trade_date AS VARCHAR)) FROM limit_premium_matrix
           WHERE prev_trade_date <= ?""", [trade_date]).fetchone()
    if not row or not row[0]:
        return "", []
    rows = con.execute(
        """SELECT board_bucket, sample_size, avg_pct, median_pct, win_rate
           FROM limit_premium_matrix WHERE prev_trade_date = ?
           ORDER BY CASE WHEN board_bucket='_all' THEN 0 ELSE 1 END,
                    TRY_CAST(board_bucket AS INTEGER) NULLS LAST""",
        [row[0]]).fetchall()
    return str(row[0]), rows


def _picks_top(con, trade_date: str, n: int = 10) -> list:
    has = con.execute("SELECT count(*) FROM information_schema.tables "
                      "WHERE table_name='daily_stock_picks'").fetchone()[0]
    if not has:
        return []
    latest = con.execute(
        "SELECT max(trade_date) FROM daily_stock_picks "
        "WHERE trade_date <= ?", [trade_date]).fetchone()[0]
    if not latest:
        return []
    return con.execute(
        """SELECT rank, stock_code, stock_name, total_score, board,
                  limit_up_reason, llm_bull_case, llm_risk, llm_watch_condition
           FROM daily_stock_picks WHERE trade_date=? ORDER BY rank LIMIT ?""",
        [latest, n]).fetchall()


def _qlib_screen_rows(con) -> tuple[str, str, list]:
    reg = con.execute("SELECT status FROM qlib_model_registry "
                      "ORDER BY model_id DESC LIMIT 1").fetchone()
    status = reg[0] if reg else "unregistered"
    head = con.execute(
        """SELECT trade_date, model_id FROM qlib_prediction
           ORDER BY trade_date DESC LIMIT 1""").fetchone()
    if not head:
        return "", status, []
    rows = con.execute(
        """SELECT symbol, score FROM qlib_prediction
           WHERE trade_date=? AND model_id=? ORDER BY score DESC LIMIT 10""",
        [head[0], head[1]]).fetchall()
    return str(head[0]), str(head[1]), rows


def _operator_stats(con, trade_date: str) -> dict:
    row = con.execute(
        """SELECT count(*),
                  avg(CASE WHEN net_return_pct>0 THEN 1.0 ELSE 0 END),
                  avg(net_return_pct)
           FROM operator_trade_outcome
           WHERE execution_status IN ('executed','filled')
             AND net_return_pct IS NOT NULL
             AND substr(CAST(created_at AS VARCHAR),1,7)=substr(?,1,7)""",
        [trade_date]).fetchone()
    pf = con.execute(
        """SELECT sum(CASE WHEN net_return_pct>0 THEN net_return_pct ELSE 0 END)
                  / NULLIF(-sum(CASE WHEN net_return_pct<=0 THEN net_return_pct ELSE 0 END),0)
           FROM operator_trade_outcome
           WHERE execution_status IN ('executed','filled')
             AND net_return_pct IS NOT NULL
             AND substr(CAST(created_at AS VARCHAR),1,7)=substr(?,1,7)""",
        [trade_date]).fetchone()[0]
    return {"n": row[0], "win": row[1], "avg_ret": row[2], "pf": _fnum(pf)}


def _journal_recent(con, trade_date: str, n: int = 5) -> list:
    return con.execute(
        """SELECT CAST(trade_date AS VARCHAR), note, tags FROM market_journal
           WHERE trade_date <= ? ORDER BY trade_date DESC LIMIT ?""",
        [trade_date, n]).fetchall()


def _plan_vs_actual(con, trade_date: str) -> list:
    return con.execute(
        """
        WITH prev AS (
            SELECT stock_code, max(stock_name) AS stock_name FROM trade_plan
            WHERE CAST(trade_date AS VARCHAR) = (
                SELECT max(CAST(trade_date AS VARCHAR)) FROM trade_plan
                WHERE CAST(trade_date AS VARCHAR) < ?)
            GROUP BY stock_code),
        k0 AS (
            SELECT stock_code, close AS pc FROM v_kline_daily
            WHERE ktype='D' AND CAST(trade_date AS DATE) = (
                SELECT max(CAST(trade_date AS DATE)) FROM v_kline_daily
                WHERE CAST(trade_date AS DATE) < ?)),
        k1 AS (
            SELECT stock_code, open, high, close, change_pct FROM v_kline_daily
            WHERE ktype='D' AND CAST(trade_date AS DATE) = ?)
        SELECT p.stock_code, max(p.stock_name),
               round((k1.open/k0.pc-1)*100,2), round(k1.change_pct,2),
               round((k1.high/k0.pc-1)*100,2),
               CASE WHEN k1.high>=k0.pc*1.05 THEN '给了介入点'
                    WHEN k1.open>k0.pc*1.07 THEN '高开过大难接'
                    ELSE '未给介入点' END
        FROM prev p JOIN k1 ON k1.stock_code=p.stock_code
        LEFT JOIN k0 ON k0.stock_code=p.stock_code
        GROUP BY p.stock_code,k1.open,k1.high,k1.close,k1.change_pct,k0.pc
        ORDER BY 3 DESC NULLS LAST LIMIT 15
        """, [trade_date]*3).fetchall()


def _first_seal_buckets(con, trade_date: str) -> list:
    """Buckets on normalized HHMM integer (source format is 'HH:MM')."""
    buckets = [("集合竞价秒板", 925, 926),
               ("早盘抢板", 926, 1000),
               ("上午中段", 1000, 1130),
               ("午后", 1300, 1400),
               ("尾盘偷袭", 1400, 1500)]
    out = []
    for label, lo, hi in buckets:
        n = con.execute(
            """SELECT count(*) FROM official_limit_pool
               WHERE trade_date=? AND continue_day_cnt IS NOT NULL
                 AND CAST(replace(limit_up_time,':','') AS INTEGER) BETWEEN ? AND ?""",
            [trade_date, lo, hi]).fetchone()[0]
        out.append((label, int(n)))
    return out


def _loss_trend(con, trade_date: str, days: int = 20) -> list:
    return con.execute(
        """SELECT CAST(date AS VARCHAR), limit_down_count, blown_limit_up_rate
           FROM market_limit_up_down_summary
           WHERE date <= ? ORDER BY date DESC LIMIT ?""",
        [trade_date, days]).fetchall()


def _loss_trend(con, trade_date: str, days: int = 20) -> list:
    return con.execute(
        """SELECT CAST(date AS VARCHAR), limit_down_count, blown_limit_up_rate
           FROM market_limit_up_down_summary
           WHERE date <= ? ORDER BY date DESC LIMIT ?""",
        [trade_date, days]).fetchall()


def _cycle_series(con, trade_date: str, days: int = 60) -> list:
    return con.execute(
        """SELECT CAST(trade_date AS VARCHAR), phase, score,
                  limit_up_count, premium_pct, promotion_rate
           FROM market_cycle_phase WHERE trade_date <= ?
           ORDER BY trade_date DESC LIMIT ?""",
        [trade_date, days]).fetchall()


def build_terminal_context(db_path: str | Path, trade_date: str | None = None) -> dict:
    trade_date = trade_date or date.today().isoformat()
    con = _connect(db_path)
    if con is None:
        return {"meta": {"trade_date": trade_date,
                         "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                         "db_locked": True}, "breadth": {}, "emotion_history": [],
                "candidates": {}, "sector_flow_today": {}, "stock_flow_today": {},
                "sector_flow_history": {}, "ladder": [], "concepts": [],
                "index_kline": {}, "auction": {}, "lhb": [], "data_health": {},
                "alerts": [], "stage_validation": {}, "plan_console": {},
                "blown_history": [], "promotion": {}, "auction_confirmation": {},
                "flow_coverage": {}, "pipeline_matrix": {}, "northbound": {},
            "cycle_state": {}, "premium_matrix": ("", []), "picks_top": [],
            "qlib_screen": ("", "", []), "operator_stats": {}, "journal": [],
            "plan_vs_actual": [], "first_seal": [], "loss_trend": [],
            "cycle_series": [],
            "strategy_line": ""}
    try:
        names = _name_map(con)
        lu_map, lu_covered = _concept_limit_up_map(con, trade_date)
        ctx = {
            "meta": _market_meta(con, trade_date),
            "breadth": _breadth(con, trade_date),
            "emotion_history": _emotion_history(con, trade_date),
            "candidates": _candidate_funnel(con, trade_date),
            "sector_flow_today": _sector_flow_today(con, trade_date, names, lu_map, lu_covered),
            "stock_flow_today": _stock_flow_today(con, trade_date, names),
            "sector_flow_history": _sector_flow_history(con, trade_date),
            "ladder": _limit_up_ladder(con, trade_date),
            "concepts": _concepts_today(con, trade_date, lu_map, lu_covered),
            "index_kline": _index_kline(con),
            "auction": _auction(con, trade_date),
            "lhb": _lhb(con, trade_date),
            "data_health": _data_health(con, trade_date, db_path),
            "alerts": _alerts(con, trade_date),
            "stage_validation": _stage_validation(db_path),
            "plan_console": _plan_console(con, trade_date),
            "blown_history": _blown_history(con, trade_date),
            "promotion": _promotion_stats(con, trade_date),
            "northbound": _northbound(con, trade_date),
            "theme_timeline": _theme_timeline_data(con, trade_date),
        }
        active_stage = ctx["candidates"].get("active_stage")
        readiness_stage = {
            "premarket_pool": "premarket",
            "auction_confirmation": "auction",
            "intraday_strength": "intraday",
            "close_decision": "close",
        }.get(active_stage, "intraday")
        ctx["readiness"] = assess_trade_date_readiness(
            con,
            trade_date,
            stage=readiness_stage,
        )
        try:
            cycle = _cycle_state(con, trade_date)
            ctx["cycle_state"] = cycle
            ctx["premium_matrix"] = _premium_matrix_section_data(con, trade_date)
            ctx["picks_top"] = _picks_top(con, trade_date)
            ctx["qlib_screen"] = _qlib_screen_rows(con)
            ctx["operator_stats"] = _operator_stats(con, trade_date)
            ctx["journal"] = _journal_recent(con, trade_date)
            ctx["plan_vs_actual"] = _plan_vs_actual(con, trade_date)
            ctx["first_seal"] = _first_seal_buckets(con, trade_date)
            ctx["loss_trend"] = _loss_trend(con, trade_date)
            ctx["cycle_series"] = _cycle_series(con, trade_date)
        except duckdb.Error:
            # Maintenance/test databases may miss the analytics tables.
            cycle = {}
            pass
        promo_first = None
        if isinstance(cycle.get("promotion_rate"), (int, float)):
            promo_first = float(cycle["promotion_rate"])
        ctx["strategy_line"] = synthesize_strategy(
            cycle.get("phase"),
            cycle.get("premium_pct") if isinstance(cycle.get("premium_pct"), (int, float)) else None,
            promo_first)
    finally:
        con.close()
    # Fetchers that open their own config-governed connections (via
    # base.connect_duckdb) must run after the plain connection is closed:
    # DuckDB forbids two connections to the same file with different
    # configurations in one process.
    ctx["auction_confirmation"] = _auction_confirmation(db_path, trade_date, names)
    ctx["flow_coverage"] = _flow_coverage(db_path, trade_date)
    ctx["pipeline_matrix"] = _pipeline_matrix(db_path, trade_date)
    return ctx


# ===========================================================================
# RENDERER — "trading war-room" static HTML + ECharts
# ===========================================================================

# A-share convention: red = up / inflow, green = down / outflow.
REGIME_COLORS = {
    "高潮": "#ff5b6a", "主升": "#f0524a", "启动": "#f0b90b",
    "震荡": "#8fa3c0", "退潮": "#2ebd85", "冰点": "#4cc3ff", "数据缺失": "#5b6b85",
}


def _fmt_money(v) -> str:
    n = _fnum(v)
    if n is None:
        return "—"
    a, sign = abs(n), ("-" if n < 0 else "")
    if a >= 1e8:
        return f"{sign}{a/1e8:.2f}亿"
    if a >= 1e4:
        return f"{sign}{a/1e4:.0f}万"
    return f"{sign}{a:.0f}"


def _fmt_num(v) -> str:
    n = _fnum(v)
    return "—" if n is None else f"{int(round(n)):,}"


def _fmt_pct(v) -> str:
    n = _fnum(v)
    return "—" if n is None else f"{n:+.2f}%"


def _fmt_score(v) -> str:
    n = _fnum(v)
    if n is None:
        return "—"
    s = f"{n:.1f}"
    return s[:-2] if s.endswith(".0") else s


def _fmt_rate(v) -> str:
    n = _fnum(v)
    return "—" if n is None else f"{n:.1f}%"


def _ud(v) -> str:
    """up/down/flat css class (red up, green down)."""
    n = _fnum(v)
    return "flat" if n is None else ("up" if n > 0 else ("down" if n < 0 else "flat"))


def _fmt_limit_time(v) -> str:
    """Limit-up times arrive in mixed formats (epoch-seconds strings from some
    providers, ``HH:MM:SS``, or full datetimes).  Normalize to ``HH:MM:SS``."""
    if v in (None, ""):
        return ""
    s = str(v).strip()
    if s.isdigit():
        try:
            return datetime.fromtimestamp(int(s)).strftime("%H:%M:%S")
        except (ValueError, OSError, OverflowError):
            return s
    tail = s.split(" ")[-1]  # drop any date part
    return tail if ":" in tail else s


def _badge(text, cls="b-neutral") -> str:
    return f'<span class="badge {cls}">{text}</span>'


def _fb_badge(fb) -> str:
    if fb is None:
        return ""
    return _badge("回退", "b-warn") if fb else _badge("实时", "b-ok")


def _cmd_bar(meta: dict, breadth: dict, readiness: dict | None = None) -> str:
    regime = meta.get("regime") or "—"
    rcolor = REGIME_COLORS.get(regime, "#8fa3c0")
    readiness = readiness or {}
    gate_blocked = (
        meta.get("regime_is_fallback") is True
        or readiness.get("execution_ready") is False
    )
    position_pct = 0 if gate_blocked else meta.get("position_pct")
    position_title = (
        "总门禁未通过，禁止新增仓位"
        if gate_blocked
        else "市场状态建议仓位"
    )
    return (
        '<div class="cmdbar">'
        '<div class="cmd-left">'
        '<div class="brand"><span class="brand-mark">KPL</span><span class="brand-name">交易作战室</span></div>'
        f'<div class="cmd-date">{meta.get("trade_date","")} '
        f'<span class="pulse" title="数据生成于 {meta.get("generated_at","")}"></span>'
        f'<span class="cmd-gen">{meta.get("generated_at","")}</span></div>'
        '</div>'
        '<div class="cmd-right">'
        f'<div class="regime-chip" style="--rc:{rcolor}"><span class="rc-label">情绪阶段</span>'
        f'<span class="rc-value">{regime}</span></div>'
        f'<div class="cmd-metric" title="{position_title}"><span class="cm-label">可用仓位</span>'
        f'<span class="cm-value">{position_pct if position_pct is not None else "—"}<i>%</i></span></div>'
        f'<div class="cmd-metric"><span class="cm-label">涨停</span>'
        f'<span class="cm-value up">{_fmt_num(breadth.get("limit_up"))}</span></div>'
        f'<div class="cmd-metric"><span class="cm-label">跌停</span>'
        f'<span class="cm-value down">{_fmt_num(breadth.get("limit_down"))}</span></div>'
        f'<div class="cmd-metric"><span class="cm-label">炸板率</span>'
        f'<span class="cm-value">{_fmt_pct(breadth.get("blown_rate")) if breadth.get("blown_rate") is not None else "—"}</span></div>'
        '</div></div>'
    )


def _alerts_strip(alerts: list) -> str:
    if not alerts:
        return ""
    sev_cls = {"P0": "b-bad", "P1": "b-warn", "P2": "b-info", "P3": "b-neutral"}
    items = ""
    for a in alerts[:12]:
        ev = a.get("evidence") or ""
        detail = (f'<details class="alert-ev"><summary>证据</summary>'
                  f'<pre>{str(ev)[:800]}</pre></details>') if ev else ""
        items += (f'<div class="alert-item">'
                  f'{_badge(a.get("severity") or "", sev_cls.get(a.get("severity"), "b-neutral"))}'
                  f'<span class="alert-cat">{a.get("category") or ""}</span>'
                  f'<span class="alert-msg">{a.get("message") or ""}</span>{detail}</div>')
    return f'<div class="sec alert-wrap"><div class="alert-strip">{items}</div></div>'


def _emotion_section(meta: dict, breadth: dict) -> str:
    regime = meta.get("regime") or "—"
    rcolor = REGIME_COLORS.get(regime, "#8fa3c0")
    cycle = ["冰点", "启动", "主升", "高潮", "退潮"]
    cycle_html = "".join(
        f'<span class="cy-step{" cy-on" if c == regime else ""}" '
        f'style="--rc:{REGIME_COLORS.get(c, "#8fa3c0")}">{c}</span>'
        + ('<span class="cy-arrow">→</span>' if i < len(cycle) - 1 else "")
        for i, c in enumerate(cycle))
    stats = [
        ("涨停家数", breadth.get("limit_up"), "up", "count"),
        ("跌停家数", breadth.get("limit_down"), "down", "count"),
        ("上涨", breadth.get("rise"), "up", "count"),
        ("下跌", breadth.get("fall"), "down", "count"),
        ("连板高度", breadth.get("consecutive_height"), "", "count"),
        ("赚钱效应", breadth.get("earning_effect"), "", "score"),
        ("急跌风险", breadth.get("acute_drop"), "warnv", "score"),
    ]
    stat_html = ""
    for label, val, cls, kind in stats:
        if val is None:
            disp = "—"
        elif kind == "score":
            disp = f"{_fnum(val):.0f}" if _fnum(val) is not None else "—"
        else:
            disp = _fmt_num(val)
        c = cls if cls in ("up", "down") else ("warn" if cls == "warnv" else "")
        stat_html += (f'<div class="stat reveal"><span class="stat-label">{label}</span>'
                      f'<span class="stat-value {c}" data-count="{_fnum(val) if _fnum(val) is not None else ""}">'
                      f'{disp}</span></div>')
    return (
        '<section class="sec hero" id="emotion">'
        '<div class="hero-grid">'
        '<div class="hero-left">'
        f'<div class="regime-big reveal" style="--rc:{rcolor}">'
        f'<span class="rb-label">当前情绪阶段</span><span class="rb-value">{regime}</span>'
        f'<span class="rb-score">情绪分 {_fmt_num(meta.get("regime_score"))} · '
        f'仓位 {_fmt_num(meta.get("position_pct"))}%</span></div>'
        f'<div class="cycle reveal">{cycle_html}</div>'
        f'<div class="hero-stats">{stat_html}</div>'
        '</div>'
        '<div class="hero-right">'
        '<div class="panel">'
        '<div class="panel-head"><h2>市场情绪趋势</h2>'
        '<span class="panel-sub">涨停 / 跌停 / 涨跌差 · 情绪阶段色带 · 380 个交易日</span></div>'
        '<div id="chart-emotion" class="chart chart-h"></div>'
        '</div>'
        '<div class="panel">'
        '<div class="panel-head"><h2>赚钱效应</h2>'
        '<span class="panel-sub">成功率 · cgl / yll 分项 · 与情绪趋势同源 · 近 120 日</span></div>'
        '<div id="chart-emotion-money" class="chart chart-money"></div>'
        '</div></div></div></section>'
    )


_STAGE_LABEL = {"intraday_strength": "盘中", "close_decision": "收盘"}


def _stage_badges(stages) -> str:
    out = ""
    for s in stages or []:
        dec = s.get("decision") or ""
        cls = "b-ok" if s.get("actionable") else ("b-bad" if "block" in dec else "b-neutral")
        out += _badge(_STAGE_LABEL.get(s.get("stage"), s.get("stage") or ""), cls)
    return out


def _candidates_section(cand: dict) -> str:
    actionable = cand.get("actionable", [])
    executable = cand.get("executable") or [
        row for row in actionable if row.get("executable")
    ]
    pending = cand.get("pending") or [
        row for row in actionable if not row.get("executable")
    ]
    blocked = cand.get("blocked", [])
    pool = cand.get("pool_size")
    evaluated = cand.get("evaluated", 0)
    close_passed = cand.get("close_passed")
    rows = ""
    for c in executable[:40]:
        rows += (
            f'<tr class="reveal"><td class="mono">{c.get("code","")}</td>'
            f'<td>{c.get("name","")}</td>'
            f'<td>{_stage_badges(c.get("stages")) or _badge(c.get("stage") or "", "b-info")}</td>'
            f'<td class="mono">{_fmt_score(c.get("score"))}</td>'
            f'<td>{_badge(c.get("decision") or "", "b-ok" if c.get("decision") in ("follow","confirm","keep","pool") else "b-neutral")}</td>'
            f'<td class="dim">{c.get("reason") or ""}</td></tr>')
    remaining = max(0, 40 - len(executable))
    for c in pending[:remaining]:
        pending_reason = (
            "待风控批准"
            if c.get("tradable")
            else "不可成交"
        )
        rows += (
            f'<tr class="reveal"><td class="mono">{c.get("code","")}</td>'
            f'<td>{c.get("name","")}</td>'
            f'<td>{_stage_badges(c.get("stages")) or _badge(c.get("stage") or "", "b-info")}</td>'
            f'<td class="mono">{_fmt_score(c.get("score"))}</td>'
            f'<td>{_badge(pending_reason, "b-warn")}</td>'
            f'<td class="dim">{c.get("reason") or pending_reason}</td></tr>')
    for c in blocked[:12]:
        rows += (
            f'<tr class="row-blocked reveal"><td class="mono">{c.get("code","")}</td>'
            f'<td>{c.get("name","")}</td>'
            f'<td>{_stage_badges(c.get("stages")) or _badge(c.get("stage") or "", "b-info")}</td>'
            f'<td class="mono">{_fmt_score(c.get("score"))}</td>'
            f'<td>{_badge("阻断", "b-bad")}</td>'
            f'<td class="dim">{c.get("reason") or c.get("decision") or ""}</td></tr>')
    close_txt = f' · 收盘确认 {close_passed}' if close_passed is not None else ""
    return (
        '<section class="sec" id="candidates">'
        '<div class="sec-head"><h2><span class="h-idx">01</span>候选与执行门禁</h2>'
        f'<span class="sec-sub">候选池 {_fmt_num(pool)} → 评估 {evaluated} → '
        f'策略通过 {len(actionable)} → <b class="up">可执行 {len(executable)}</b>'
        f'{close_txt} · 阻断 {len(blocked)}</span></div>'
        '<div class="cand-grid">'
        '<div class="panel"><div class="panel-head"><h3>候选漏斗</h3></div>'
        '<div id="chart-funnel" class="chart chart-funnel"></div>'
        '<div class="panel-head"><h3>阻断原因分布</h3></div>'
        '<div id="chart-block" class="chart chart-block"></div></div>'
        '<div class="panel"><div class="panel-head"><h3>候选明细</h3>'
        '<span class="panel-sub">可执行在前 · 待风控其次 · 阻断在后</span></div>'
        '<div class="tbl-wrap"><table><thead><tr><th>代码</th><th>名称</th><th>阶段</th>'
        '<th>评分</th><th>决策</th><th>原因/阻断</th></tr></thead>'
        f'<tbody>{rows or "<tr><td colspan=6 class=dim>暂无候选</td></tr>"}</tbody></table></div>'
        '</div></div></section>'
    )


_VERDICT_META = {
    "positive_sample": ("正样本", "b-ok"),
    "negative_sample": ("负样本", "b-bad"),
    "mixed_sample": ("混合样本", "b-warn"),
    "insufficient_sample": ("样本不足", "b-neutral"),
}


def _stage_section(validation: dict) -> str:
    stats = validation.get("stage_statistics") or {}
    if not stats:
        return ""
    cards = ""
    for stage, item in sorted(stats.items()):
        label, cls = _VERDICT_META.get(item.get("verdict"),
                                       (item.get("verdict") or "—", "b-neutral"))
        hr = item.get("hit_rate")
        ar = item.get("avg_forward_return_pct")
        cards += (
            '<div class="panel reveal stage-card">'
            f'<div class="panel-head"><h3>{stage}</h3>{_badge(label, cls)}</div>'
            '<div class="stage-metrics">'
            f'<div><span class="stat-label">胜率(T+1)</span>'
            f'<span class="stat-value">{f"{hr:.1f}%" if hr is not None else "—"}</span></div>'
            f'<div><span class="stat-label">平均前瞻收益</span>'
            f'<span class="stat-value {_ud(ar)}">{_fmt_pct(ar)}</span></div>'
            f'<div><span class="stat-label">收益样本</span>'
            f'<span class="stat-value">{int(item.get("return_sample_count") or 0)}'
            f'<i class="dim">/{int(item.get("sample_count") or 0)}</i></span></div>'
            '</div></div>')
    regime_rows = ""
    for regime, stages in sorted((validation.get("regime_stage_counts") or {}).items()):
        cells = " · ".join(f"{s} {n}" for s, n in sorted(stages.items()))
        regime_rows += f'<tr><td>{regime}</td><td class="mono dim">{cells}</td></tr>'
    regime_tbl = ('<div class="panel"><div class="panel-head"><h3>情绪 × 阶段 信号分布</h3></div>'
                  f'<div class="tbl-wrap"><table><tbody>{regime_rows}</tbody></table></div></div>'
                  if regime_rows else "")
    return (
        '<section class="sec" id="stages">'
        '<div class="sec-head"><h2><span class="h-idx">02</span>阶段信号验证</h2>'
        f'<span class="sec-sub">T+1 可执行口径 · 样本 {int(validation.get("sample_count") or 0)} 条 · '
        '前瞻收益按次日买入计算</span></div>'
        f'<div class="grid-3">{cards}</div>{regime_tbl}'
        '</section>')


def _plan_console_section(pc: dict) -> str:
    rows = pc.get("rows") or []
    risk = pc.get("risk")
    if not rows and not risk:
        return ""
    risk_html = ""
    if risk:
        risk_html = (
            '<div class="panel risk-card reveal"><div class="panel-head"><h3>风险态</h3>'
            f'{_badge(risk.get("state") or "", "b-warn" if risk.get("state") == "defensive" else "b-ok")}</div>'
            '<div class="stage-metrics">'
            f'<div><span class="stat-label">总仓位</span><span class="stat-value">{_fmt_rate(risk.get("total"))}</span></div>'
            f'<div><span class="stat-label">单票上限</span><span class="stat-value">{_fmt_rate(risk.get("single"))}</span></div>'
            f'<div><span class="stat-label">板块上限</span><span class="stat-value">{_fmt_rate(risk.get("sector"))}</span></div>'
            '</div></div>')
    tbl = ""
    for p in rows[:61]:
        thesis = p.get("thesis") or ""
        invalid = p.get("invalidation") or ""
        tbl += (
            '<tr class="reveal">'
            f'<td class="mono">{p.get("code","")}</td><td>{p.get("name","")}</td>'
            f'<td>{_badge(p.get("setup") or "", "b-info")}</td>'
            f'<td class="mono">{_fmt_rate(p.get("maxpos"))}</td>'
            f'<td>{_badge(p.get("status") or "", "b-ok" if p.get("status") in ("active","watch","planned") else "b-neutral")}</td>'
            f'<td class="dim reason">{p.get("entry") or ""}</td>'
            f'<td class="dim reason">{p.get("stop") or ""}</td>'
            f'<td class="dim reason">{thesis}{(" ｜ 失效: " + invalid) if invalid else ""}</td></tr>')
    return (
        '<section class="sec" id="plans">'
        '<div class="sec-head"><h2><span class="h-idx">03</span>操盘计划执行台</h2>'
        f'<span class="sec-sub">计划 {len(rows)} 条 · trade_plan / watchlist / risk_snapshot</span></div>'
        f'{risk_html}'
        '<div class="panel"><div class="tbl-wrap"><table><thead><tr><th>代码</th><th>名称</th>'
        '<th>形态</th><th>仓位上限</th><th>状态</th><th>入场条件</th><th>止损条件</th>'
        f'<th>论点 / 失效条件</th></tr></thead>'
        f'<tbody>{tbl or "<tr><td colspan=8 class=dim>暂无计划</td></tr>"}</tbody></table></div></div>'
        '</section>')


def _blown_section(blown: list) -> str:
    if not blown:
        return ""
    latest = blown[-1]
    return (
        '<section class="sec" id="blown">'
        '<div class="sec-head"><h2><span class="h-idx">06</span>炸板封板体检</h2>'
        f'<span class="sec-sub">炸板率 / 炸板家数 · 近 {len(blown)} 个交易日 · '
        f'最新 {latest.get("d","")} 炸板率 {_fmt_rate(latest.get("rate"))} '
        f'(炸板 {latest.get("blown")} / 破板 {latest.get("broken")})</span></div>'
        '<div class="panel"><div id="chart-blown" class="chart chart-blown"></div></div>'
        '</section>')


def _flow_section(ctx: dict) -> str:
    sflow = ctx.get("sector_flow_today", {})
    kflow = ctx.get("stock_flow_today", {})
    sect_rows = sflow.get("inflow", []) + sflow.get("outflow", [])
    show_lu = any(r.get("lu") is not None for r in sect_rows)
    def srow(r):
        kind = f' {_badge(r["kind"], "b-info")}' if r.get("kind") else ""
        lu_cell = f'<td class="mono">{_fmt_num(r.get("lu"))}</td>' if show_lu else ""
        return (f'<tr class="reveal"><td>{r.get("name","")}{kind}</td>'
                f'<td class="mono {_ud(r.get("main"))}">{_fmt_money(r.get("main"))}</td>'
                f'{lu_cell}</tr>')
    def krow(r):
        return (f'<tr class="reveal"><td class="mono">{r.get("code","")}</td>'
                f'<td>{r.get("name") or ""}</td>'
                f'<td class="mono {_ud(r.get("main"))}">{_fmt_money(r.get("main"))}</td>'
                f'<td class="mono {_ud(r.get("chg"))}">{_fmt_pct(r.get("chg"))}</td>'
                f'<td class="mono">{_fmt_rate(r.get("turn"))}</td></tr>')
    th_lu = "<th>涨停</th>" if show_lu else ""
    sin = "".join(srow(r) for r in sflow.get("inflow", [])[:12])
    sout = "".join(srow(r) for r in sflow.get("outflow", [])[:12])
    kin = "".join(krow(r) for r in kflow.get("inflow", [])[:12])
    kout = "".join(krow(r) for r in kflow.get("outflow", [])[:12])
    kinds = sflow.get("kinds") or {}
    kind_txt = " · ".join(f"{_SECTOR_KIND.get(k, k)} {v}" for k, v in kinds.items())
    sec_sub = f'个股 {kflow.get("count",0)} 只 · 板块 {sflow.get("count",0)} 个'
    if kind_txt:
        sec_sub += f"（{kind_txt}）"
    nb = ctx.get("northbound") or {}
    intra = nb.get("intraday")
    north_html = ""
    if intra:
        north_html = (
            '<div class="panel"><div class="panel-head"><h3>北向资金·沪深股通分钟字段（待核验）</h3>'
            f'<span class="panel-sub">同花顺沪股通/深股通原始分钟序列（{nb.get("intraday_date","")}）· 最新 '
            f'<b class="{_ud(intra.get("latest_total"))}">{intra.get("latest_total")} 亿</b>'
            ' · 不代表可验证的当日净买入；每日历史净买额已停披露</span></div>'
            '<div id="chart-north" class="chart chart-north"></div></div>')
    return (
        '<section class="sec" id="flow">'
        '<div class="sec-head"><h2><span class="h-idx">04</span>资金流向</h2>'
        f'<span class="sec-sub">{sec_sub}</span></div>'
        '<div class="panel"><div class="panel-head"><h3>板块主力净流入排行</h3>'
        '<span class="panel-sub">红 = 净流入 · 绿 = 净流出</span></div>'
        '<div id="chart-sector" class="chart chart-sector"></div></div>'
        '<div class="grid-2">'
        '<div class="panel"><div class="panel-head"><h3>板块净流入 Top</h3></div>'
        '<div class="tbl-wrap"><table><thead><tr><th>板块</th><th>主力净额</th>'
        f'{th_lu}</tr></thead>'
        f'<tbody>{sin or "<tr><td colspan=3 class=dim>暂无</td></tr>"}</tbody></table></div></div>'
        '<div class="panel"><div class="panel-head"><h3>板块净流出 Top</h3></div>'
        '<div class="tbl-wrap"><table><thead><tr><th>板块</th><th>主力净额</th>'
        f'{th_lu}</tr></thead>'
        f'<tbody>{sout or "<tr><td colspan=3 class=dim>暂无</td></tr>"}</tbody></table></div></div>'
        '</div>'
        '<div class="grid-2">'
        '<div class="panel"><div class="panel-head"><h3>个股净流入 Top</h3></div>'
        '<div class="tbl-wrap"><table><thead><tr><th>代码</th><th>名称</th><th>主力净额</th>'
        f'<th>涨跌</th><th>换手</th></tr></thead><tbody>{kin or "<tr><td colspan=5 class=dim>暂无</td></tr>"}</tbody></table></div></div>'
        '<div class="panel"><div class="panel-head"><h3>个股净流出 Top</h3></div>'
        '<div class="tbl-wrap"><table><thead><tr><th>代码</th><th>名称</th><th>主力净额</th>'
        f'<th>涨跌</th><th>换手</th></tr></thead><tbody>{kout or "<tr><td colspan=5 class=dim>暂无</td></tr>"}</tbody></table></div></div>'
        '</div>'
        '<div class="panel"><div class="panel-head"><h3>板块资金流热力图</h3>'
        '<span class="panel-sub">主力净流入 · 近 60 个交易日</span></div>'
        '<div id="chart-heatmap" class="chart chart-heat"></div></div>'
        f'{north_html}'
        '</section>'
    )


def _ladder_section(ladder: list) -> str:
    cols = ""
    for lvl in ladder:
        stocks = "".join(
            f'<div class="lad-stock" title="{s.get("code","")} {s.get("time") or ""}">'
            f'<span class="ls-name">{s.get("name") or s.get("code")}</span>'
            f'<span class="ls-code">{s.get("code","")}</span></div>'
            for s in lvl.get("stocks", [])[:14])
        cols += (f'<div class="lad-col reveal"><div class="lad-level">'
                 f'<span class="ll-n">{lvl.get("level","")}板</span>'
                 f'<span class="ll-c">{lvl.get("count",0)}</span></div>'
                 f'<div class="lad-stocks">{stocks}</div></div>')
    return (
        '<section class="sec" id="ladder">'
        '<div class="sec-head"><h2><span class="h-idx">05</span>连板梯队</h2>'
        f'<span class="sec-sub">按连板高度 · 涨停池口径 {sum(l.get("count",0) for l in ladder)} 只'
        '（含回退源，可能与行情涨停数不同）</span></div>'
        f'<div class="ladder">{cols or "<div class=dim>暂无涨停梯队</div>"}</div>'
        '</section>'
    )


def _promotion_section(promo: dict) -> str:
    series = promo.get("series") or []
    if not series:
        return ""
    latest = series[-1]
    prem = promo.get("premium")
    cards = (
        f'<div class="stat reveal"><span class="stat-label">首板→二板晋级率（{latest.get("d","")}）</span>'
        f'<span class="stat-value">{_fmt_rate(latest.get("l1_rate"))}</span></div>'
        f'<div class="stat reveal"><span class="stat-label">连板存活率（2板+次日晋板）</span>'
        f'<span class="stat-value">{_fmt_rate(latest.get("hi_rate"))}</span></div>'
        f'<div class="stat reveal"><span class="stat-label">昨涨停今均涨幅</span>'
        f'<span class="stat-value {_ud(prem)}">{_fmt_pct(prem)}</span></div>')
    return (
        '<section class="sec" id="promotion">'
        '<div class="sec-head"><h2><span class="h-idx">07</span>连板晋级体检</h2>'
        f'<span class="sec-sub">首板晋级 / 连板存活 / 昨涨停溢价 · 近 {len(series) + 1} 个交易日</span></div>'
        f'<div class="grid-3">{cards}</div>'
        '<div class="panel"><div class="panel-head"><h3>晋级率走势</h3>'
        '<span class="panel-sub">红 = 首板→二板 · 黄 = 连板存活</span></div>'
        '<div id="chart-promotion" class="chart chart-promo"></div></div>'
        '</section>')


def _concepts_section(concepts: list) -> str:
    show_reason = any(c.get("reason") for c in concepts)
    rows = ""
    for i, c in enumerate(concepts, 1):
        reason_cell = (f'<td class="dim reason">{c.get("reason") or ""}</td>'
                       if show_reason else "")
        rows += (f'<tr class="reveal"><td class="mono dim">{i}</td>'
                 f'<td><b>{c.get("name","")}</b></td>'
                 f'<td class="mono amber">{_fmt_score(c.get("score"))}</td>'
                 f'<td class="mono up">{_fmt_num(c.get("lu"))}</td>'
                 f'<td class="mono {_ud(c.get("main"))}">{_fmt_money(c.get("main"))}</td>'
                 f'<td class="mono">{_fmt_num(c.get("members"))}</td>'
                 f'{reason_cell}</tr>')
    reason_th = '<th>爆发原因</th>' if show_reason else ""
    sub = "主线评分 · 涨停家数 · 主力净流入" + (" · 爆发原因" if show_reason else "")
    ncols = 7 if show_reason else 6
    return (
        '<section class="sec" id="concepts">'
        '<div class="sec-head"><h2><span class="h-idx">08</span>主线题材</h2>'
        f'<span class="sec-sub">{sub}</span></div>'
        '<div class="panel"><div class="tbl-wrap"><table><thead><tr><th>#</th><th>概念</th>'
        f'<th>主线分</th><th>涨停</th><th>主力净额</th><th>成分</th>{reason_th}</tr></thead>'
        f'<tbody>{rows or f"<tr><td colspan={ncols} class=dim>暂无概念数据</td></tr>"}</tbody></table></div></div>'
        '</section>'
    )


def _index_section(index_kline: dict, trade_date: str | None = None) -> str:
    cards = ""
    latest_all = None
    for code, info in index_kline.items():
        data = info.get("data", [])
        last = data[-1] if data else None
        chg = last[5] if last and len(last) > 5 else None
        latest = last[0] if last else None
        if latest and (latest_all is None or latest > latest_all):
            latest_all = latest
        stale = bool(trade_date and latest and latest < trade_date)
        stale_tag = (f' <span class="stale" title="指数日线采集停滞">截至 {latest}</span>'
                     if stale else "")
        cards += (f'<div class="panel idx-card reveal"><div class="panel-head">'
                  f'<h3>{info.get("name", code)}{stale_tag}</h3>'
                  f'<span class="mono {_ud(chg)}">{_fmt_pct(chg)}</span></div>'
                  f'<div id="chart-idx-{code}" class="chart chart-idx"></div></div>')
    sub = "日 K · 近 250 个交易日"
    if trade_date and latest_all and latest_all < trade_date:
        sub += f' · <span class="stale">数据停滞：最新 {latest_all}</span>'
    return (
        '<section class="sec" id="index">'
        '<div class="sec-head"><h2><span class="h-idx">09</span>指数走势</h2>'
        f'<span class="sec-sub">{sub}</span></div>'
        f'<div class="idx-grid">{cards or "<div class=dim>暂无指数数据</div>"}</div>'
        '</section>'
    )


def _auction_section(auction: dict, trade_date: str | None = None) -> str:
    tape = auction.get("tape") or []
    anoms = auction.get("anomalies") or []
    if not anoms and not tape:
        return ""
    # A sparse tape (fewer than 3 snapshots per stock) still renders, but with
    # an explicit notice so the user knows the curve is thin by collection,
    # not by market.  Hiding the whole section used to look like missing data.
    sparse = bool(tape) and not any(len(t) >= 3 for t in tape)
    notice = ""
    if sparse:
        notice = ('<div class="notice">竞价快照稀疏：当日每股仅 '
                  f'{auction.get("tape_max_points") or 0} 个快照'
                  '（仅首末时点），曲线仅供参考 — 采集频率不足</div>')
    anomaly_latest = auction.get("anomaly_latest")
    empty_note = (f"当日无异动（异动数据截至 {anomaly_latest}）" if anomaly_latest
                  else "暂无异动")
    anoms = "".join(
        f'<tr class="reveal"><td class="mono">{a.get("code","")}</td>'
        f'<td>{_badge(a.get("type") or "", "b-warn")}</td>'
        f'<td class="mono">{_fmt_money(a.get("value"))}</td></tr>'
        for a in auction.get("anomalies", [])[:12])
    return (
        '<section class="sec" id="auction">'
        '<div class="sec-head"><h2><span class="h-idx">10</span>竞价窗口</h2>'
        '<span class="sec-sub">09:15–09:25 竞价 · 异动监控</span></div>'
        f'{notice}'
        '<div class="grid-2">'
        '<div class="panel"><div class="panel-head"><h3>竞价撮合价曲线</h3>'
        f'<span class="panel-sub">{auction.get("tape_source") or ""} · '
        f'{" / ".join(auction.get("tape_stocks", [])[:6])}</span></div>'
        '<div id="chart-auction" class="chart chart-auction"></div></div>'
        '<div class="panel"><div class="panel-head"><h3>竞价异动</h3></div>'
        '<div class="tbl-wrap"><table><thead><tr><th>代码</th><th>类型</th><th>金额</th></tr></thead>'
        f'<tbody>{anoms or f"<tr><td colspan=3 class=dim>{empty_note}</td></tr>"}</tbody></table></div></div>'
        '</div></section>'
    )


def _auction_confirmation_section(conf: dict) -> str:
    rows = conf.get("rows") or []
    if not rows:
        return ""
    counts = conf.get("counts") or {}
    badges = "".join(
        f'{_badge(f"{k} {v}", "b-ok" if "confirm" in str(k) else "b-neutral")}'
        for k, v in counts.items())
    tbl = ""
    for r in rows:
        conf_tag = str(r.get("confirmation") or "")
        cls = "b-ok" if "confirmed" in conf_tag else ("b-warn" if conf_tag else "b-neutral")
        tbl += (
            '<tr class="reveal">'
            f'<td class="mono">{r.get("stock_code","")}</td><td>{r.get("name","")}</td>'
            f'<td class="mono amber">{_fmt_score(r.get("auction_strength"))}</td>'
            f'<td class="mono">{_fmt_money(r.get("auction_amount"))}</td>'
            f'<td>{_badge(conf_tag, cls)}</td>'
            f'<td class="mono dim">{r.get("tick_rows") or r.get("quote_rows") or 0}</td>'
            f'<td class="dim">{r.get("source_table") or ""}</td></tr>')
    return (
        '<section class="sec" id="confirmation">'
        '<div class="sec-head"><h2><span class="h-idx">11</span>竞价确认榜</h2>'
        f'<span class="sec-sub">逐股竞价强度与确认来源 · {badges}</span></div>'
        '<div class="panel"><div class="tbl-wrap"><table><thead><tr><th>代码</th><th>名称</th>'
        '<th>竞价强度</th><th>竞价金额</th><th>确认</th><th>快照数</th>'
        f'<th>来源</th></tr></thead><tbody>{tbl}</tbody></table></div></div>'
        '</section>')


def _lhb_section(lhb: list, trade_date: str | None = None) -> str:
    if not lhb:
        return ""
    latest = lhb[0].get("date")
    stale_note = (f' · <span class="stale">采集停滞：最新 {latest}</span>'
                  if trade_date and latest and latest < trade_date else "")
    rows = ""
    for r in lhb[:16]:
        rows += (f'<tr class="reveal"><td class="mono dim">{r.get("date","")}</td>'
                 f'<td class="mono">{r.get("code","")}</td><td>{r.get("name","")}</td>'
                 f'<td class="dim reason">{r.get("reason") or ""}</td>'
                 f'<td class="mono {_ud(r.get("net"))}">{_fmt_money(r.get("net"))}</td>'
                 f'<td class="mono">{_fmt_num(r.get("brokers"))}</td>'
                 f'<td class="mono up">{_fmt_money(r.get("youzi"))}</td>'
                 f'<td class="mono amber">{_fmt_money(r.get("agency"))}</td></tr>')
    return (
        '<section class="sec" id="lhb">'
        '<div class="sec-head"><h2><span class="h-idx">12</span>龙虎榜</h2>'
        f'<span class="sec-sub">席位 · 游资 · 机构{stale_note}</span></div>'
        '<div class="panel"><div class="tbl-wrap"><table><thead><tr><th>日期</th><th>代码</th>'
        '<th>名称</th><th>上榜原因</th><th>净买额</th><th>席位数</th><th>游资买入</th>'
        f'<th>机构买入</th></tr></thead><tbody>{rows}</tbody></table></div></div></section>'
    )


def _coverage_section(cov: dict) -> str:
    if not cov:
        return ""
    def block_card(title: str, blk: dict | None) -> str:
        if not blk:
            return ""
        pct = blk.get("coverage")
        w = max(0.0, min(100.0, float(pct or 0)))
        rels = "".join(
            f'<div class="chain"><span class="chain-name">{r.get("relation","")}</span>'
            f'{_badge(r.get("status",""), "b-ok" if r.get("status")=="ready" else "b-warn")}'
            f'<span class="mono dim" style="margin-left:8px">{_fmt_num(r.get("rows"))} 行 / '
            f'{_fmt_num(r.get("codes"))} 码</span></div>'
            for r in blk.get("relations", []))
        return (
            f'<div class="panel reveal"><div class="panel-head"><h3>{title}</h3>'
            f'{_badge("就绪" if blk.get("ready") else "降级", "b-ok" if blk.get("ready") else "b-warn")}</div>'
            f'<div class="cov-head"><span class="mono">{_fmt_num(blk.get("observed"))} / '
            f'{_fmt_num(blk.get("expected"))} 码</span>'
            f'<span class="mono amber">{_fmt_rate(pct)}</span></div>'
            f'<div class="cov-bar"><div class="cov-fill" style="width:{w:.1f}%"></div></div>'
            f'<div class="chains" style="margin-top:10px">{rels}</div></div>')
    recon = cov.get("reconciliation") or {}
    recon_html = ""
    if recon:
        indep = recon.get("independent_reconciliation_ready")
        recon_html = (
            f'<div class="panel reveal"><div class="panel-head"><h3>独立源对账</h3>'
            f'{_badge(recon.get("status") or "—", "b-ok" if recon.get("status")=="pass" else "b-warn")}</div>'
            f'<div class="recon"><div>参考源行数：<span class="mono">{_fmt_num(recon.get("reference_rows"))}</span></div>'
            f'<div>独立源在位：{_badge("是" if recon.get("independent_source_present") else "否", "b-ok" if recon.get("independent_source_present") else "b-bad")}</div>'
            f'<div>对账就绪：{_badge("是" if indep else "否", "b-ok" if indep else "b-warn")}</div></div></div>')
    cards = block_card("个股资金流覆盖", cov.get("stock_flow")) + block_card("板块资金流覆盖", cov.get("sector_flow"))
    return (
        '<section class="sec" id="coverage">'
        '<div class="sec-head"><h2><span class="h-idx">13</span>资金流覆盖率</h2>'
        f'<span class="sec-sub">关系级行数/代码数/新鲜度 · 整体'
        f'{"就绪" if cov.get("ready") else "降级"}</span></div>'
        f'<div class="grid-3">{cards}{recon_html}</div>'
        '</section>')


def _matrix_section(mx: dict) -> str:
    sessions = mx.get("sessions") or []
    if not sessions:
        return ""
    check_names = list(sessions[0].get("checks", {}).keys())
    head = "<th>日期</th>" + "".join(
        f'<th title="{c}">{c.replace("_", " ")}</th>' for c in check_names)
    body = ""
    for s in sessions:
        checks = s.get("checks") or {}
        cells = "".join(
            f'<td class="mx {"mx-ok" if checks.get(c) else "mx-bad"}">'
            f'{"✓" if checks.get(c) else "✗"}</td>'
            for c in check_names)
        body += (f'<tr><td class="mono">{s.get("d","")}</td>{cells}</tr>')
    return (
        '<section class="sec" id="matrix">'
        '<div class="sec-head"><h2><span class="h-idx">14</span>流水线体检矩阵</h2>'
        f'<span class="sec-sub">近 {len(sessions)} 个交易日 × {len(check_names)} 项检查 · '
        f'连续通过 {mx.get("consecutive") or 0} 天 · '
        f'P1 就绪：{"是" if mx.get("ready_for_p1") else "否"}</span></div>'
        '<div class="panel"><div class="tbl-wrap"><table class="mx-tbl">'
        f'<thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div></div>'
        '</section>')


def _health_section(health: dict, trade_date: str | None = None) -> str:
    if not health:
        return ""
    chains = "".join(
        f'<div class="chain reveal"><span class="chain-name">{c.get("chain","")}</span>'
        f'{_badge(c.get("status",""), "b-ok" if c.get("status")=="available" else ("b-warn" if c.get("status")=="fallback" else "b-bad"))}</div>'
        for c in health.get("chains", []))
    def fresh_row(f):
        latest = f.get("latest")
        stale = bool(trade_date and latest and str(latest) < trade_date)
        cell = f'<span class="stale">{latest}</span>' if stale else (latest or "—")
        return f'<tr><td>{f.get("label","")}</td><td class="mono">{cell}</td></tr>'
    fresh = "".join(fresh_row(f) for f in health.get("freshness", []))
    recon = health.get("reconciliation", {})
    indep = health.get("independent_source_codes") or 0
    recon_status = recon.get("status") or "not_run"
    recon_label = {"pass": "通过", "warning": "警告", "not_run": "未运行",
                   "error": "对账异常"}.get(recon_status, recon_status)
    recon_cls = "b-ok" if recon_status == "pass" else ("b-bad" if recon_status == "error" else "b-warn")
    ref_rows = recon.get("reference_rows")
    ref_disp = "—" if ref_rows is None else _fmt_num(ref_rows)
    return (
        '<section class="sec" id="health">'
        '<div class="sec-head"><h2><span class="h-idx">15</span>数据健康</h2>'
        '<span class="sec-sub">数据链路 · 新鲜度 · 对账</span></div>'
        '<div class="grid-3">'
        f'<div class="panel"><div class="panel-head"><h3>数据链路</h3></div><div class="chains">{chains}</div></div>'
        '<div class="panel"><div class="panel-head"><h3>数据新鲜度</h3></div>'
        f'<div class="tbl-wrap"><table><tbody>{fresh}</tbody></table></div></div>'
        '<div class="panel"><div class="panel-head"><h3>对账 / 独立来源</h3></div>'
        f'<div class="recon"><div>对账状态：{_badge(recon_label, recon_cls)}</div>'
        f'<div>参考源行数：<span class="mono">{ref_disp}</span></div>'
        f'<div>独立来源(TuShare)：<span class="mono">{_fmt_num(indep)}</span> 只</div></div></div>'
        '</div>'
        '<div class="foot">交易作战室 · 数据仅供研究参考，不构成投资建议 · '
        f'生成于 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</div>'
        '</section>'
    )


# ---- CSS (plain string; no f-string to avoid brace escaping) ----
_TERMINAL_CSS_V2 = """
/* ---- war-room v2: layered layout + new sections ---- */
.sec{max-width:1840px}
.laynav{position:sticky;top:0;z-index:60;display:flex;gap:10px;justify-content:center;
padding:8px 0;background:rgba(10,15,24,.92);backdrop-filter:blur(8px);
border-bottom:1px solid var(--line)}
.ln-link{color:var(--muted);text-decoration:none;font-size:13px;padding:4px 14px;
border-radius:14px;border:1px solid transparent}
.ln-link:hover{color:var(--text)}
.ln-link.on{color:var(--amber);border-color:var(--amber)}
.pcard{display:flex;gap:28px;align-items:stretch;justify-content:space-between;
background:linear-gradient(135deg,rgba(240,185,11,.06),rgba(76,195,255,.04));
border:1px solid var(--line2);border-radius:12px;padding:20px 26px;margin-bottom:18px;
flex-wrap:wrap}
.pc-phase{font-size:30px;font-weight:900;letter-spacing:.06em;border-bottom:3px solid;
display:inline-block;padding-bottom:4px}
.pc-phase span{display:block;font-size:11px;color:var(--muted);font-weight:400;
letter-spacing:.1em;margin-top:2px}
.pc-strategy{margin-top:12px;font-size:15px;color:var(--text);max-width:760px;line-height:1.7}
.pc-right{display:grid;grid-template-columns:repeat(2,minmax(160px,auto));gap:10px 34px;
align-content:center}
.pc-kv span{display:block;font-size:10px;color:var(--dim);letter-spacing:.08em}
.pc-kv b{font-family:var(--mono);font-size:18px}
.coreband{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin:14px 0 6px}
.band-cell{background:var(--panel);border:1px solid var(--line);border-radius:10px;
text-align:center;padding:12px 6px}
.band-cell span{display:block;font-size:10px;color:var(--muted);margin-bottom:4px;
letter-spacing:.08em}
.band-cell b{font-family:var(--mono);font-size:22px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:16px 0}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px}
.panel h3{font-size:13px;color:var(--cyan);margin-bottom:10px;font-weight:700}
.panel table{width:100%;border-collapse:collapse}
.panel th,.panel td{padding:5px 8px;border-bottom:1px solid var(--line);font-size:12.5px;text-align:left}
.panel th{color:var(--muted);font-weight:600}
.cb-strip,.cbd-strip{display:flex;gap:2px;overflow-x:auto;align-items:flex-end;padding:4px 0}
.cbd-cell{flex:1;min-width:26px;border-radius:4px 4px 0 0;display:flex;align-items:flex-end;
justify-content:center;color:#fff;font-size:9px}
.cbd-lg{margin-top:8px;font-size:11px;color:var(--muted);display:flex;gap:10px;flex-wrap:wrap}
.ls-strip{display:flex;align-items:flex-end;gap:2px;height:100px}
.ls-col{flex:1;background:#101a2a;border-radius:2px 2px 0 0;display:flex;align-items:flex-end}
.ls-col i{width:100%;background:linear-gradient(180deg,#ff5b6a,#2ebd85);border-radius:2px 2px 0 0}
.fs-wrap{display:flex;gap:12px;align-items:flex-end;min-height:130px}
.fsx{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end}
.fsx i{width:72%;background:linear-gradient(180deg,#f0b90b,#ff5b6a);border-radius:4px 4px 0 0}
.tlb-head,.tlb-row{display:grid;grid-template-columns:170px repeat(auto-fit,minmax(30px,1fr));
gap:2px;margin-bottom:2px;font-size:10px}
.tlh-date{color:var(--dim);text-align:center}
.tlb-name{color:var(--text);font-weight:600;overflow:hidden;text-overflow:ellipsis;
white-space:nowrap}
.tlb-cell{min-height:24px;border-radius:3px;text-align:center;line-height:24px;color:#fff}
.jr-list{list-style:none}
.jr-list li{padding:6px 0;border-bottom:1px dashed var(--line2)}
.appendix{max-width:1840px;margin:30px auto;color:var(--muted)}
.appendix summary{cursor:pointer;font-size:13px}
.empty{color:var(--muted);font-size:12.5px}
"""

_TERMINAL_CSS = """
:root{
  --bg:#0a0f18; --bg2:#0d1420; --panel:#101a2a; --panel2:#0d1624;
  --line:#1b2740; --line2:#233150;
  --text:#e7edf6; --muted:#7d8ca6; --dim:#55647f;
  --up:#ff5b6a; --down:#2ebd85; --amber:#f0b90b; --cyan:#4cc3ff;
  --mono:"Bahnschrift","DIN Alternate",ui-monospace,"Cascadia Code",Consolas,"Courier New",monospace;
  --sans:"Segoe UI","Microsoft YaHei","PingFang SC","Helvetica Neue",Arial,sans-serif;
}
*{margin:0;padding:0;box-sizing:border-box}
html{color-scheme:dark}
body{
  background:var(--bg); color:var(--text); font-family:var(--sans); font-size:14px;
  line-height:1.5; min-height:100vh;
  background-image:
    radial-gradient(1100px 480px at 78% -8%, rgba(76,195,255,.07), transparent 60%),
    radial-gradient(900px 420px at 8% 4%, rgba(255,91,106,.05), transparent 55%),
    repeating-linear-gradient(0deg, rgba(125,140,166,.035) 0 1px, transparent 1px 44px),
    repeating-linear-gradient(90deg, rgba(125,140,166,.03) 0 1px, transparent 1px 44px);
  background-attachment:fixed;
}
.mono{font-family:var(--mono); font-variant-numeric:tabular-nums; letter-spacing:.01em}
.up{color:var(--up)} .down{color:var(--down)} .amber{color:var(--amber)}
.dim{color:var(--muted); font-size:12px} .flat{color:var(--muted)}

/* command bar */
.cmdbar{position:sticky; top:0; z-index:50; display:flex; align-items:center; gap:16px;
  justify-content:space-between; padding:12px 26px; background:rgba(10,15,24,.88);
  backdrop-filter:blur(10px); border-bottom:1px solid var(--line)}
.cmd-left{display:flex; align-items:center; gap:22px}
.brand{display:flex; align-items:center; gap:10px}
.brand-mark{font-family:var(--mono); font-weight:800; font-size:15px; letter-spacing:.08em;
  color:var(--bg); background:linear-gradient(135deg,var(--amber),#ff8f5b); padding:3px 8px; border-radius:6px}
.brand-name{font-size:17px; font-weight:800; letter-spacing:.04em}
.cmd-date{font-family:var(--mono); font-size:12px; color:var(--muted); display:flex; align-items:center; gap:8px}
.cmd-gen{color:var(--dim)}
.pulse{width:8px;height:8px;border-radius:50%;background:var(--down);box-shadow:0 0 0 0 rgba(46,189,133,.5);
  animation:pulse 2s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(46,189,133,.45)}70%{box-shadow:0 0 0 8px rgba(46,189,133,0)}100%{box-shadow:0 0 0 0 rgba(46,189,133,0)}}
.cmd-right{display:flex; align-items:center; gap:14px}
.regime-chip{display:flex; align-items:center; gap:8px; padding:5px 12px; border-radius:8px;
  border:1px solid var(--line2); background:linear-gradient(135deg, color-mix(in srgb,var(--rc) 22%, transparent), transparent)}
.rc-label{font-size:11px; color:var(--muted)}
.rc-value{font-size:15px; font-weight:800; color:var(--rc)}
.cmd-metric{text-align:right}
.cm-label{display:block; font-size:10px; color:var(--dim); letter-spacing:.08em}
.cm-value{font-family:var(--mono); font-size:16px; font-weight:700}
.cm-value i{font-style:normal; font-size:11px; color:var(--muted)}

/* sections */
.sec{padding:34px 26px 8px; max-width:1460px; margin:0 auto}
.sec-head{display:flex; align-items:baseline; gap:14px; margin-bottom:16px}
.sec-head h2{font-size:21px; font-weight:800; letter-spacing:.02em; display:flex; align-items:center; gap:10px}
.h-idx{font-family:var(--mono); font-size:12px; font-weight:700; color:var(--cyan);
  border:1px solid var(--line2); padding:2px 7px; border-radius:5px; letter-spacing:.05em}
.sec-sub{font-size:12px; color:var(--muted)}
.sec-sub b{font-weight:700}

/* hero */
.hero-grid{display:grid; grid-template-columns:330px 1fr; gap:20px}
.panel{background:linear-gradient(160deg,var(--panel),var(--panel2)); border:1px solid var(--line);
  border-radius:12px; padding:18px 20px; box-shadow:0 1px 0 rgba(255,255,255,.03) inset, 0 8px 24px rgba(0,0,0,.25)}
.panel-head{display:flex; align-items:baseline; justify-content:space-between; margin-bottom:12px; gap:12px}
.panel-head h2,.panel-head h3{font-size:15px; font-weight:700}
.panel-sub{font-size:11px; color:var(--dim)}
.regime-big{border-left:3px solid var(--rc); padding:6px 0 6px 16px; margin-bottom:18px;
  background:linear-gradient(90deg, color-mix(in srgb,var(--rc) 12%, transparent), transparent 70%); border-radius:0 10px 10px 0}
.rb-label{display:block; font-size:11px; color:var(--muted); letter-spacing:.12em}
.rb-value{display:block; font-size:46px; font-weight:900; line-height:1.1; color:var(--rc); letter-spacing:.02em}
.rb-score{font-family:var(--mono); font-size:12px; color:var(--muted)}
.cycle{display:flex; align-items:center; gap:6px; margin-bottom:20px; flex-wrap:wrap}
.cy-step{font-size:12px; font-weight:700; padding:4px 10px; border-radius:7px; border:1px solid var(--line2);
  color:var(--muted); background:var(--panel2)}
.cy-on{color:var(--bg); background:var(--rc); border-color:var(--rc); box-shadow:0 0 14px color-mix(in srgb,var(--rc) 45%, transparent)}
.cy-arrow{color:var(--dim); font-size:11px}
.hero-stats{display:grid; grid-template-columns:1fr 1fr; gap:10px}
.stat{background:var(--panel2); border:1px solid var(--line); border-radius:10px; padding:12px 14px;
  transition:transform .18s ease, border-color .18s ease}
.stat:hover{transform:translateY(-2px); border-color:var(--line2)}
.stat-label{display:block; font-size:10px; color:var(--dim); letter-spacing:.1em}
.stat-value{font-family:var(--mono); font-size:24px; font-weight:800}
.stat-value.warn{color:var(--amber)}

/* charts */
.chart{width:100%}
.chart-h{height:330px}
.chart-funnel{height:190px}
.chart-block{height:200px}
.chart-sector{height:380px}
.chart-heat{height:340px}
.chart-idx{height:170px}
.chart-auction{height:280px}

/* tables */
.tbl-wrap{max-height:430px; overflow:auto}
table{width:100%; border-collapse:collapse; font-size:13px}
th{position:sticky; top:0; background:var(--panel2); color:var(--muted); font-weight:600;
  text-align:left; padding:8px 10px; border-bottom:1px solid var(--line2); font-size:11px; letter-spacing:.06em; z-index:2}
td{padding:7px 10px; border-bottom:1px solid var(--line); vertical-align:middle}
tbody tr{transition:background .15s ease}
tbody tr:hover{background:rgba(76,195,255,.05)}
.row-blocked{opacity:.42}
.reason{max-width:330px}

.badge{display:inline-block; font-size:11px; font-weight:600; padding:2px 8px; border-radius:6px; border:1px solid transparent}
.b-ok{color:var(--down); background:rgba(46,189,133,.12); border-color:rgba(46,189,133,.3)}
.b-bad{color:var(--up); background:rgba(255,91,106,.12); border-color:rgba(255,91,106,.3)}
.b-warn{color:var(--amber); background:rgba(240,185,11,.12); border-color:rgba(240,185,11,.3)}
.b-info{color:var(--cyan); background:rgba(76,195,255,.12); border-color:rgba(76,195,255,.3)}
.b-neutral{color:var(--muted); background:rgba(125,140,166,.12); border-color:var(--line2)}

/* candidates */
.cand-grid{display:grid; grid-template-columns:360px 1fr; gap:20px; align-items:start}

/* ladder */
.ladder{display:flex; gap:12px; overflow-x:auto; padding-bottom:10px; align-items:flex-start}
.lad-col{min-width:150px; flex:0 0 auto; background:var(--panel2); border:1px solid var(--line);
  border-radius:10px; padding:12px; transition:transform .18s ease, border-color .18s ease}
.lad-col:hover{transform:translateY(-3px); border-color:var(--line2)}
.lad-level{display:flex; justify-content:space-between; align-items:baseline; margin-bottom:10px;
  border-bottom:1px solid var(--line2); padding-bottom:8px}
.ll-n{font-size:16px; font-weight:800; color:var(--amber)}
.ll-c{font-family:var(--mono); font-size:12px; color:var(--muted)}
.lad-stocks{display:flex; flex-direction:column; gap:6px}
.lad-stock{display:flex; justify-content:space-between; gap:8px; font-size:12px; padding:5px 8px;
  border-radius:6px; background:rgba(76,195,255,.05); transition:background .15s ease}
.lad-stock:hover{background:rgba(76,195,255,.12)}
.ls-name{font-weight:600}
.ls-code{font-family:var(--mono); color:var(--dim); font-size:11px}

/* index */
.idx-grid{display:grid; grid-template-columns:repeat(4,1fr); gap:16px}
.idx-card{padding:14px 16px}

/* flow */
.grid-2{display:grid; grid-template-columns:1fr 1fr; gap:20px; margin-top:20px}
.grid-3{display:grid; grid-template-columns:1fr 1fr 1fr; gap:20px}

/* health */
.chains{display:flex; flex-direction:column; gap:8px}
.chain{display:flex; justify-content:space-between; align-items:center; padding:7px 10px;
  background:var(--panel2); border:1px solid var(--line); border-radius:8px}
.chain-name{font-size:12px}
.recon{display:flex; flex-direction:column; gap:8px; font-size:13px}
.foot{margin-top:26px; padding:16px 0 30px; text-align:center; font-size:11px; color:var(--dim); border-top:1px solid var(--line)}
.notice{padding:12px 16px; margin:0 0 14px; border:1px dashed rgba(240,185,11,.4);
  border-radius:10px; color:var(--amber); font-size:12px; background:rgba(240,185,11,.05)}
.stale{color:var(--amber); font-size:10px; font-weight:600; border:1px solid rgba(240,185,11,.35);
  padding:1px 6px; border-radius:5px; letter-spacing:.03em; vertical-align:middle}

/* alerts strip */
.alert-wrap{padding-top:18px; padding-bottom:0}
.alert-strip{display:flex; flex-direction:column; gap:8px}
.alert-item{display:flex; align-items:center; gap:10px; padding:8px 12px; border-radius:8px;
  background:var(--panel2); border:1px solid var(--line); font-size:12px; flex-wrap:wrap}
.alert-cat{font-family:var(--mono); color:var(--muted); font-size:11px; letter-spacing:.05em}
.alert-msg{color:var(--text)}
.alert-ev{margin-left:auto}
.alert-ev summary{cursor:pointer; color:var(--dim); font-size:11px; list-style:none}
.alert-ev pre{white-space:pre-wrap; word-break:break-all; color:var(--muted); font-size:11px;
  margin-top:4px; max-width:680px}

/* stage validation / plan console */
.stage-card .stage-metrics,.risk-card .stage-metrics{display:grid; grid-template-columns:repeat(3,1fr); gap:10px}
.stage-metrics .stat-value{font-size:20px}
.stage-metrics i{font-style:normal; font-size:12px}
.risk-card{margin-bottom:16px}

/* new charts */
.chart-money{height:240px}
.chart-blown{height:280px}
.chart-promo{height:240px}
.chart-north{height:280px}
.hero-right{display:flex; flex-direction:column; gap:20px; min-width:0}

/* coverage bars */
.cov-head{display:flex; justify-content:space-between; align-items:baseline; margin-bottom:8px; font-size:14px}
.cov-bar{height:8px; border-radius:4px; background:var(--panel2); border:1px solid var(--line); overflow:hidden}
.cov-fill{height:100%; background:linear-gradient(90deg, rgba(46,189,133,.5), var(--down)); border-radius:4px}

/* pipeline matrix */
.mx-tbl td.mx,.mx-tbl th{text-align:center; font-size:12px}
.mx-ok{color:var(--down); font-weight:700}
.mx-bad{color:var(--up); font-weight:700}

/* scroll reveal */
.reveal{opacity:0; transform:translateY(14px); transition:opacity .55s ease, transform .55s ease}
.reveal.in{opacity:1; transform:none}

@media (max-width:1100px){
  .hero-grid{grid-template-columns:1fr}
  .cand-grid{grid-template-columns:1fr}
  .idx-grid{grid-template-columns:1fr 1fr}
  .grid-3{grid-template-columns:1fr}
}
@media (max-width:720px){
  .cmdbar{flex-wrap:wrap}
  .idx-grid{grid-template-columns:1fr}
  .grid-2{grid-template-columns:1fr}
}
"""

# ---- JS (plain string; DATA injected via __DATA__ placeholder) ----
_TERMINAL_JS = """
const UP='#ff5b6a', DOWN='#2ebd85', AMBER='#f0b90b', CYAN='#4cc3ff', MUTED='#7d8ca6', GRID='#1b2740';
const fmtMoney=v=>{if(v==null)return'—';const a=Math.abs(v),s=v<0?'-':'';
  if(a>=1e8)return s+(a/1e8).toFixed(2)+'亿'; if(a>=1e4)return s+Math.round(a/1e4)+'万'; return s+Math.round(a);};
const hasEcharts=typeof echarts!=='undefined';
const baseTooltip={backgroundColor:'#0e1520',borderColor:'#22304a',textStyle:{color:'#e7edf6',fontSize:12}};

function initEmotion(){
  const el=document.getElementById('chart-emotion'); if(!el||!hasEcharts)return;
  const h=DATA.emotion_history||[]; if(!h.length)return;
  const dates=h.map(x=>x.d);
  // regime ribbon segments
  const rc={'高潮':'rgba(255,91,106,.14)','主升':'rgba(240,82,74,.11)','启动':'rgba(240,185,11,.10)',
    '震荡':'rgba(143,163,192,.06)','退潮':'rgba(46,189,133,.10)','冰点':'rgba(76,195,255,.10)'};
  const segs=[]; let cur=null;
  h.forEach((x,i)=>{ if(!cur||cur.regime!==x.regime){ if(cur)segs.push(cur); cur={regime:x.regime,start:i,end:i}; } else cur.end=i; });
  if(cur)segs.push(cur);
  const markArea={silent:true,data:segs.map(s=>[{xAxis:dates[s.start],
      itemStyle:{color:rc[s.regime]||'transparent'},
      label:{show:(s.end-s.start)>=8,position:'insideTop',color:'#9fb2cf',fontSize:10,formatter:s.regime}},
      {xAxis:dates[s.end]}])};
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',
    tooltip:Object.assign({trigger:'axis'},baseTooltip),
    legend:{data:['涨停','跌停','涨跌差'],textStyle:{color:MUTED,fontSize:11},top:0,right:0,itemWidth:12,itemHeight:8},
    grid:{left:52,right:18,top:34,bottom:64},
    xAxis:{type:'category',data:dates,axisLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10},boundaryGap:true},
    yAxis:{type:'value',axisLine:{show:false},splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    dataZoom:[{type:'inside',startValue:Math.max(0,dates.length-120)},
      {type:'slider',height:16,bottom:6,borderColor:GRID,fillerColor:'rgba(76,195,255,.12)',textStyle:{color:'#5b6b85'},handleStyle:{color:CYAN}}],
    series:[
      {name:'涨停',type:'bar',data:h.map(x=>x.lu),itemStyle:{color:UP},barMaxWidth:7,markArea:markArea},
      {name:'跌停',type:'bar',data:h.map(x=>-(x.ld||0)),itemStyle:{color:DOWN},barMaxWidth:7},
      {name:'涨跌差',type:'line',data:h.map(x=>(x.rise||0)-(x.fall||0)),smooth:true,showSymbol:false,
        lineStyle:{color:CYAN,width:1.6},areaStyle:{color:'rgba(76,195,255,.07)'}}
    ]
  });
  addEventListener('resize',()=>c.resize());
}

function initFunnel(){
  const el=document.getElementById('chart-funnel'); if(!el||!hasEcharts)return;
  const cd=DATA.candidates||{};
  const data=[
    {value:cd.pool_size||0,name:'候选池'},
    {value:cd.evaluated||0,name:'评估'},
    {value:(cd.actionable||[]).length,name:'策略通过'},
    {value:(cd.executable||[]).length,name:'可执行'}
  ];
  if(cd.close_passed!=null)data.push({value:cd.close_passed,name:'收盘确认'});
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',tooltip:Object.assign({trigger:'item'},baseTooltip),
    series:[{type:'funnel',left:'8%',right:'8%',top:8,bottom:8,minSize:'28%',maxSize:'100%',
      gap:3,label:{color:'#e7edf6',fontSize:12,formatter:'{b} {c}'},
      itemStyle:{borderColor:'#0a0f18',borderWidth:2},
      data:data.map((d,i)=>Object.assign({},d,{itemStyle:{color:[CYAN,AMBER,UP,'#a78bfa'][i%4]}}))}]
  });
  addEventListener('resize',()=>c.resize());
}

function initBlock(){
  const el=document.getElementById('chart-block'); if(!el||!hasEcharts)return;
  const br=DATA.candidates?.block_reasons||{};
  const entries=Object.entries(br).sort((a,b)=>b[1]-a[1]); if(!entries.length){el.innerHTML='<div style="color:#55647f;font-size:12px;padding:12px">无阻断</div>';return;}
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',tooltip:Object.assign({trigger:'axis'},baseTooltip),
    grid:{left:10,right:40,top:6,bottom:6,containLabel:true},
    xAxis:{type:'value',axisLine:{show:false},splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    yAxis:{type:'category',data:entries.map(e=>e[0]).reverse(),axisLine:{lineStyle:{color:GRID}},axisLabel:{color:'#8fa3c0',fontSize:10,width:150,overflow:'truncate'}},
    series:[{type:'bar',data:entries.map(e=>e[1]).reverse(),barMaxWidth:14,
      itemStyle:{color:AMBER,borderRadius:[0,3,3,0]},label:{show:true,position:'right',color:MUTED,fontSize:10}}]
  });
  addEventListener('resize',()=>c.resize());
}

function initSector(){
  const el=document.getElementById('chart-sector'); if(!el||!hasEcharts)return;
  const sf=DATA.sector_flow_today||{};
  const items=[...(sf.inflow||[]),...(sf.outflow||[])];
  // dedupe by name keep first, then sort by main
  const seen=new Set(); const arr=[];
  items.forEach(r=>{const k=r.name; if(!seen.has(k)){seen.add(k);arr.push(r);}});
  arr.sort((a,b)=>(a.main||0)-(b.main||0));
  const top=arr.length>26?arr.slice(0,13).concat(arr.slice(-13)):arr;
  if(!top.length)return;
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',tooltip:Object.assign({trigger:'axis',
      formatter:p=>{const d=p[0];return d.name+'<br/>主力净额：'+fmtMoney(top[d.dataIndex].main);}},baseTooltip),
    grid:{left:10,right:70,top:6,bottom:6,containLabel:true},
    xAxis:{type:'value',axisLine:{show:false},splitLine:{lineStyle:{color:GRID}},
      axisLabel:{color:'#5b6b85',fontSize:10,formatter:v=>fmtMoney(v)}},
    yAxis:{type:'category',data:top.map(r=>r.name),axisLine:{lineStyle:{color:GRID}},
      axisLabel:{color:'#8fa3c0',fontSize:11,width:110,overflow:'truncate'}},
    series:[{type:'bar',data:top.map(r=>({value:r.main,itemStyle:{color:(r.main||0)>=0?UP:DOWN,borderRadius:(r.main||0)>=0?[0,3,3,0]:[3,0,0,3]}})),
      barMaxWidth:13,label:{show:true,position:'right',color:MUTED,fontSize:10,formatter:p=>fmtMoney(top[p.dataIndex].main)}}]
  });
  addEventListener('resize',()=>c.resize());
}

function initHeatmap(){
  const el=document.getElementById('chart-heatmap'); if(!el||!hasEcharts)return;
  const sh=DATA.sector_flow_history||{};
  if(!(sh.dates||[]).length||!(sh.sectors||[]).length)return;
  const data=[]; let mn=0,mx=0;
  sh.matrix.forEach((row,y)=>row.forEach((v,x)=>{ if(v!=null){data.push([x,y,v]); mn=Math.min(mn,v); mx=Math.max(mx,v);} }));
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',tooltip:Object.assign({position:'top',
      formatter:p=>sh.sectors[p.value[1]]+'<br/>'+sh.dates[p.value[0]]+'：'+fmtMoney(p.value[2])},baseTooltip),
    grid:{left:10,right:10,top:10,bottom:70,containLabel:true},
    xAxis:{type:'category',data:sh.dates,axisLabel:{color:'#5b6b85',fontSize:9},axisLine:{lineStyle:{color:GRID}},splitArea:{show:false}},
    yAxis:{type:'category',data:sh.sectors,axisLabel:{color:'#8fa3c0',fontSize:10,width:100,overflow:'truncate'},axisLine:{lineStyle:{color:GRID}}},
    visualMap:{min:mn,max:mx,calculable:true,orient:'horizontal',left:'center',bottom:8,
      inRange:{color:[DOWN,'#0e1a2a',UP]},textStyle:{color:MUTED,fontSize:10},itemWidth:14,itemHeight:120,
      formatter:v=>fmtMoney(v)},
    series:[{type:'heatmap',data:data,progressive:500,
      itemStyle:{borderColor:'#0a0f18',borderWidth:1},emphasis:{itemStyle:{shadowBlur:8,shadowColor:'rgba(0,0,0,.5)'}}}]
  });
  addEventListener('resize',()=>c.resize());
}

function initIndex(){
  if(!hasEcharts)return;
  Object.entries(DATA.index_kline||{}).forEach(([code,info])=>{
    const el=document.getElementById('chart-idx-'+code); if(!el)return;
    const d=info.data||[]; if(!d.length)return;
    const dates=d.map(r=>r[0]);
    const closes=d.map(r=>r[2]);
    const chg=(d[d.length-1]&&d[d.length-1][5])||0;
    const col=chg>=0?UP:DOWN;
    const c=echarts.init(el);
    c.setOption({
      backgroundColor:'transparent',tooltip:Object.assign({trigger:'axis'},baseTooltip),
      grid:{left:46,right:8,top:10,bottom:24},
      xAxis:{type:'category',data:dates,axisLabel:{show:false},axisLine:{lineStyle:{color:GRID}}},
      yAxis:{type:'value',scale:true,axisLine:{show:false},splitLine:{lineStyle:{color:GRID}},
        axisLabel:{color:'#5b6b85',fontSize:9}},
      series:[{type:'line',data:closes,showSymbol:false,smooth:true,
        lineStyle:{color:col,width:1.5},areaStyle:{color:{type:'linear',x:0,y:0,x2:0,y2:1,
          colorStops:[{offset:0,color:col+'33'},{offset:1,color:col+'00'}]}}}]
    });
    addEventListener('resize',()=>c.resize());
  });
}

function initAuction(){
  const el=document.getElementById('chart-auction'); if(!el||!hasEcharts)return;
  const tape=DATA.auction?.tape||[]; const stocks=DATA.auction?.tape_stocks||[];
  if(!tape.length)return;
  const palette=[UP,CYAN,AMBER,DOWN,'#a78bfa','#fb923c'];
  const c=echarts.init(el);
  const series=tape.map((t,i)=>({name:stocks[i]||('股票'+i),type:'line',
    data:t.map(p=>[p[0],p[1]]),showSymbol:false,smooth:true,lineStyle:{width:1.4,color:palette[i%palette.length]}}));
  c.setOption({
    backgroundColor:'transparent',tooltip:Object.assign({trigger:'axis'},baseTooltip),
    legend:{data:series.map(s=>s.name),textStyle:{color:MUTED,fontSize:10},top:0,type:'scroll'},
    grid:{left:52,right:12,top:30,bottom:28},
    xAxis:{type:'category',axisLabel:{color:'#5b6b85',fontSize:9},axisLine:{lineStyle:{color:GRID}}},
    yAxis:{type:'value',scale:true,axisLine:{show:false},splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:9}},
    series:series
  });
  addEventListener('resize',()=>c.resize());
}

function initEmotionMoney(){
  const el=document.getElementById('chart-emotion-money'); if(!el||!hasEcharts)return;
  const h=(DATA.emotion_history||[]).filter(x=>x.sr!=null||x.cgl!=null).slice(-120);
  if(!h.length)return;
  const dates=h.map(x=>x.d);
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',
    tooltip:Object.assign({trigger:'axis'},baseTooltip),
    legend:{data:['成功率','cgl','yll'],textStyle:{color:MUTED,fontSize:11},top:0,right:0,itemWidth:12,itemHeight:8},
    grid:{left:46,right:18,top:30,bottom:52},
    xAxis:{type:'category',data:dates,axisLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    yAxis:{type:'value',axisLine:{show:false},splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    dataZoom:[{type:'inside'},{type:'slider',height:14,bottom:4,borderColor:GRID,fillerColor:'rgba(76,195,255,.12)',textStyle:{color:'#5b6b85'}}],
    series:[
      {name:'成功率',type:'line',data:h.map(x=>x.sr),showSymbol:false,smooth:true,lineStyle:{color:UP,width:1.6}},
      {name:'cgl',type:'line',data:h.map(x=>x.cgl),showSymbol:false,smooth:true,lineStyle:{color:AMBER,width:1.2}},
      {name:'yll',type:'line',data:h.map(x=>x.yll),showSymbol:false,smooth:true,lineStyle:{color:CYAN,width:1.2}}
    ]
  });
  addEventListener('resize',()=>c.resize());
}

function initBlown(){
  const el=document.getElementById('chart-blown'); if(!el||!hasEcharts)return;
  const b=DATA.blown_history||[]; if(!b.length)return;
  const dates=b.map(x=>x.d);
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',
    tooltip:Object.assign({trigger:'axis'},baseTooltip),
    legend:{data:['炸板率','炸板家数','破板家数'],textStyle:{color:MUTED,fontSize:11},top:0,right:0,itemWidth:12,itemHeight:8},
    grid:{left:46,right:46,top:32,bottom:52},
    xAxis:{type:'category',data:dates,axisLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    yAxis:[{type:'value',name:'%',nameTextStyle:{color:'#5b6b85'},axisLine:{show:false},
           splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
          {type:'value',name:'家',nameTextStyle:{color:'#5b6b85'},axisLine:{show:false},
           splitLine:{show:false},axisLabel:{color:'#5b6b85',fontSize:10}}],
    dataZoom:[{type:'inside'},{type:'slider',height:14,bottom:4,borderColor:GRID,fillerColor:'rgba(76,195,255,.12)',textStyle:{color:'#5b6b85'}}],
    series:[
      {name:'炸板率',type:'line',data:b.map(x=>x.rate),showSymbol:false,smooth:true,
        lineStyle:{color:AMBER,width:1.6},areaStyle:{color:'rgba(240,185,11,.08)'},
        markLine:{silent:true,symbol:'none',lineStyle:{color:UP,type:'dashed'},
          label:{color:MUTED,fontSize:10,formatter:'20%'},data:[{yAxis:20}]}},
      {name:'炸板家数',type:'bar',yAxisIndex:1,data:b.map(x=>x.blown),itemStyle:{color:'rgba(255,91,106,.55)'},barMaxWidth:6},
      {name:'破板家数',type:'bar',yAxisIndex:1,data:b.map(x=>x.broken),itemStyle:{color:'rgba(143,163,192,.35)'},barMaxWidth:6}
    ]
  });
  addEventListener('resize',()=>c.resize());
}

function initNorth(){
  const el=document.getElementById('chart-north'); if(!el||!hasEcharts)return;
  const nb=(DATA.northbound||{}).intraday; if(!nb)return;
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',
    tooltip:Object.assign({trigger:'axis',valueFormatter:v=>v==null?'—':(+v).toFixed(2)+'亿'},baseTooltip),
    legend:{data:['合计','沪股通','深股通'],textStyle:{color:MUTED,fontSize:11},top:0,right:0,itemWidth:12,itemHeight:8},
    grid:{left:52,right:18,top:30,bottom:28},
    xAxis:{type:'category',data:nb.time,axisLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    yAxis:{type:'value',name:'亿',nameTextStyle:{color:'#5b6b85'},axisLine:{show:false},
      splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    series:[
      {name:'合计',type:'line',data:nb.total,showSymbol:false,smooth:true,
        lineStyle:{color:AMBER,width:2},areaStyle:{color:'rgba(240,185,11,.08)'}},
      {name:'沪股通',type:'line',data:nb.hgt,showSymbol:false,smooth:true,lineStyle:{color:UP,width:1.2}},
      {name:'深股通',type:'line',data:nb.sgt,showSymbol:false,smooth:true,lineStyle:{color:CYAN,width:1.2}}
    ]
  });
  addEventListener('resize',()=>c.resize());
}

function initPromotion(){
  const el=document.getElementById('chart-promotion'); if(!el||!hasEcharts)return;
  const s=(DATA.promotion||{}).series||[]; if(!s.length)return;
  const dates=s.map(x=>x.d);
  const c=echarts.init(el);
  c.setOption({
    backgroundColor:'transparent',
    tooltip:Object.assign({trigger:'axis'},baseTooltip),
    legend:{data:['首板→二板','连板存活'],textStyle:{color:MUTED,fontSize:11},top:0,right:0,itemWidth:12,itemHeight:8},
    grid:{left:46,right:18,top:30,bottom:28},
    xAxis:{type:'category',data:dates,axisLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    yAxis:{type:'value',name:'%',nameTextStyle:{color:'#5b6b85'},axisLine:{show:false},
      splitLine:{lineStyle:{color:GRID}},axisLabel:{color:'#5b6b85',fontSize:10}},
    series:[
      {name:'首板→二板',type:'line',data:s.map(x=>x.l1_rate),showSymbol:true,symbolSize:5,
        lineStyle:{color:UP,width:1.8},itemStyle:{color:UP}},
      {name:'连板存活',type:'line',data:s.map(x=>x.hi_rate),showSymbol:true,symbolSize:5,
        lineStyle:{color:AMBER,width:1.8},itemStyle:{color:AMBER}}
    ]
  });
  addEventListener('resize',()=>c.resize());
}

// count-up animation
function countUp(el){
  const target=parseFloat(el.dataset.count); if(isNaN(target))return;
  const dur=900, t0=performance.now(), isInt=Number.isInteger(target);
  function step(t){const p=Math.min(1,(t-t0)/dur), e=1-Math.pow(1-p,3);
    const v=target*e; el.textContent=isInt?Math.round(v).toLocaleString():v.toFixed(0);
    if(p<1)requestAnimationFrame(step);}
  requestAnimationFrame(step);
}

// scroll reveal
function initReveal(){
  const els=document.querySelectorAll('.reveal');
  if(!('IntersectionObserver' in window)){els.forEach(e=>e.classList.add('in'));return;}
  const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target);}}),{threshold:.08});
  els.forEach(e=>io.observe(e));
}

document.addEventListener('DOMContentLoaded',()=>{
  initReveal();
  document.querySelectorAll('.stat-value[data-count]').forEach(countUp);
  initEmotion(); initEmotionMoney(); initFunnel(); initBlock(); initSector(); initHeatmap(); initIndex(); initAuction(); initBlown(); initPromotion(); initNorth();
});
"""


# ===========================================================================
# LAYERED WAR-ROOM v2  (L0 decision -> L1 structure -> L2 flow/theme ->
#                      L3 execution -> L4 review/archive)
# ===========================================================================

def _esc(v) -> str:
    import html as _html
    return _html.escape(str(v)) if v is not None else "—"

_LAYER_NAV = [
    ("l0", "决策"),
    ("l1", "市场结构"),
    ("l2", "资金题材"),
    ("l3", "执行参考"),
    ("l4", "复盘沉淀"),
]

_PHASE_COLOR_DARK = {
    "climax": "#ff5b6a", "ferment": "#f0854c", "recovery": "#f0b90b",
    "divergence": "#8a93a6", "retreat": "#4c7fd6", "ice": "#3457d5",
}


def _layer_nav(active: str) -> str:
    links = "".join(
        f"<a href='#{lid}' class='ln-link{' on' if lid == active else ''}'>{label}</a>"
        for lid, label in _LAYER_NAV
    )
    return f"<div class='laynav'>{links}</div>"


def _phase_command_v2(ctx: dict) -> str:
    cyc = ctx.get("cycle_state") or {}
    phase = cyc.get("phase")
    color = _PHASE_COLOR_DARK.get(phase, "#8a93a6")
    cap = None
    try:
        from trade_system.signal_attribution import PHASE_POSITION_CAP_PCT
        cap = PHASE_POSITION_CAP_PCT.get(phase or "")
    except Exception:
        cap = None
    risk_state = (ctx.get("plan_console", {}).get("risk") or {}).get("state") or "—"
    strategy = ctx.get("strategy_line") or "—"
    prem = cyc.get("premium_pct")
    promo = cyc.get("promotion_rate")
    return f"""
<div class='pcard'>
  <div class='pc-left'>
    <div class='pc-phase' style='color:{color};border-color:{color}'>
      {PHASE_CN.get(phase, phase or '—')}<span>相位 · 温度 {_fmt_num(cyc.get('score'))}</span></div>
    <div class='pc-strategy'>{_esc(strategy)}</div>
  </div>
  <div class='pc-right'>
    <div class='pc-kv'><span>建议仓位上限</span><b style='color:{color}'>{cap if cap is not None else '—'}%</b></div>
    <div class='pc-kv'><span>风控状态</span><b>{_esc(risk_state)}</b></div>
    <div class='pc-kv'><span>昨日涨停溢价</span><b>{_fmt_pct(prem)}</b></div>
    <div class='pc-kv'><span>首板晋级率</span><b>{f"{promo:.0%}" if isinstance(promo,(int,float)) else '—'}</b></div>
  </div>
</div>"""


def _core_band(ctx: dict) -> str:
    b = ctx.get("breadth", {}) or {}
    cyc = ctx.get("cycle_state") or {}
    cells = [
        ("涨停", b.get("limit_up_count"), "up"),
        ("跌停", b.get("limit_down_count"), "down"),
        ("最高板", ctx.get("cycle_state", {}).get("max_board"), ""),
        ("炸板率",
         (lambda v: f"{round(v * 100, 1)}%" if isinstance(v, (int, float)) else "—")
         ((ctx.get("blown_history") or [{}])[0].get("blown_limit_up_rate")
          if ctx.get("blown_history") else None), "amber"),
        ("昨日溢价", _fmt_pct(cyc.get("premium_pct")), "up"),
        ("首板晋级", (f"{cyc['promotion_rate']:.0%}" if isinstance(cyc.get("promotion_rate"), (int, float)) else "—"), "cyan"),
    ]
    items = "".join(
        f"<div class='band-cell'><span>{label}</span>"
        f"<b class='{cls}'>{_fmt_num(v) if not isinstance(v, str) else v}</b></div>"
        for label, v, cls in cells)
    return f"<div class='coreband'>{items}</div>"


def _cycle_band_dark(series: list) -> str:
    if not series:
        return "<div class='empty'>暂无相位数据</div>"
    cells = "".join(
        f"<div class='cbd-cell' title='{_esc(d)} {PHASE_CN.get(p, p)} 温度{_esc(s)}"
        f" 涨停{_esc(lu)} 溢价{_esc(prem)}'"
        f" style='background:{_PHASE_COLOR_DARK.get(p, '#8a93a6')};"
        f"height:{28 + min(34.0, float(s or 0) / 2)}px'>"
        f"<span>{str(d)[5:]}</span></div>"
        for d, p, s, lu, prem, _pr in reversed(series))
    legend = " · ".join(
        f"<span style='color:{c}'>{PHASE_CN.get(k, k)}</span>"
        for k, c in _PHASE_COLOR_DARK.items())
    return f"<div class='cbd-strip'>{cells}</div><div class='cbd-lg'>{legend}</div>"


def _premium_matrix_section(prem) -> str:
    as_of, rows = prem
    if not rows:
        return "<div class='empty'>暂无溢价矩阵数据</div>"
    trs = "".join(
        f"<tr><td>{'全部' if b == '_all' else b + '板'}</td><td class='mono'>{n}</td>"
        f"<td class='{('up' if (a or 0) > 0 else 'down')}'>{_fmt_pct(a)}</td>"
        f"<td class='mono'>{_fmt_pct(m)}</td><td class='mono'>{_fmt_rate(w)}</td></tr>"
        for b, n, a, m, w in rows)
    return (f"<div class='dim' style='margin-bottom:8px'>快照日 {as_of}"
            "（昨日涨停股 → 今日表现）</div>"
            "<table><thead><tr><th>板位</th><th>样本</th><th>平均溢价</th>"
            "<th>中位数</th><th>胜率</th></tr></thead>"
            f"<tbody>{trs}</tbody></table>")


def _loss_section_v2(trend: list) -> str:
    if not trend:
        return "<div class='empty'>暂无数据</div>"
    today = trend[0]
    bars = "".join(
        f"<div class='ls-col' title='{_esc(d)} 跌停{_esc(ld)} 炸板率"
        f"{_esc(round(bl, 1) if bl is not None else '—')}%'>"
        f"<i style='height:{min(90, int((ld or 0) * 5) + 4)}px'></i></div>"
        for d, ld, bl in reversed(trend))
    blown_txt = (f"{round(today[2], 1)}%" if today[2] is not None else "—")
    return (f"<div class='ls-head'>当日：跌停 <b class='down'>{today[1] or 0}</b> 家 · "
            f"炸板率 <b class='amber'>{blown_txt}</b></div>"
            f"<div class='ls-strip'>{bars}</div>")


def _first_seal_section_v2(buckets: list) -> str:
    total = sum(n for _, n in buckets) or 1
    items = "".join(
        f"<div class='fsx' title='{_esc(label)} {_esc(n)} 只（{_esc(round(n/total*100))}%）'>"
        f"<i style='height:{max(8, int(n / total * 96))}px'></i>"
        f"<span>{_esc(label)}<br><b class='mono'>{n}</b></span></div>"
        for label, n in buckets)
    return ("<div class='sec-head'><h3>首次涨停时点分布</h3>"
            "<span class='dim' style='font-size:12px'>越早封板越强，尾盘板次日溢价通常最差</span></div>"
            + f"<div class='fs-wrap'>{items}</div>")


def _picks_section_v2(picks: list) -> str:
    if not picks:
        return ("<div class='empty'>今日无候选存档。运行 scripts/run_daily_screen.py 生成。</div>")
    trs = "".join(
        f"<tr><td class='mono'>{rank}</td><td class='mono'>{code}</td><td>{name or '—'}</td>"
        f"<td class='num'><b>{score}</b></td><td>{board or '—'}</td>"
        f"<td class='reason'>{(reason or '—')[:26]}</td>"
        f"<td class='reason dim'>{(bull or '—')[:80]}</td>"
        f"<td class='reason dim'>{(risk or '—')[:60]}</td>"
        f"<td class='reason dim'>{(watch or '—')[:60]}</td></tr>"
        for rank, code, name, score, board, reason, bull, risk, watch in picks)
    return ("<table><thead><tr><th>#</th><th>代码</th><th>名称</th><th>总分</th>"
            "<th>板</th><th>原因</th><th>做多逻辑</th><th>风险</th><th>明日观察</th>"
            "</tr></thead><tbody>" + trs + "</tbody></table>")


def _qlib_screen_section_v2(head: tuple) -> str:
    day, model_id, data = head
    if not day or not data:
        return "<div class='empty'>QLib 尚无预测。运行 predict_qlib_daily 后重试。</div>"
    trs = "".join(
        f"<tr><td class='mono'>{i + 1}</td><td class='mono'>{s}</td>"
        f"<td class='num'>{sc:.4f}</td></tr>"
        for i, (s, sc) in enumerate(data))
    return (f"<div class='dim' style='margin-bottom:8px'>{model_id} · 特征日 {day} · "
            "shadow 状态仅供研究，不进计划</div>"
            "<table><thead><tr><th>#</th><th>代码</th><th>模型分</th></tr></thead>"
            f"<tbody>{trs}</tbody></table>")


def _stats_section_v2(st: dict) -> str:
    cards = "".join(
        f"<div class='metric'><span>{label}</span><strong>{val}</strong></div>"
        for label, val in [
            ("本月执行", st.get("n", 0)),
            ("胜率", f"{st['win']:.0%}" if isinstance(st.get("win"), (int, float)) else "—"),
            ("平均收益", f"{st['avg_ret']:.2f}%" if st.get("avg_ret") is not None else "—"),
            ("盈亏比 PF", f"{st['pf']:.2f}" if st.get("pf") else "—"),
        ])
    return f"<div class='ticker'>{cards}</div>"


def _journal_section_v2(rows: list) -> str:
    if not rows:
        return "<div class='empty'>暂无市场日志（add_market_note.py）</div>"
    lis = "".join(
        f"<li><span class='mono dim'>{d}</span> {note} "
        f"<span class='badge b-neutral'>{tags or ''}</span></li>"
        for d, note, tags in rows)
    return f"<ul class='jr-list'>{lis}</ul>"


def _plan_vs_actual_section_v2(rows: list) -> str:
    if not rows:
        return "<div class='empty'>昨日无交易计划，无法对照。</div>"
    trs = "".join(
        f"<tr><td class='mono'>{code}</td><td>{name or '—'}</td>"
        f"<td class='num'>{gap if gap is not None else '—'}%</td>"
        f"<td class='num {_ud(day)}'>{day}%</td>"
        f"<td class='num'>{touch}%</td><td>{verdict}</td></tr>"
        for code, name, gap, day, touch, verdict in rows)
    return ("<table><thead><tr><th>代码</th><th>名称</th><th>竞价高开%</th>"
            "<th>全天涨幅%</th><th>最高触及%</th><th>结论</th></tr></thead>"
            f"<tbody>{trs}</tbody></table>")


def _theme_timeline_html(ctx: dict) -> str:
    tl = ctx.get("theme_timeline") or {}
    dates = tl.get("dates") or []
    rows = tl.get("rows") or []
    if not dates or not rows:
        return "<div class='empty'>暂无题材成员数据</div>"
    max_zt = max((c for r in rows for c in r["cells"]), default=1)
    head = "".join(f"<div class='tlh-date'>{d[5:]}</div>" for d in dates)
    body_rows = []
    for r in rows:
        cells = "".join(
            f"<div class='tlb-cell' style='background:rgba(255,91,106,"
            f"{0.12 + 0.88 * c / max_zt:.2f})' title='{r['name']} {d}：{c} 只涨停'>{c or ''}</div>"
            for c, d in zip(r["cells"], dates))
        body_rows.append(
            f"<div class='tlb-row'><div class='tlb-name'>{r['name'] or '—'}</div>{cells}</div>")
    warn = "" if tl.get("coverage", 0) >= 374 else \
        f" ⚠️ 当日快照不完整（{tl.get('coverage', 0)}/375）"
    return (
        "<div class='tlb-head'><div class='tlb-name'></div>" + head + "</div>"
        + "".join(body_rows)
        + f"<div class='dim' style='margin-top:8px'>最新快照覆盖 {tl.get('coverage', 0)}/375{warn}"
        "；颜色深浅=当日涨停家数。research-only。</div>")


def _northbound_section(nb: dict) -> str:
    intra = nb.get("intraday")
    if not intra:
        return ("<div class='empty'>北向分钟字段未对齐（数据源已停更），"
                "该面板保留占位。</div>")
    return f"<div class='mono'>{intra}</div>"


def render_terminal_html(ctx: dict, echarts_tag: str | None = None) -> str:
    meta = ctx.get("meta", {})
    breadth = ctx.get("breadth", {})
    body = [
        '<!doctype html><html lang="zh"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>交易作战室 · {meta.get("trade_date","")}</title>',
        echarts_tag or _ECHARTS_CDN,
        '<style>', _TERMINAL_CSS, _TERMINAL_CSS_V2, '</style></head><body>',
        _cmd_bar(meta, breadth, ctx.get("readiness")),
        _layer_nav("l0"),

        # ---------- L0 决策层 ----------
        '<section class="sec" id="l0"><div class="sec-head"><h2>L0 · 决策</h2></div>',
        _phase_command_v2(ctx),
        _core_band(ctx),
        _alerts_strip(ctx.get("alerts", [])),
        '</section>',

        # ---------- L1 市场结构层 ----------
        '<section class="sec" id="l1"><div class="sec-head"><h2>L1 · 市场结构</h2></div>',
        _cycle_band_dark(ctx.get("cycle_series", [])),
        _emotion_section(meta, breadth),
        '<div class="grid2">',
        f'<div class="panel"><h3>溢价分层矩阵</h3>{_premium_matrix_section(ctx.get("premium_matrix", ("", [])))}</div>',
        f'<div class="panel"><h3>亏钱效应</h3>{_loss_section_v2(ctx.get("loss_trend", []))}</div>',
        '</div>',
        _promotion_section(ctx.get("promotion", {})),
        _ladder_section(ctx.get("ladder", [])),
        _blown_section(ctx.get("blown_history", [])),
        '</section>',

        # ---------- L2 资金题材层 ----------
        '<section class="sec" id="l2"><div class="sec-head"><h2>L2 · 资金题材</h2></div>',
        _flow_section(ctx),
        _theme_timeline_html(ctx),
        _concepts_section(ctx.get("concepts", [])),
        _northbound_section(ctx.get("northbound", {})),
        _lhb_section(ctx.get("lhb", []), meta.get("trade_date")),
        '</section>',

        # ---------- L3 执行参考层 ----------
        '<section class="sec" id="l3"><div class="sec-head"><h2>L3 · 执行参考</h2></div>',
        f'<div class="panel"><h3>昨日计划 × 今日实际</h3>'
        f'{_plan_vs_actual_section_v2(ctx.get("plan_vs_actual", []))}</div>',
        _candidates_section(ctx.get("candidates", {})),
        _picks_section_v2(ctx.get("picks_top", [])),
        _auction_confirmation_section(ctx.get("auction_confirmation", {})),
        _auction_section(ctx.get("auction", {}), meta.get("trade_date")),
        _first_seal_section_v2(ctx.get("first_seal", [])),
        _plan_console_section(ctx.get("plan_console", {})),
        '</section>',

        # ---------- L4 复盘沉淀层 ----------
        '<section class="sec" id="l4"><div class="sec-head"><h2>L4 · 复盘沉淀</h2></div>',
        _stats_section_v2(ctx.get("operator_stats", {})),
        _journal_section_v2(ctx.get("journal", [])),
        _qlib_screen_section_v2(ctx.get("qlib_screen", ("", "", []))),
        _index_section(ctx.get("index_kline", {}), meta.get("trade_date")),
        '</section>',

        # ---------- 附录（运维细节折叠）----------
        '<details class="appendix"><summary>附录 · 系统运维细节</summary>',
        _stage_section(ctx.get("stage_validation", {})),
        _coverage_section(ctx.get("flow_coverage", {})),
        _matrix_section(ctx.get("pipeline_matrix", {})),
        _health_section(ctx.get("data_health", {}), meta.get("trade_date")),
        '</details>',

        '<script>', 'const DATA=', json.dumps(ctx, ensure_ascii=False), ';',
        _TERMINAL_JS, '</script></body></html>',
    ]
    return "".join(body)


def _echarts_script_tag(out_path: Path) -> str:
    """Prefer a locally vendored ECharts (offline-capable, matches the project's
    self-contained convention), falling back to the CDN if the vendor file is
    absent or the local script fails to load."""
    vendor = Path(__file__).resolve().parent / "vendor" / "echarts.min.js"
    if vendor.exists():
        try:
            rel = os.path.relpath(vendor, Path(out_path).resolve().parent).replace(os.sep, "/")
        except ValueError:
            rel = None  # different drive on Windows -> a relative path is impossible
        if rel:
            local = f'<script src="{rel}"></script>'
            fallback = ('<script>window.echarts||document.write('
                        "'<script src=\"https://cdn.jsdelivr.net/npm/echarts@5.5.0/dist/echarts.min.js\"><\\/script>')"
                        '</script>')
            return local + fallback
    return _ECHARTS_CDN


def write_terminal(db_path: str | Path, out_path: str | Path = "reports/trading_terminal_latest.html",
                   trade_date: str | None = None) -> Path:
    ctx = build_terminal_context(db_path, trade_date)
    html = render_terminal_html(ctx, _echarts_script_tag(Path(out_path)))
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Generate the trading war-room dashboard.")
    ap.add_argument("--db", default="kpl_data.duckdb")
    ap.add_argument("--date", default=None)
    ap.add_argument("--out", default="reports/trading_terminal_latest.html")
    ap.add_argument("--dump", default=None, help="Also dump the raw context JSON here")
    a = ap.parse_args()
    out = write_terminal(a.db, a.out, a.date)
    print(f"terminal dashboard: {out} ({out.stat().st_size} bytes)")
    if a.dump:
        ctx = build_terminal_context(a.db, a.date)
        Path(a.dump).write_text(json.dumps(ctx, ensure_ascii=False), encoding="utf-8")
        print(f"context: {a.dump}")
