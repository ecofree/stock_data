from __future__ import annotations

import argparse
import math
from pathlib import Path
import statistics
import sys

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import DB_PATH


def _split_codes(value: str) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in str(value or "").replace(";", ",").split(",") if item.strip()))


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return ""
    return f"{float(value):.{digits}f}"


def _stock_metrics(con, start_date: str, end_date: str, codes: list[str]) -> list[dict]:
    where = ["date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)"]
    params: list[object] = [start_date, end_date]
    if codes:
        where.append("stock_code IN (" + ",".join("?" for _ in codes) + ")")
        params.extend(codes)
    rows = con.execute(
        f"""
        SELECT stock_code, CAST(date AS VARCHAR), close, change_pct
        FROM tushare_daily
        WHERE {' AND '.join(where)}
        ORDER BY stock_code, date
        """,
        params,
    ).fetchall()
    grouped: dict[str, list[tuple[str, float, float | None]]] = {}
    for code, date, close, pct in rows:
        if close is None or float(close) <= 0:
            continue
        grouped.setdefault(str(code), []).append((str(date)[:10], float(close), float(pct) if pct is not None else None))

    latest_basic = {}
    basic_rows = con.execute(
        """
        SELECT stock_code, turnover_rate, pe, pb, total_mv, circ_mv
        FROM (
            SELECT *, row_number() OVER (PARTITION BY stock_code ORDER BY date DESC) AS rn
            FROM tushare_daily_basic
            WHERE date <= CAST(? AS DATE)
        ) x WHERE rn = 1
        """,
        [end_date],
    ).fetchall()
    for row in basic_rows:
        latest_basic[str(row[0])] = row[1:]

    latest_adj = {}
    adj_rows = con.execute(
        """
        SELECT stock_code, adj_factor
        FROM (
            SELECT *, row_number() OVER (PARTITION BY stock_code ORDER BY date DESC) AS rn
            FROM tushare_adj_factor
            WHERE date <= CAST(? AS DATE)
        ) x WHERE rn = 1
        """,
        [end_date],
    ).fetchall()
    for row in adj_rows:
        latest_adj[str(row[0])] = row[1]

    result = []
    for code, series in grouped.items():
        closes = [item[1] for item in series]
        returns = [item[2] / 100.0 for item in series if item[2] is not None]
        running_max = closes[0]
        drawdowns = []
        for close in closes:
            running_max = max(running_max, close)
            drawdowns.append(close / running_max - 1.0)
        total_return = closes[-1] / closes[0] - 1.0
        ann_return = (1.0 + total_return) ** (252.0 / max(len(returns), 1)) - 1.0 if total_return > -1 else -1.0
        volatility = statistics.stdev(returns) * math.sqrt(252.0) if len(returns) > 1 else None
        basic = latest_basic.get(code, (None,) * 5)
        result.append(
            {
                "stock_code": code,
                "days": len(series),
                "start_date": series[0][0],
                "end_date": series[-1][0],
                "start_close": closes[0],
                "end_close": closes[-1],
                "return_pct": total_return * 100.0,
                "annualized_pct": ann_return * 100.0,
                "volatility_pct": volatility * 100.0 if volatility is not None else None,
                "max_drawdown_pct": min(drawdowns) * 100.0,
                "win_rate_pct": sum(1 for value in returns if value > 0) * 100.0 / max(len(returns), 1),
                "turnover_rate": basic[0],
                "pe": basic[1],
                "pb": basic[2],
                "total_mv": basic[3],
                "circ_mv": basic[4],
                "adj_factor": latest_adj.get(code),
            }
        )
    return result


