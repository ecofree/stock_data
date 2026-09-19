# 历史补充职责

旧 `backfill_legacy_baostock.py` 及其并行版已退出。两者曾把 BaoStock 行情和不完整估值写入 TuShare 表，缺少逐行来源与单位约束；当前实现与历史输入可从 Git 基线 `84141f0` 查阅。

行情补充使用现有 `scripts/collect_multisource.py`：显式指定日期、证券、`--types kline`、原始价格 `--fq ""`，先 `--dry-run` 检查。运行仍受整改目录写入边界约束，生产保持停用。
它复用供应商取数、限时 SDK、原始回执及交易日覆盖检查，实际来源写入 `multi_source_kline`；缺少交易日历或来源数据时保留缺口，不冒充完成，也不把原始价格改标为复权价格。

TuShare 行情、估值继续由 `TushareHistoryCollector` 与 `backfill_2026_tushare.py` 管理。原 BaoStock 检查点、原始表和历史来源争议均保留；旧字段不迁移成获准估值，也不删除历史行。
