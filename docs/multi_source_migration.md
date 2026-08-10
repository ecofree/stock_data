# 多源数据迁移说明

## 已迁移内容

`trade_system/stock_data_sources.py`、`trade_system/resilient_sources.py`、
`trade_system/eastmoney_finance.py` 与统一数据源层均为项目内生产代码，不再依赖
`D:\accio\2026-07-08-22-11-38`；历史演示用的重复行情脚本已移除。
解析器覆盖行情、估值、财务三表、个股资金流、板块排名/资金流、龙虎榜、融资融券、股东、解禁、分红、公告、互动易、热点、涨停池、北向、分时、指数、ETF、可转债、期权、IPO 与宏观等 53 类计划。

## 数据链路

- 日 K 线：Baostock → pytdx → 腾讯 → 新浪 → Tushare 中继 → 百度 → 东方财富。
- 个股资金流：东方财富 120 日资金流 → 新浪资金流 → Tushare `moneyflow` 末级兜底。
- 板块资金流：东方财富板块列表的主力/超大单/大单/中单/小单净流入字段；不依赖 Tushare。
- 其他类型按 `SOURCE_PLAN` 的异后端顺序降级，缓存优先，过期缓存只标记为 `stale`，不会覆盖新鲜记录。

Tushare 凭证只从 `TUSHARE_FAST_RELAY_TOKEN`、`TUSHARE_TOKEN` 或项目设置读取，不写入代码。没有凭证时，其他数据源仍可工作。

## 运行

```powershell
python scripts/init_schema_tables.py --db kpl_data.duckdb
python scripts/bootstrap_multisource_from_core.py --db kpl_data.duckdb
python scripts/run_staged_multisource.py --stage close --stock-codes 000001,600519
python scripts/run_staged_multisource.py --all-stages --dry-run
python scripts/collect_multisource.py --offline --types stock_flow,sector_flow  # 兼容/离线诊断
```

写入表：

- `multi_source_stock_flow`：个股资金流明细；
- `multi_source_sector_flow`：板块资金流明细；
- `multi_source_kline`、`multi_source_quote`：行情与估值；
- `multi_source_observation`：完整原始 payload、来源、延迟、状态、哈希；
- `multi_source_sync_status`：每次采集的成功/过期/失败统计。

`bootstrap_multisource_from_core.py` 只做一次已有 `kline`、`index_kline`、`sector_capital` 的保留迁移，provider 标为 `existing_core`；它不伪造个股资金流，个股资金流仍必须通过新采集器获取。

默认会把新鲜板块资金流同步到现有 `sector_capital`，把新鲜股票/指数日线同步到 `kline` / `index_kline`。`--no-sync-core` 可关闭。

## 到可用状态的验收顺序

1. 先用 `--types stock_flow,sector_flow,kline` 对一个小股票集跑通，检查报告中的 provider 与 source_date；
2. 再扩展到 20–50 只股票，确认 `multi_source_stock_flow` 每只至少有最近交易日记录，板块表有完整排名；
3. 用 `--all` 分批预热，观察 `multi_source_observation` 中各源的失败率和 `stale` 比例；连续失败源应由健康度冷却，不能反复打满超时；
4. 最后把脚本作为每日数据管线的独立步骤，先采集、再质量检查、最后生成信号，禁止把 `stale` 数据直接当成实时交易触发条件。

## 分阶段调度与断点续跑

不要把 `SOURCE_PLAN` 中的所有接口一次性调用。`scripts/run_staged_multisource.py` 将接口按交易时段分组，默认单线程、单任务串行执行；每个任务在开始时写入 `running` 检查点，获取后立即写入 `multi_source_observation` 及对应的资金流/K 线表，再把任务更新为 `success`、`stale` 或 `failed`。进程中断后，已完成任务会跳过，未完成任务可继续；达到预算后剩余任务标记为 `budget_exhausted`，下次运行会重试。

THS 概念目录及成分股是低频维表：按当前 THS 全量目录（当前约 375 个概念）按自然周执行。周内已有完整检查点时返回 `skipped`，盘中/盘后任务只读取最近快照；只有首次缺失、部分失败或显式 `--force` 才会续传网页。

阶段顺序固定为：

- `premarket`：股票基础信息、指数快照、估值；
- `open`：板块资金流、五档盘口、分时、北向快照；
- `midday`：个股资金流、板块资金流、分时、北向快照；
- `close`：个股/指数日线、收盘个股资金流、板块资金流；
- `after_close`：财务数据、利润表、融资融券、龙虎榜、北向历史。

常用命令：