def _index_metrics(con, start_date: str, end_date: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT index_code, CAST(date AS VARCHAR), close, change_pct
        FROM tushare_index_daily
        WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        ORDER BY index_code, date
        """,
        [start_date, end_date],
    ).fetchall()
    grouped: dict[str, list[tuple[str, float, float | None]]] = {}
    for code, date, close, pct in rows:
        if close is not None and float(close) > 0:
            grouped.setdefault(str(code), []).append((str(date)[:10], float(close), float(pct) if pct is not None else None))
    result = []
    for code, series in grouped.items():
        result.append(
            {
                "index_code": code,
                "days": len(series),
                "start_date": series[0][0],
                "end_date": series[-1][0],
                "return_pct": (series[-1][1] / series[0][1] - 1.0) * 100.0,
                "latest_close": series[-1][1],
            }
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze historical TuShare daily, valuation, adjustment and index data.")
    parser.add_argument("--db", default=DB_PATH)
    parser.add_argument("--start-date", default="20250101")
    parser.add_argument("--end-date", default="20260710")
    parser.add_argument("--stock-codes", default="")
    parser.add_argument("--out", default="reports/tushare_history_analysis_latest.md")
    args = parser.parse_args()

    start_date = args.start_date.replace("-", "")
    end_date = args.end_date.replace("-", "")
    start_iso = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    end_iso = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
    codes = _split_codes(args.stock_codes)

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        metrics = _stock_metrics(con, start_iso, end_iso, codes)
        indexes = _index_metrics(con, start_iso, end_iso)
        industries = {}
        if metrics:
            basic = con.execute(
                "SELECT stock_code, industry FROM tushare_stock_basic WHERE stock_code IS NOT NULL"
            ).fetchall()
            industry_map = {str(code): (industry or "未分类") for code, industry in basic}
            for item in metrics:
                industries.setdefault(industry_map.get(item["stock_code"], "未分类"), []).append(item)
        counts = {}
        for table in ("tushare_daily", "tushare_daily_basic", "tushare_adj_factor", "tushare_index_daily"):
            counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    finally:
        con.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# TuShare 历史数据分析",
        "",
        f"- 分析区间：`{start_iso}` 至 `{end_iso}`",
        f"- 股票样本：`{len(metrics)}` 只（由项目候选/历史样本构成，非全市场随机样本）",
        "- 说明：收益、波动和回撤是历史统计，不构成投资建议；估值字段沿用 TuShare 原始口径。",
        "",
        "## 数据规模",
        "",
        "| 表 | 行数 |",
        "|---|---:|",
    ]
    lines.extend(f"| {table} | {count:,} |" for table, count in counts.items())
    lines.extend(["", "## 样本总体统计", ""])
    if metrics:
        returns = [item["return_pct"] for item in metrics]
        vol = [item["volatility_pct"] for item in metrics if item["volatility_pct"] is not None]
        dd = [item["max_drawdown_pct"] for item in metrics]
        lines.extend(
            [
                f"- 区间收益中位数：`{_fmt(statistics.median(returns))}%`；上涨样本：`{sum(1 for x in returns if x > 0)}/{len(returns)}`",
                f"- 年化波动率中位数：`{_fmt(statistics.median(vol))}%`；最大回撤中位数：`{_fmt(statistics.median(dd))}%`",
                f"- 最新估值覆盖：PE `{sum(1 for x in metrics if x['pe'] is not None)}` 只（有效 PE>0：`{sum(1 for x in metrics if x['pe'] is not None and x['pe'] > 0)}`），PB `{sum(1 for x in metrics if x['pb'] is not None)}` 只（有效 PB>0：`{sum(1 for x in metrics if x['pb'] is not None and x['pb'] > 0)}`），复权因子 `{sum(1 for x in metrics if x['adj_factor'] is not None)}` 只",
                "",
                "## 股票表现（前 10 / 后 10）",
                "",
                "| 代码 | 天数 | 区间收益% | 年化% | 年化波动% | 最大回撤% | 胜率% | PE | PB |",
                "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        ordered = sorted(metrics, key=lambda item: item["return_pct"], reverse=True)
        for item in ordered[:10] + ordered[-10:]:
            lines.append(
                f"| {item['stock_code']} | {item['days']} | {_fmt(item['return_pct'])} | {_fmt(item['annualized_pct'])} | "
                f"{_fmt(item['volatility_pct'])} | {_fmt(item['max_drawdown_pct'])} | {_fmt(item['win_rate_pct'])} | "
                f"{_fmt(item['pe'])} | {_fmt(item['pb'])} |"
            )
        lines.extend(["", "## 行业分组", "", "| 行业 | 股票数 | 平均收益% | 中位收益% | 上涨数 |", "|---|---:|---:|---:|---:|"])
        industry_rows = []
        for industry, items in industries.items():
            vals = [item["return_pct"] for item in items]
            industry_rows.append((industry, len(items), statistics.mean(vals), statistics.median(vals), sum(1 for x in vals if x > 0)))
        for row in sorted(industry_rows, key=lambda item: item[2], reverse=True)[:20]:
            lines.append(f"| {row[0]} | {row[1]} | {_fmt(row[2])} | {_fmt(row[3])} | {row[4]} |")
    else:
        lines.append("未找到指定区间的股票日线数据。")
    lines.extend(["", "## 指数对照", "", "| 指数 | 天数 | 区间收益% | 最新收盘 |", "|---|---:|---:|---:|"])
    for item in indexes:
        lines.append(f"| {item['index_code']} | {item['days']} | {_fmt(item['return_pct'])} | {_fmt(item['latest_close'])} |")
    lines.extend(["", "## 解读边界", "", "- 样本由项目当前候选与已有历史代码组成，不能代表全市场收益分布。", "- 估值、复权因子和行情必须按同一交易日对齐；缺失字段不补零。", "- 下一步若用于策略回测，应增加行业中性、样本外区间和交易成本分析。", ""])
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"status=ok stocks={len(metrics)} indexes={len(indexes)} out={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
