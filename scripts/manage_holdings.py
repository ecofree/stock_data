"""Manage personal stock holdings: add, remove, close, show P&L."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


from trade_system.logging_setup import configure  # noqa: E402


def cmd_add(con, args):
    con.execute(
        "INSERT INTO holdings (stock_code, stock_name, entry_date, entry_price,"
        " shares, stop_loss_price, target_price, notes)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [args.code, args.name or "", args.entry_date, args.entry_price,
         args.shares, args.stop_loss, args.target, args.notes or ""])
    print(f"added {args.code} entry={args.entry_price} shares={args.shares}")


def cmd_remove(con, args):
    n = con.execute(
        "DELETE FROM holdings WHERE stock_code=? AND status='open'",
        [args.code]).fetchall()
    print(f"removed {len(n)} open position(s) for {args.code}")


def cmd_close(con, args):
    con.execute(
        "UPDATE holdings SET status='closed', exit_date=?, exit_price=?,"
        " updated_at=now() WHERE stock_code=? AND status='open'",
        [args.exit_date or "", args.exit_price, args.code])
    row = con.execute(
        "SELECT entry_price FROM holdings WHERE stock_code=?"
        " ORDER BY entry_date DESC LIMIT 1", [args.code]).fetchone()
    if row and args.exit_price:
        pnl = round((args.exit_price / row[0] - 1) * 100, 2)
        print(f"closed {args.code}: entry={row[0]} exit={args.exit_price} pnl={pnl}%")
    else:
        print(f"closed {args.code}")


def cmd_show(con, _):
    rows = con.execute("""
        SELECT h.stock_code, h.stock_name, CAST(h.entry_date AS VARCHAR),
               h.entry_price, h.shares, h.stop_loss_price, h.target_price,
               k.close AS last_close
        FROM holdings h
        LEFT JOIN v_kline_daily k ON k.stock_code=h.stock_code
          AND k.trade_date=(SELECT max(CAST(trade_date AS DATE))
                            FROM v_kline_daily WHERE ktype='D')
        WHERE h.status='open' ORDER BY h.stock_code""").fetchall()
    if not rows:
        print("no open positions")
        return
    total_cost = total_value = 0.0
    for code, name, edate, ep, sh, sl, tp, lc in rows:
        lc = lc or ep
        cost = (ep or 0) * (sh or 0)
        value = lc * (sh or 0)
        pnl_pct = round((lc / ep - 1) * 100, 2) if ep else None
        total_cost += cost
        total_value += value
        print(f"{code} {name or '—':8s} entry={ep} last={lc} "
              f"pnl={pnl_pct}% stop={sl or '—'} target={tp or '—'} "
              f"value={value:.0f}")
    if total_cost:
        overall = round((total_value / total_cost - 1) * 100, 2)
        print(f"\ntotal cost={total_cost:.0f} value={total_value:.0f} "
              f"P&L={overall}%")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    sub = parser.add_subparsers(dest="cmd")
    p_add = sub.add_parser("add")
    p_add.add_argument("--code", required=True)
    p_add.add_argument("--name", default="")
    p_add.add_argument("--entry-date", required=True)
    p_add.add_argument("--entry-price", type=float, required=True)
    p_add.add_argument("--shares", type=int, required=True)
    p_add.add_argument("--stop-loss", type=float, default=None)
    p_add.add_argument("--target", type=float, default=None)
    p_add.add_argument("--notes", default="")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--code", required=True)
    p_cls = sub.add_parser("close")
    p_cls.add_argument("--code", required=True)
    p_cls.add_argument("--exit-price", type=float, default=None)
    p_cls.add_argument("--exit-date", default="")
    sub.add_parser("show")
    args = parser.parse_args()

    configure()
    from trade_system.db_utils import legacy_connect
    con = legacy_connect(args.db)
    try:
        {"add": lambda: cmd_add(con, args),
         "remove": lambda: cmd_remove(con, args),
         "close": lambda: cmd_close(con, args),
         "show": lambda: cmd_show(con, args)}[args.cmd]()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
