"""CLI for the limit-up continuation backtest (see backtest_engine docstring)."""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

from trade_system.backtest_engine import (  # noqa: E402
    KLINE_DEDUP_CTE,
    BacktestParams,
    load_kline,
    load_universe,
    simulate,
)
from trade_system.logging_setup import configure  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--hold-days", type=int, default=1)
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--board-min", type=int, default=1)
    parser.add_argument("--board-max", type=int, default=None)
    parser.add_argument(
        "--out-prefix",
        default=str(PROJECT_ROOT / "reports" / "strategy_backtest"),
        help="Writes <prefix>_latest.md and <prefix>_trades_latest.csv",
    )
    args = parser.parse_args()

    configure()
    con = duckdb.connect(args.db, read_only=True)
    try:
        sessions = [
            str(r[0]) for r in con.execute(
                f"SELECT DISTINCT d FROM ({KLINE_DEDUP_CTE}) ORDER BY d"
            ).fetchall()
        ]
        universe = load_universe(con, args.start, args.end)
        kline = load_kline(con)
    finally:
        con.close()

    params = BacktestParams(
        hold_days=args.hold_days,
        slippage_bps=args.slippage_bps,
        max_positions=args.max_positions,
        board_min=args.board_min,
        board_max=args.board_max,
    )
    result = simulate(universe, kline, sessions, params)
    stats = result["stats"]

    out_md = Path(f"{args.out_prefix}_latest.md")
    out_csv = Path(f"{args.out_prefix}_trades_latest.csv")
    out_md.parent.mkdir(parents=True, exist_ok=True)

    lines = [
        "# Limit-up continuation backtest",
        "",
        f"- window: {args.start} .. {args.end}",
        f"- params: hold_days={params.hold_days}, slippage={params.slippage_bps}bps"
        f" x2 sides, slots={params.max_positions}, board>={params.board_min}"
        + (f", board<={params.board_max}" if params.board_max is not None else ""),
        "",
        "| metric | value |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in stats.items()],
        "",
        "Limitations: no partial fills/intraday stops; 10cm boards only;"
        " equal-slot compounding.",
        "",
    ]
    out_md.write_text("\n".join(lines), encoding="utf-8")

    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=[
            "stock_code", "signal_date", "entry_date", "exit_date",
            "entry_price", "exit_price", "ret_pct"])
        writer.writeheader()
        for t in result["trades"]:
            writer.writerow(t.__dict__)

    print(f"trades={stats['n_trades']} win_rate={stats['win_rate']} "
          f"cum_return%={stats['cumulative_return_pct']}")
    print(f"report: {out_md}")
    print(f"trades csv: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
