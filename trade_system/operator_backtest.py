"""Professional operator-stage backtest helpers."""

from __future__ import annotations

from pathlib import Path
from statistics import mean

import duckdb

from trade_system.backtest import run_stage_candidate_backtest
from trade_system.quality import table_exists


MIN_HISTORY_TRADING_DAYS = 250
MIN_RETURN_SAMPLES = 50
MIN_REAL_OUTCOME_SAMPLES = 30


def _empty_result() -> dict:
    return {"sample_count": 0, "rows": [], "summary": {}}


def _summary(rows: list[dict], fee_rate: float, slippage_bps: float) -> dict:
    returns = [row["net_return_pct"] for row in rows]
    real_count = sum(1 for row in rows if row.get("source") == "real_outcome")
    proxy_count = sum(1 for row in rows if row.get("source") == "proxy_signal")
    return {
        "avg_net_return_pct": round(mean(returns), 2) if returns else None,
        "win_rate_pct": round(sum(1 for value in returns if value > 0) * 100.0 / len(returns), 2) if returns else None,
        "fee_rate": fee_rate,
        "slippage_bps": slippage_bps,
        "real_outcome_count": real_count,
        "proxy_signal_count": proxy_count,
    }


def _backtest_sample_context(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Collect the minimum evidence needed before treating a backtest as usable."""
    kline_days = 0
    for relation in ("v_kline_daily", "kline"):
        if not table_exists(con, relation):
            continue
        try:
            kline_days = int(con.execute(f"SELECT count(DISTINCT trade_date) FROM {relation}").fetchone()[0])
        except Exception:
            try:
                kline_days = int(con.execute(f"SELECT count(DISTINCT date) FROM {relation}").fetchone()[0])
            except Exception:
                kline_days = 0
        if kline_days:
            break

    def count_rows(relation: str) -> int:
        return int(con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]) if table_exists(con, relation) else 0

    def count_days(relation: str) -> int:
        if not table_exists(con, relation):
            return 0
        try:
            return int(con.execute(f"SELECT count(DISTINCT trade_date) FROM {relation}").fetchone()[0])
        except Exception:
            return 0

    return {
        "kline_trading_days": kline_days,
        "stage_signal_rows": count_rows("stock_candidate_stage_signal"),
        "actionable_stage_signal_rows": (
            int(con.execute(
                "SELECT count(*) FROM stock_candidate_stage_signal WHERE coalesce(is_actionable, false)=true"
            ).fetchone()[0])
            if table_exists(con, "stock_candidate_stage_signal")
            and "is_actionable" in {row[1] for row in con.execute("PRAGMA table_info('stock_candidate_stage_signal')").fetchall()}
            else 0
        ),
        "trade_plan_rows": count_rows("trade_plan"),
        "trade_plan_days": count_days("trade_plan"),
        "trade_journal_rows": count_rows("trade_journal"),
        "trade_journal_days": count_days("trade_journal"),
        "operator_outcome_rows": count_rows("operator_trade_outcome"),
        "operator_outcome_days": count_days("operator_trade_outcome"),
    }


def _readiness(sample_count: int, return_sample_count: int, real_outcome_count: int,
               context: dict[str, int]) -> dict[str, object]:
    reasons: list[str] = []
    if context["kline_trading_days"] < MIN_HISTORY_TRADING_DAYS:
        reasons.append(
            f"历史日线仅 {context['kline_trading_days']} 个交易日，至少需要 {MIN_HISTORY_TRADING_DAYS} 个"
        )
    if return_sample_count < MIN_RETURN_SAMPLES:
        reasons.append(
            f"有效收益样本仅 {return_sample_count} 个，至少需要 {MIN_RETURN_SAMPLES} 个"
        )
    if real_outcome_count == 0:
        reasons.append("尚无 operator_trade_outcome，当前只能作代理回测")
    elif real_outcome_count < MIN_REAL_OUTCOME_SAMPLES:
        reasons.append(
            f"真实结果仅 {real_outcome_count} 个，至少需要 {MIN_REAL_OUTCOME_SAMPLES} 个"
        )
    if sample_count == 0:
        if context.get("stage_signal_rows", 0) and not context.get("actionable_stage_signal_rows", 0):
            reasons.append("阶段信号存在但没有 is_actionable=true 的可执行样本")
        verdict = "insufficient_sample"
    elif real_outcome_count == 0:
        verdict = "proxy_only"
    elif reasons:
        verdict = "insufficient_sample"
    else:
        verdict = "ready"
    return {
        "ready": not reasons and sample_count > 0,
        "verdict": verdict,
        "reasons": reasons,
        "minimum_history_trading_days": MIN_HISTORY_TRADING_DAYS,
        "minimum_return_samples": MIN_RETURN_SAMPLES,
        "minimum_real_outcome_samples": MIN_REAL_OUTCOME_SAMPLES,
        **context,
    }


def _real_outcome_rows(con: duckdb.DuckDBPyConnection) -> list[dict]:
    if not table_exists(con, "operator_trade_outcome"):
        return []
    if not int(con.execute("SELECT count(*) FROM operator_trade_outcome").fetchone()[0]):
        return []
    if table_exists(con, "stock_candidate_stage_signal"):
        rows = con.execute(
            """
            WITH stage_score AS (
                SELECT
                    CAST(trade_date AS VARCHAR) AS trade_date,
                    stock_code,
                    max(score) AS score,
                    string_agg(DISTINCT stage, ',') AS evidence_stages
                FROM stock_candidate_stage_signal
                GROUP BY CAST(trade_date AS VARCHAR), stock_code
            )
            SELECT
                CAST(o.trade_date AS VARCHAR) AS trade_date,
                o.stock_code,
                o.stock_name,
                o.execution_status,
                o.entry_time,
                o.exit_time,
                o.entry_price,
                o.exit_price,
                o.position_pct,
                o.gross_return_pct,
                o.net_return_pct,
                o.outcome_tag,
                o.mistake_tag,
                o.review_note,
                coalesce(s.score, 0) AS score,
                coalesce(s.evidence_stages, 'operator_outcome') AS evidence_stages
            FROM operator_trade_outcome o
            LEFT JOIN stage_score s
                ON CAST(o.trade_date AS VARCHAR) = s.trade_date AND o.stock_code = s.stock_code
            WHERE lower(coalesce(o.execution_status, '')) = 'executed'
              AND coalesce(o.entry_price, 0) > 0
              AND coalesce(o.exit_price, 0) > 0
              AND o.gross_return_pct IS NOT NULL
              AND o.net_return_pct IS NOT NULL
            ORDER BY o.trade_date, o.stock_code
            """
        ).fetchall()
    else:
        rows = con.execute(
            """
            SELECT
                CAST(trade_date AS VARCHAR) AS trade_date,
                stock_code,
                stock_name,
                execution_status,
                entry_time,
                exit_time,
                entry_price,
                exit_price,
                position_pct,
                gross_return_pct,
                net_return_pct,
                outcome_tag,
                mistake_tag,
                review_note,
                0 AS score,
                'operator_outcome' AS evidence_stages
            FROM operator_trade_outcome
            WHERE lower(coalesce(execution_status, '')) = 'executed'
              AND coalesce(entry_price, 0) > 0
              AND coalesce(exit_price, 0) > 0
              AND gross_return_pct IS NOT NULL
              AND net_return_pct IS NOT NULL
            ORDER BY trade_date, stock_code
            """
        ).fetchall()
    result = []
    for row in rows:
        (
            trade_date,
            stock_code,
            stock_name,
            execution_status,
            entry_time,
            exit_time,
            entry_price,
            exit_price,
            position_pct,
            gross_return_pct,
            net_return_pct,
            outcome_tag,
            mistake_tag,
            review_note,
            score,
            evidence_stages,
        ) = row
        result.append(
            {
                "signal_date": str(trade_date),
                "entry_date": str(trade_date),
                "stage": "operator_outcome",
                "stock_code": stock_code,
                "stock_name": stock_name,
                "score": float(score or 0),
                "signal_close": float(entry_price or 0),
                "t1_close": float(exit_price or 0),
                "gross_return_pct": round(float(gross_return_pct or 0), 2),
                "net_return_pct": round(float(net_return_pct or 0), 2),
                "source": "real_outcome",
                "execution_status": execution_status,
                "position_pct": float(position_pct or 0),
                "outcome_tag": outcome_tag,
                "mistake_tag": mistake_tag,
                "review_note": review_note,
                "evidence_stages": evidence_stages,
                "entry_time": entry_time,
                "exit_time": exit_time,
            }
        )
    return result


def _outcome_exclusion_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Count manual records that must not be treated as realized returns."""
    if not table_exists(con, "operator_trade_outcome"):
        return {"total": 0, "unfilled": 0, "missing_price": 0, "missing_return": 0}
    row = con.execute(
        """
        SELECT
            count(*) AS total,
            sum(CASE WHEN lower(coalesce(execution_status, '')) <> 'executed' THEN 1 ELSE 0 END) AS unfilled,
            sum(CASE WHEN lower(coalesce(execution_status, '')) = 'executed'
                      AND (coalesce(entry_price, 0) <= 0 OR coalesce(exit_price, 0) <= 0)
                     THEN 1 ELSE 0 END) AS missing_price,
            sum(CASE WHEN lower(coalesce(execution_status, '')) = 'executed'
                      AND coalesce(entry_price, 0) > 0 AND coalesce(exit_price, 0) > 0
                      AND (gross_return_pct IS NULL OR net_return_pct IS NULL)
                     THEN 1 ELSE 0 END) AS missing_return
        FROM operator_trade_outcome
        """
    ).fetchone()
    return {
        "total": int(row[0] or 0),
        "unfilled": int(row[1] or 0),
        "missing_price": int(row[2] or 0),
        "missing_return": int(row[3] or 0),
    }


def run_operator_stage_backtest(
    db_path: str | Path,
    fee_rate: float = 0.001,
    slippage_bps: float = 10.0,
    *,
    enforce_t1: bool = False,
) -> dict:
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        context = _backtest_sample_context(con)
        outcome_quality = _outcome_exclusion_counts(con)
        real_rows = _real_outcome_rows(con)
    finally:
        con.close()
    if real_rows:
        return {
            "sample_count": len(real_rows),
            "rows": real_rows,
            "summary": _summary(real_rows, fee_rate, slippage_bps),
            "mode": "real_outcome",
            "excluded_unfilled_count": outcome_quality["unfilled"],
            "excluded_missing_price_count": outcome_quality["missing_price"],
            "excluded_missing_return_count": outcome_quality["missing_return"],
            "readiness": _readiness(len(real_rows), len(real_rows), len(real_rows), context),
        }

    if outcome_quality["total"]:
        readiness = _readiness(0, 0, 0, context)
        readiness["reasons"].append(
            "operator_trade_outcome 存在记录，但没有同时具备 executed、有效进出场价和已计算收益的成交样本"
        )
        return {
            "sample_count": 0,
            "rows": [],
            "summary": _summary([], fee_rate, slippage_bps),
            "mode": "real_outcome_incomplete",
            "excluded_unfilled_count": outcome_quality["unfilled"],
            "excluded_missing_price_count": outcome_quality["missing_price"],
            "excluded_missing_return_count": outcome_quality["missing_return"],
            "readiness": readiness,
        }

    stage_result = run_stage_candidate_backtest(db_path, enforce_t1=enforce_t1)
    rows = []
    cost_pct = fee_rate * 100.0 + slippage_bps / 100.0
    for signal in stage_result.get("rows", []):
        gross = signal.get("forward_return_pct")
        if gross is None:
            continue
        rows.append(
            {
                "signal_date": str(signal["trade_date"]),
                "entry_date": signal.get("entry_date"),
                "stage": signal["stage"],
                "stock_code": signal["stock_code"],
                "stock_name": signal["stock_name"],
                "score": float(signal.get("score") or 0),
                "signal_close": float(signal.get("entry_price") or 0),
                "t1_close": float(signal.get("exit_price") or 0),
                "gross_return_pct": round(float(gross), 2),
                "net_return_pct": round(float(gross) - cost_pct, 2),
                "source": "proxy_signal",
                "execution_status": "proxy",
                "return_method": signal.get("return_method"),
            }
        )

    result = {
        "sample_count": len(rows),
        "rows": rows,
        "summary": _summary(rows, fee_rate, slippage_bps),
        "mode": "proxy_signal",
        "independent_sample_count": stage_result.get("independent_sample_count", 0),
        "excluded_count": stage_result.get("excluded_count", 0),
        "excluded_unfilled_count": 0,
        "excluded_missing_price_count": 0,
        "excluded_missing_return_count": 0,
    }
    result["readiness"] = _readiness(
        len(rows),
        len(rows),
        0,
        context,
    )
    return result


def render_operator_backtest_markdown(result: dict) -> str:
    summary = result.get("summary") or {}
    lines = [
        "# Operator Stage Backtest",
        "",
        f"- Sample count: {result.get('sample_count', 0)}",
        f"- Average net return: {summary.get('avg_net_return_pct')}",
        f"- Win rate: {summary.get('win_rate_pct')}",
        f"- Fee rate: {summary.get('fee_rate')}",
        f"- Slippage bps: {summary.get('slippage_bps')}",
        f"- Real outcome samples: {summary.get('real_outcome_count', 0)}",
        f"- Proxy signal samples: {summary.get('proxy_signal_count', 0)}",
        f"- Independent stock-date samples: {result.get('independent_sample_count', result.get('sample_count', 0))}",
        f"- Excluded non-executable signals: {result.get('excluded_count', 0)}",
        f"- Excluded unfilled outcomes: {result.get('excluded_unfilled_count', 0)}; missing price: {result.get('excluded_missing_price_count', 0)}; missing return: {result.get('excluded_missing_return_count', 0)}",
        f"- Backtest verdict: `{(result.get('readiness') or {}).get('verdict', 'insufficient_sample')}`",
        f"- Historical K-line trading days: `{(result.get('readiness') or {}).get('kline_trading_days', 0)}`",
        f"- Trade plan/journal/outcome rows: `{(result.get('readiness') or {}).get('trade_plan_rows', 0)}`/"
        f"`{(result.get('readiness') or {}).get('trade_journal_rows', 0)}`/"
        f"`{(result.get('readiness') or {}).get('operator_outcome_rows', 0)}`",
        f"- Stage signals/actionable signals: `{(result.get('readiness') or {}).get('stage_signal_rows', 0)}`/"
        f"`{(result.get('readiness') or {}).get('actionable_stage_signal_rows', 0)}`",
        "- Readiness reasons: " + "; ".join((result.get('readiness') or {}).get('reasons', [])),
        "",
        "| Signal Date | Entry Date | Source | Stage | Code | Name | Status | Score | Gross % | Net % | Tag |",
        "|---|---|---|---|---|---|---|---:|---:|---:|---|",
    ]
    for row in result.get("rows", [])[:100]:
        lines.append(
            f"| {row['signal_date']} | {row['entry_date']} | {row.get('source', '')} | {row['stage']} | "
            f"{row['stock_code']} | {row['stock_name']} | {row.get('execution_status', '')} | "
            f"{row['score']:.2f} | {row['gross_return_pct']:.2f} | {row['net_return_pct']:.2f} | "
            f"{row.get('outcome_tag', '')} |"
        )
    lines.append("")
    return "\n".join(lines)
