"""Cross-sectional information-coefficient (IC) analysis for any feature table.

Before training any model, verify that candidate features actually predict
forward returns.  This tool computes the daily cross-sectional Spearman IC of
each requested feature against the N-session forward close-to-close return,
then reports mean IC, IC std, ICIR and coverage.

Works directly on DuckDB tables/views via pandas; no qlib install required.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402

from trade_system.logging_setup import configure  # noqa: E402


KLINE_DEDUP_CTE = """
SELECT CAST(trade_date AS DATE) AS d, stock_code, close FROM (
    SELECT CAST(trade_date AS DATE) AS trade_date, stock_code, close,
           row_number() OVER (
               PARTITION BY trade_date, stock_code
               ORDER BY is_fallback ASC, fetched_at DESC
           ) AS _rn
    FROM v_kline_daily WHERE ktype='D' AND close IS NOT NULL
) WHERE _rn = 1
"""


def load_panel(con, table: str, date_col: str, code_col: str,
               features: list[str], horizon: int,
               start_date: str | None = None) -> pd.DataFrame:
    quoted = ", ".join(f'b."{f}"' for f in features)
    start_clause = "WHERE base.d >= ?" if start_date else ""
    binds = [start_date] if start_date else []
    binds.append(horizon)
    sql = f"""
        WITH base AS (
            SELECT CAST("{date_col}" AS DATE) AS d, "{code_col}" AS stock_code,
                   {quoted}
            FROM "{table}" AS b
            {start_clause}
        ),
        kline_all AS ({KLINE_DEDUP_CTE}),
        sessions AS (SELECT DISTINCT d FROM kline_all),
        next_map AS (
            SELECT a.d AS cur, b.d AS nxt
            FROM (SELECT d, row_number() OVER (ORDER BY d) AS i FROM sessions) a
            JOIN (SELECT d, row_number() OVER (ORDER BY d) AS i FROM sessions) b
              ON b.i = a.i + ?
        ),
        kline AS ({KLINE_DEDUP_CTE})
        SELECT b.d, b.stock_code, {quoted},
               k0.close AS entry_close, k1.close AS exit_close
        FROM base b
        JOIN next_map nm ON nm.cur = b.d
        LEFT JOIN kline k0 ON k0.stock_code = b.stock_code AND k0.d = b.d
        LEFT JOIN kline k1 ON k1.stock_code = b.stock_code AND k1.d = nm.nxt
    """
    panel = con.execute(sql, binds).df()
    panel = panel.dropna(subset=["entry_close", "exit_close"])
    panel["fwd_ret"] = panel["exit_close"] / panel["entry_close"] - 1.0
    return panel


def _spearman(group: pd.DataFrame, feat: str) -> float:
    return group[feat].rank().corr(group["fwd_ret"].rank())


def ic_summary(panel: pd.DataFrame, features: list[str],
               min_names: int = 10) -> pd.DataFrame:
    out = []
    usable = panel.dropna(subset=["fwd_ret"])
    for feat in features:
        daily_ic = (
            usable.dropna(subset=[feat])
            .groupby("d")
            .filter(lambda g: len(g) >= min_names)
            .groupby("d")
            .apply(lambda g: _spearman(g, feat), include_groups=False)
        )
        daily_ic = daily_ic.dropna()
        if isinstance(daily_ic, pd.DataFrame):
            daily_ic = daily_ic.iloc[:, 0]
        if daily_ic.empty:
            out.append({"feature": feat, "days": 0})
            continue
        mean, std = float(daily_ic.mean()), float(daily_ic.std())
        out.append({
            "feature": feat,
            "days": int(daily_ic.count()),
            "ic_mean": round(mean, 4),
            "ic_std": round(std, 4),
            "icir": round(mean / std, 3) if std else 0.0,
            "ic_gt_0_pct": round(float((daily_ic > 0).mean()), 4),
        })
    result = pd.DataFrame(out)
    return result.reindex(result["ic_mean"].abs().sort_values(ascending=False).index) \
        if "ic_mean" in result else result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--table", required=True)
    parser.add_argument("--date-col", default="trade_date")
    parser.add_argument("--code-col", default="stock_code")
    parser.add_argument("--features", required=True,
                        help="Comma-separated numeric feature column names.")
    parser.add_argument("--horizon", type=int, default=5,
                        help="Forward return horizon in sessions.")
    parser.add_argument("--min-names", type=int, default=10,
                        help="Minimum names per day for a valid cross-section.")
    parser.add_argument("--out",
                        default=str(PROJECT_ROOT / "reports" / "feature_ic_latest.md"))
    args = parser.parse_args()

    configure()
    features = [f.strip() for f in args.features.split(",") if f.strip()]
    con = duckdb.connect(args.db, read_only=True)
    try:
        panel = load_panel(con, args.table, args.date_col, args.code_col,
                           features, args.horizon)
    finally:
        con.close()
    if panel.empty:
        print("no rows matched (check table/column names)")
        return 1

    summary = ic_summary(panel, features, min_names=args.min_names)
    lines = [
        f"# Feature IC analysis ({args.table}, horizon={args.horizon})",
        "",
        "Daily cross-sectional Spearman IC vs forward return.",
        "",
        "| feature | days | IC mean | IC std | ICIR | IC>0 % |",
        "|---|---|---|---|---|---|",
    ]
    for _, r in summary.iterrows():
        lines.append(
            f"| {r.get('feature')} | {r.get('days', '—')} "
            f"| {r.get('ic_mean', '—')} | {r.get('ic_std', '—')} "
            f"| {r.get('icir', '—')} | {r.get('ic_gt_0_pct', '—')} |"
        )
    Path(args.out).write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(summary.to_string(index=False))
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