```powershell
python scripts/run_staged_multisource.py --stage close --stock-codes 000001,600519 --max-stocks 20
python scripts/run_staged_multisource.py --stage midday --stock-codes 000001 --budget-seconds 90
python scripts/run_staged_multisource.py --all-stages --dry-run --report reports/staged_plan.md
```

实时快照类任务在历史日期会被明确跳过，不会把今天的数据错误标记成历史数据；K 线和北向历史默认只请求 `--start/--end` 范围，避免每次重复拉取全历史。每日集成流程的 `collect_multisource_capital_flow` 已切换到 `close` 阶段调度器。

项目原有的 KPL 专用资金流脚本 `collect_capital_flow_focus.py` 仍作为独立的有界采集步骤运行（它还覆盖 L2/大单等 KPL 专有表），其 DuckDB 写入发生在每个采集器内部；它与新调度器是前后串行关系，不会并发启动。新迁移层负责把 Eastmoney、Sina、Baostock、pytdx、Tencent、Tushare relay 等通用来源的原始响应和规范化个股/板块资金流保存到 `multi_source_*` 表。
## 2026 Historical Backfill

Use the resumable, per-trading-day collectors below. Each dataset commits immediately to DuckDB and successful checkpoints are skipped on the next run:

```powershell
python scripts/backfill_2026_tushare.py --db kpl_data.duckdb `
  --start-date 20260101 --end-date 20260713 `
  --datasets stock_basic,daily,daily_basic,adj_factor,moneyflow,industry_flow `
  --max-days 5 --budget-seconds 240
```

`daily` and `daily_basic` provide per-stock daily bars and valuation/turnover fields. Tushare stock-level `moneyflow` amounts are converted from 10,000 yuan to yuan and normalized into `multi_source_stock_flow`; small/mid/large/extra-large buckets and net flow are a main-money proxy. Tushare DC industry `moneyflow_ind_dc` amounts are already yuan and are not multiplied. Exact institution-seat flow is not a public every-stock daily field; use 龙虎榜/机构席位 data only where available.

THS is now the default concept source for the operator-facing concept/member view:

```powershell
python scripts/backfill_2026_ths_concepts.py --db kpl_data.duckdb `
  --start-date 20260101 --end-date 20260714 --period day
```

It writes `ths_concept_daily` and `ths_concept_stock_history` from 同花顺的完整概念目录与板块成分页（本次 361 个概念），而不是只取热榜前 100 只股票的标签。默认 `--max-member-pages=0`，会读取每个详情页的 `1/N` 并抓完全部分页；正数仅用于有界测试，不能视为完整映射。THS 页面目前没有历史日期参数，所以只保存真实抓取日快照；所有行携带 `date_verified=false`，报告会列出 2026 历史缺口。这些数据适合当前主题发现，不可直接用于历史回测。原 `kpl_concept_*` 表和采集器保留为兼容回退。若需要严格按交易日的历史成分，应使用支持 `trade_date` 的 TuShare `dc_member` 数据集。行业资金流存储在 `tushare_moneyflow_industry` 和 `multi_source_sector_flow`。
采集器会把同花顺反爬跳转到 `upass` 或缺少分页成员表的响应记为未抓完，不会把前几页误报为成功；应在接口恢复后无 `--force` 续跑 partial checkpoint。

Downstream queries should use `v_default_concept_daily` and `v_default_concept_stock_history`. These views select THS for dates where a THS snapshot exists and fall back to KPL only for dates with no THS snapshot; consumers must still filter `date_verified` before historical analysis.

## Provider capability audit (2026-07-14)

- AKShare remains useful for public-market fallbacks, but current releases no longer expose the old THS concept/member helpers. The historical AKShare implementation was an HTML wrapper around the same THS pages, so it inherits the page-6+ anti-bot failure.
- AKShare is not an active project source today. If it is introduced later, it must run a daily refresh and freshness/coverage validation; a failed or stale refresh must disable the source for that date rather than serve yesterday's cache as current data.
- The local runtime for optional AKShare work is `D:\anaconda\python.exe`; refresh it with `D:\anaconda\python.exe -m pip install akshare --upgrade -i https://pypi.org/simple` before a same-day collection run.
- BaoStock is retained for OHLC, stock-basic and industry classification fallback. It does not provide the required 360+ THS concept-to-constituent mapping.
- The default THS membership order is now `tushare ths_index/ths_member` (exact mapping) followed by guarded THS HTML only for concepts missing from the TuShare index. Every request is checkpointed and persisted before the next board.
- On the 2026-07-14 snapshot, 361 concepts were catalogued, 357 mapped exactly to TuShare (`65,468` members), and four unmapped concepts were stored as explicit partial HTML snapshots (`200` members). No empty concept is treated as complete.
- Daily TuShare data is complete through the latest published trading day (`2026-07-13`); the `2026-07-14` intraday probe returned no daily rows, so it is not backdated or fabricated. Stock-level moneyflow is `5,197/5,197` on 2026-07-13; sector moneyflow is `1,027` valid rows on that date. A pre-open sector response containing only names/prices is rejected instead of being saved as flow data.
- `adj_factor` was also backfilled date-wide for all 125 2026 trading days (`688,070` rows, 5,552 distinct codes); its checkpoints are resumable just like the other TuShare datasets.
- Live probe at 12:13 on 2026-07-14: KPL returned 121 intraday points for 1/5 requested stocks and no sector-flow rows; the readiness report therefore remains `false` for the live gate. The historical tables are not promoted to “current” merely because they are available.

