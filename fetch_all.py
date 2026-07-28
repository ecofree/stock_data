"""Main entry point for KPL full data collection."""
import sys
import os
import time
import argparse
from datetime import datetime

# Add current directory to Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import DB_PATH, TODAY
from base import KPLClient, DuckDBStore, logger
from schema import init_schema

# Import all collectors
from collect_market import collect_all_market
from collect_ladder import collect_all_ladder
from collect_sector import collect_all_sector
from collect_lhb import collect_all_lhb
from collect_daily import collect_all_daily
from collect_dingpan import collect_all_dingpan
from collect_fengk import collect_all_fengk
from collect_misc import collect_all_misc
from collect_news import collect_all_news
from collect_stock import collect_all_stock
from collect_l2 import collect_all_l2
from collect_index import collect_all_index
# Optional collectors (finance, index removed to slim deps)
try:
    from collect_finance import collect_all_finance
    HAS_FINANCE = True
except ImportError:
    HAS_FINANCE = False
from collect_advanced import collect_all_advanced
from collect_advanced_stock import collect_all_advanced_stock


def should_collect_finance(skip_finance: bool, only_market: bool, has_finance: bool) -> bool:
    return (not skip_finance) and (not only_market) and has_finance


def main():
    parser = argparse.ArgumentParser(description="KPL 全量数据采集")
    parser.add_argument("--date", type=str, default=TODAY, help="采集日期 (YYYY-MM-DD)")
    parser.add_argument("--db", default=DB_PATH, help="DuckDB database path")
    parser.add_argument("--skip-stock", action="store_true", help="跳过个股级数据采集")
    parser.add_argument("--skip-l2", action="store_true", help="跳过L2数据采集")
    parser.add_argument("--skip-finance", action="store_true", help="跳过财务数据采集")
    parser.add_argument("--only-market", action="store_true", help="只采集市场级数据")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Return non-zero when critical same-date market or sector-capital data is missing.",
    )
    parser.add_argument(
        "--finance-max-stocks", type=int, default=0,
        help="finance gap-fill batch size; 0 keeps finance collection off in the broad daily run",
    )
    args = parser.parse_args()

    date = args.date
    logger.info(f"=" * 80)
    logger.info(f"KPL 全量数据采集开始 - {date}")
    logger.info(f"=" * 80)

    # Market-only is the high-frequency intraday path.  Bound its total KPL
    # retry budget so a semantic mismatch/503 cannot stall the fallback
    # collector for minutes; the caller will derive market context from the
    # same-date stock-flow snapshot when KPL is unavailable.
    client = KPLClient(
        max_attempts=2 if args.only_market else None,
        total_budget_seconds=45 if args.only_market else None,
    )
    store = DuckDBStore(args.db)
    
    try:
        init_schema(store.conn)
    except Exception as e:
        logger.error(f"Schema initialization failed: {e}")
        store.close()
        return 2

    results = {}
    start_time = time.time()

    # Phase 1: Market-level data (fast, no per-stock iteration)
    logger.info(f"\n{'='*60}")
    logger.info(f"[Phase 1/6] 市场级数据")
    logger.info(f"{'='*60}")
    
    logger.info("  采集市场情绪数据...")
    results.update(collect_all_market(client, store, date))

    # ``--only-market`` is the intraday refresh path.  Do not continue into
    # unrelated collectors while their endpoints back off and hold the DB
    # lock.  Validate only same-date market rows.
    if args.only_market:
        strict_missing = []
        if args.strict:
            for table_name in ("daily_summary", "market_rise_fall"):
                try:
                    count = store.fetchall(
                        f'SELECT count(*) FROM "{table_name}" WHERE CAST(date AS VARCHAR) = ?',
                        [date],
                    )[0][0]
                except Exception:
                    count = 0
                if not count:
                    strict_missing.append(table_name)
            if not client.stats["success"]:
                strict_missing.append("api_success")
        store.close()
        if strict_missing:
            logger.error(f"Strict market-only readiness failed: {', '.join(strict_missing)}")
            return 2
        return 0

    logger.info("  采集连板梯队数据...")
    results.update(collect_all_ladder(client, store, date))
    
    logger.info("  采集每日汇总数据...")
    results.update(collect_all_daily(client, store, date))

    logger.info("  采集真实指数数据...")
    results.update(collect_all_index(client, store, date))
    
    logger.info("  采集盯盘数据...")
    results.update(collect_all_dingpan(client, store, date))
    
    logger.info("  采集复盘数据...")
    results.update(collect_all_fengk(client, store, date))
    
    logger.info("  采集高级数据(市场级)...")
    results.update(collect_all_advanced(client, store, date))

    # Phase 2: Sector data (need to collect all sectors first)
    logger.info(f"\n{'='*60}")
    logger.info(f"[Phase 2/6] 板块数据")
    logger.info(f"{'='*60}")
    
    logger.info("  采集板块排行和成分股...")
    sector_results = collect_all_sector(client, store, date)
    results.update(sector_results)
    
    # Extract sector codes for downstream use
    sector_codes = []
    try:
        sector_codes = [row[0] for row in store.fetchall(
            "SELECT sector_code FROM sector_plates ORDER BY sector_code"
        )]
        logger.info(f"  获取到 {len(sector_codes)} 个板块代码")
    except Exception as e:
        logger.warning(f"  无法获取板块代码列表: {e}")

    # Phase 3: News and theme data
    logger.info(f"\n{'='*60}")
    logger.info(f"[Phase 3/6] 资讯和主题数据")
    logger.info(f"{'='*60}")
    
    logger.info("  采集资讯数据...")
    results.update(collect_all_news(client, store, date, sector_codes))

    # Phase 4: ETF, Xianhuo, Theme, Auction, Kline
    logger.info(f"\n{'='*60}")
    logger.info(f"[Phase 4/6] ETF/现货/主题/竞价数据")
    logger.info(f"{'='*60}")

    # Get active stock codes from sector stocks (populated by sector_ranking now)
    stock_codes = []
    if not args.skip_stock and not args.only_market:
        # Try multiple sources: sector_stocks first, then sector_ranking inline stocks
        for query in [
            "SELECT DISTINCT stock_code FROM sector_stocks WHERE date = ? ORDER BY stock_code",
            "SELECT DISTINCT stock_code FROM sector_stocks WHERE stock_code != '' ORDER BY stock_code",
        ]:
            try:
                stock_codes = [row[0] for row in store.fetchall(query, [date])]
                if stock_codes:
                    break
            except Exception:
                continue
        logger.info(f"  获取到 {len(stock_codes)} 个活跃股票代码")

    # Health check: if stock_codes is empty when it should not be, warn and fallback
    if not stock_codes and not args.skip_stock and not args.only_market:
        logger.warning("  WARNING: No stock codes found! Per-stock phases will be empty.")
        logger.warning("  This usually means sector data collection failed upstream.")

    logger.info("  采集ETF/现货/主题数据...")
    results.update(collect_all_misc(client, store, date, stock_codes))

    # Phase 5: Per-stock data (slow, requires iteration)
    if not args.skip_stock and not args.only_market:
        logger.info(f"\n{'='*60}")
        logger.info(f"[Phase 5/6] 个股深度数据 ({len(stock_codes)} 只股票)")
        logger.info(f"{'='*60}")

        if stock_codes:
            logger.info("  采集个股公司信息/标签/机构持仓...")
            results.update(collect_all_stock(client, store, date, stock_codes[:200]))

            logger.info("  采集高级数据(个股级)...")
            results.update(collect_all_advanced_stock(client, store, date, stock_codes[:100]))
    else:
        logger.info(f"\n[Phase 5/6] 跳过个股深度数据")

    # Phase 6: L2 data (very large, per-stock)
    if not args.skip_l2 and not args.only_market:
        logger.info(f"\n{'='*60}")
        logger.info(f"[Phase 6/6] L2 数据")
        logger.info(f"{'='*60}")

        logger.info("  采集L2数据...")
        results.update(collect_all_l2(client, store, date, stock_codes, sector_codes))
    else:
        logger.info(f"\n[Phase 6/6] 跳过L2数据")

    # Finance data (optional, very large)
    if should_collect_finance(args.skip_finance, args.only_market, HAS_FINANCE) and args.finance_max_stocks > 0:
        logger.info(f"\n{'='*60}")
        logger.info(f"[Bonus] 财务数据 ({min(len(stock_codes), 200)} 只股票)")
        logger.info(f"{'='*60}")

        if stock_codes:
            logger.info("  采集财务报表数据...")
            results.update(collect_all_finance(client, store, date, stock_codes[:args.finance_max_stocks]))
    else:
        if not HAS_FINANCE and not args.skip_finance and not args.only_market:
            logger.warning("\n[Bonus] 跳过财务数据: collect_finance.py 不存在")
        else:
            logger.info(f"\n[Bonus] 跳过财务数据")

    # Summary
    elapsed = time.time() - start_time
    logger.info(f"\n{'='*80}")
    logger.info(f"采集完成 - 耗时 {elapsed:.1f} 秒")
    logger.info(f"{'='*80}")
    
    total_rows = sum(v for v in results.values() if isinstance(v, int))
    logger.info(f"总计插入记录数: {total_rows}")
    logger.info(f"API 请求统计: 成功 {client.stats['success']}, 失败 {client.stats['error']}")
    
    # Show breakdown
    logger.info(f"\n各模块采集详情:")
    for module, count in sorted(results.items()):
        status = "✓" if count > 0 else "×"
        logger.info(f"  {status} {module:40s} {count:>8} 条记录")
    
    logger.info(f"\n数据库文件: {store.db_path}")
    logger.info(f"采集日志已保存到 logs/ 目录")
    logger.info(f"{'='*80}")

    strict_missing = []
    if args.strict:
        for table_name in ("daily_summary", "market_rise_fall", "sector_capital"):
            try:
                count = store.fetchall(
                    f'SELECT count(*) FROM "{table_name}" WHERE CAST(date AS VARCHAR) = ?',
                    [date],
                )[0][0]
            except Exception:
                count = 0
            if not count:
                strict_missing.append(table_name)
        if not client.stats["success"]:
            strict_missing.append("api_success")

    store.close()
    if strict_missing:
        logger.error(f"Strict collection readiness failed: {', '.join(strict_missing)}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