## 2026-07-14 盘中全市场覆盖整改

个股盘中资金流不再按候选股逐只请求。`scripts/collect_intraday_stock_flow_market.py` 直接调用东方财富 `RPT_DMSK_TS_STOCKNEW` 的全市场分页接口（每页最多 500 行），逐页限速、逐页落库，并写入 `intraday_stock_flow_page_checkpoint` 与 `intraday_stock_flow_batch`。只接受接口返回的同日 `TRADE_DATE`，不把最新快照回标为历史日期。2026-07-14 实测 11/11 页、5,184/5,184 只股票、100% 覆盖。

真实候选池由 `scripts/collect_realtime_limit_pool.py` 获取 KPL `/l2/realtime/all-boards`，必要时降级到 `/ladder/realtime-boards`；数据落入 `l2_realtime_all_boards`/`ladder_realtime_boards`，`v_limit_pool` 按同日、代码去重，只有真实涨停/连板池才能满足行动门槛。2026-07-14 实测 79 只候选股，`stock_candidate_score.source=limit_pool`，THS 快照不再作为行动门槛回退。

运行顺序：

```powershell
D:\anaconda\python.exe scripts/collect_realtime_limit_pool.py --db kpl_data.duckdb --date 2026-07-14
D:\anaconda\python.exe scripts/collect_intraday_stock_flow_market.py --db kpl_data.duckdb --date 2026-07-14
D:\anaconda\python.exe scripts/generate_signals.py --db kpl_data.duckdb --date 2026-07-14 --require-ready --readiness-stage intraday
D:\anaconda\python.exe scripts/check_data_readiness.py --db kpl_data.duckdb --date 2026-07-14 --stage intraday
```
-
The focused intraday collector runs KPL first, then probes the resilient stock/sector fallback graph only for gaps left by KPL. Stock fallback rows must carry the requested date; an undated sector snapshot is accepted only when its provider status is `live`/`refreshed`. The same total-budget clock applies to fallback probes, and stale rows are never written as current data.

## TuShare relay request policy

- Endpoint: `https://fastapic.stockai888.top`; the JSON envelope is `api_name`, `token`, `params`, and optional `fields`.
- The client strips whitespace around URL/token values. Keep the real token only in the local `.env` or process environment; never commit it.
- The relay limit is 100 requests/minute. Production requests are throttled to a 0.6-second start-to-start interval, including retries. Set `TUSHARE_RELAY_MIN_INTERVAL_SECONDS` only when the purchased limit is lower.
- Prefer one date-wide request for all stocks or one stock over a long date range. Do not loop stock-by-stock and day-by-day; collectors checkpoint each response to DuckDB before continuing.

## Intraday market and sector gates

- `fetch_all.py --only-market --strict` now exits after the market-context phase. It requires same-date `daily_summary` and `market_rise_fall`; `/market/rise-fall` can backfill `daily_summary` when `/daily` is temporarily empty, without relabelling an older response.
- `scripts/collect_capital_flow_focus.py` remains the bounded 20-sector fast tier. It rejects quote-only `/sector/capital` responses and no longer writes empty flow placeholders.
- `scripts/collect_intraday_sector_flow_full.py` is the full-sector tier. It first attempts paginated Eastmoney pages, then the Tushare relay, and finally derives THS concept flow from the complete same-day stock-flow batch. Results are written to `multi_source_sector_flow` and `sector_capital`, with `intraday_sector_flow_batch` coverage/status checkpoints. A partial batch keeps the intraday readiness gate closed.
- The default intraday stage requires `market_state`, candidate pool, sector flow, and stock flow. Dashboard and reports expose the full-sector coverage separately from the 20-sector fast tier.
