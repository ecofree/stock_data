# KPL 数据在 A 股交易与复盘中的应用分析

**基于 163 个可用 API 端点的系统性应用方案**

---

## 一、按交易日时段的数据应用

### 1.1 盘前准备 (8:30 — 9:15)

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **隔夜快讯** | `/advanced/news-flash`, `/advanced/news-flash-top` | 扫描隔夜重大消息，判断是否影响持仓或关注板块 |
| **板块新闻** | `/news/plate`, `/news/concept-jxbk`, `/news/theme` | 发现政策利好/利空催化的板块，建立当日观察清单 |
| **个股消息栏** | `/stock/message-bar` | 检查持仓和关注股的公告、研报、异动提示 |
| **个股研报** | `/tuyere/by-stock` | 券商评级变动、目标价调整，辅助买卖决策 |
| **热门主题** | `/theme/hot` | 市场当前最热的主题方向，判断延续性 |
| **节假日** | `/advanced/holiday` | 确认交易日/休市日，避免无效操作 |

**核心动作**: 建立当日"关注股票池"和"板块观察清单"

---

### 1.2 集合竞价 (9:15 — 9:25)

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **竞价异动** | `/auction/bidding-anomaly` | 发现竞价阶段异常放量的股票——可能是主力抢筹或出货信号 |
| **竞价分时** | `/auction/tick` | 跟踪关注股的竞价走势，判断开盘强弱 |
| **竞价历史** | `/advanced/bid-history` | 对比历史竞价数据，判断当日竞价是否异常 |
| **早盘竞价汇总** | `/advanced/morning-bidding-summary` | 全市场竞价概况，判断整体开盘情绪 |
| **早盘竞价列表** | `/advanced/morning-bidding-list` | 竞价强度排名，发现最强开盘方向 |
| **板块竞价比例** | `/advanced/bkjj-bl` | 各板块竞价参与度，判断资金主攻方向 |
| **竞价量K线** | `/advanced/bidvol-kline` | 个股竞价量的历史趋势，判断是否放量突破 |

**核心动作**:
1. 竞价异动 + 板块竞价比例 = 判断当日主攻板块
2. 竞价分时 + 竞价历史 = 确认关注股的开盘信号强度
3. 早盘竞价汇总 = 决定当日仓位策略（激进/保守/观望）

**实战信号**:
- 竞价异动数量 > 20 且集中在某一板块 → 该板块大概率是当日主线
- 关注股竞价量较前5日均量放大 3 倍以上 → 关注开盘后买入机会

---

### 1.3 早盘交易 (9:30 — 10:00)

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **市场情绪** | `/market/mood` | 开盘情绪定性(冰点/启动/主升/高潮/退潮)，决定仓位上限 |
| **涨跌停统计** | `/market/rise-fall` | 涨停/跌停数量对比，判断多空力量 |
| **实时涨停** | `/ladder/realtime-boards` | 谁最先封板？首板集中在哪些板块？ |
| **全市场涨停** | `/l2/realtime/all-boards` | 更实时的涨停监控 |
| **指数分时** | `/index/intraday` | 大盘走势确认，判断是否适合追高 |
| **指数完整信息** | `/index/full-info` | 上证/深证/创业板/科创板综合状态 |
| **指数实时** | `/advanced/zs-real` | 指数实时状态 |
| **盯盘雷达** | `/dingpan/radar` | 盘中异动实时推送 |
| **盯盘模块** | `/dingpan/module-versatile` | 多维度盯盘信号汇总 |
| **盯盘解释** | `/advanced/dp-explain` | 异动原因解读 |
| **盯盘实时数据** | `/advanced/dp-realdata` | 实时盘面数据 |

**核心动作**:
1. market/mood 定情绪基调 → 决定仓位（冰点轻仓试探，主升满仓进攻）
2. realtime-boards + 板块排行 → 确认主攻方向是否与盘前判断一致
3. 指数分时 → 确认大盘不破位，排除系统性风险

---

### 1.4 盘中监控 (10:00 — 14:30)

#### 板块轮动与资金流向

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **板块排行** | `/sector/ranking` | 当日板块涨幅排名，确认主线与支线 |
| **板块强度** | `/sector/strength` | 各板块强度值，量化板块相对强弱 |
| **板块强度批量** | `/sector/strength-batch` | 一次性获取多板块强度，高效对比 |
| **板块强度N日** | `/sector/strength-ndays` | 多日强度趋势，判断板块是启动期还是衰退期 |
| **板块强度DF** | `/sector/strength-dataframe` | 板块强度的时序数据，做趋势分析 |
| **板块涨停原因** | `/sector/boom-reason` | 理解板块为何涨停——是政策、事件还是资金驱动 |
| **板块分时直播** | `/sector/bk-fenshi-zhibo` | 板块盘中实时走势描述 |
| **板块资金** | `/sector/capital` | 板块资金净流入/流出，验证涨幅是否有资金支撑 |
| **板块信息** | `/sector/plate-info-qj` | 板块详细信息 |
| **板块成分股** | `/sector/stocks` | 确认板块龙头是谁 |
| **子板块/子概念** | `/sector/son-plates`, `/sector/sub-concepts` | 细分方向挖掘，发现更精确的投资标的 |
| **板块盯盘排列** | `/advanced/bk-dj-arrange` | 板块盯盘信号排序 |

#### 连板梯队与短线情绪

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **连板梯队** | `/ladder/market` | 当前连板高度（最高几板？几板股几只？） |
| **连板统计** | `/ladder/consecutive` | 各板高度的具体股票列表 |
| **板块连板** | `/ladder/sector` | 哪些板块连板股最多？（板块集中度） |
| **涨停板股票** | `/ladder/board-stocks` | 全部涨停股分类 |
| **炸板统计** | `/ladder/broken` | 炸板数量 → 封板成功率 → 短线情绪温度计 |
| **急跌统计** | `/ladder/sharp-withdrawal` | 盘中急跌股数量 → 恐慌情绪指标 |
| **涨停表达式** | `/advanced/zhangting-expression` | 涨停的结构化描述 |
| **涨停基因** | `/advanced/zhangting-gene` | 个股历史涨停特征，判断连板概率 |
| **涨停原因K线** | `/advanced/kline-zhangting-reason` | 历史涨停原因回溯 |

#### 个股深度分析

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **盘口** | `/advanced/pankou` | 五档盘口数据，判断买卖力量 |
| **大单K线** | `/advanced/dadan-kline`<br>`/advanced/dadan-kline-today` | 大单进出趋势，主力资金方向 |
| **当日大单K线新** | `/advanced/kline-today-dadan-new` | 更精确的当日大单分析 |
| **大单趋势增量** | `/advanced/dadan-trend-incremental` | 大单趋势变化 |
| **资金流向分钟** | `/advanced/zjmm-min` | 分钟级资金流向，判断盘中买卖时点 |
| **主力活跃K线** | `/advanced/main-activity-kline` | 主力活跃度历史趋势 |
| **当日主力活跃** | `/advanced/kline-today-main-activity` | 今日主力活跃状态 |
| **对倒K线** | `/advanced/duidao-kline`<br>`/advanced/kline-today-duidao` | 检测主力对倒行为(自买自卖拉抬/打压) |
| **托压单K线** | `/advanced/tuoyadan-kline`<br>`/advanced/kline-today-tyd` | 托单/压单变化，判断主力意图 |
| **主力监控** | `/advanced/main-monitor` | 综合主力行为监控 |
| **偏离值** | `/advanced/pianlizhi` | 股价偏离均线程度，判断超买超卖 |
| **偏离值多日/32周** | `/advanced/pianlizhi-many`<br>`/advanced/pianlizhi-w32` | 中长期偏离度分析 |
| **盘面实力** | `/advanced/pmsl` | 个股盘面强弱量化 |
| **量比** | `/advanced/vol-tur`<br>`/advanced/voltur-history` | 量比异动，判断是否异常放量 |
| **换手率前十** | `/advanced/turnover-ten` | 全市场换手率最高股，发现最活跃标的 |
| **筹码分布** | `/advanced/chouma` | 获利盘/套牢盘分布，判断压力位和支撑位 |
| **融资融券** | `/advanced/rqz-data` | 融资余额变化，判断杠杆资金方向 |

#### 风险预警

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **急跌监控** | `/l2/realtime/sharp-withdrawal` | 全市场急跌股实时监控 |
| **大提醒** | `/advanced/big-reminder` | 重要风险事件提醒 |
| **市场雷达** | `/advanced/market-radar` | 市场异动雷达扫描 |

**核心动作**:
1. **板块轮动判断**: sector/ranking + sector/strength 每30分钟刷新 → 确认主线是否切换
2. **封板成功率**: ladder/broken 炸板数 / ladder/market 涨停数 → < 30% 为强市，> 50% 为弱市
3. **龙头确认**: ladder/consecutive 最高板 + sector/stocks 板块成分 → 找到市场总龙头
4. **主力行为**: dadan-kline + duidaok-line + tuoyadan-kline → 三维判断主力真实意图
5. **风险预警**: sharp-withdrawal + big-reminder → 触发减仓/止损信号

---

### 1.5 尾盘决策 (14:30 — 15:00)

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **尾盘抢筹** | `/dingpan/weipan`, `/advanced/weipan-qiangchou` | 尾盘资金抢筹股列表 → 次日高开概率大 |
| **基金增持** | `/dingpan/jijin` | 机构尾盘增持方向 |
| **盯盘全部** | `/dingpan/all` | 尾盘全部盯盘信号汇总 |
| **上龙虎榜概率** | `/advanced/on-the-lhb` | 预测哪些股可能上龙虎榜 → 关注度高 |
| **大提醒** | `/advanced/big-reminder` | 收盘前的重要提醒 |

**核心动作**:
1. 尾盘抢筹信号 → 如果该股处于强势板块 + 技术形态好 → 尾盘买入次日高开
2. 结合市场情绪判断尾盘操作策略：
   - 情绪高潮期 → 尾盘减仓锁利
   - 情绪冰点期 → 尾盘低吸布局

---

### 1.6 盘后复盘 (15:00 — )

#### 第一层：市场全景

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **每日汇总** | `/daily` | 涨跌停数、上涨/下跌家数、连板数 → 市场温度定量描述 |
| **每日情绪** | `/daily/sentiment` | 情绪指数，辅助判断情绪周期位置 |
| **百日新高** | `/daily/new-high` | 新高股数量 → 市场赚钱效应强弱 |
| **涨跌停统计** | `/market/rise-fall` | 涨停/跌停详细统计 |
| **涨跌停明细** | `/market/limit-up-down` | 每一只涨停/跌停股的具体信息 |
| **每日导出** | `/daily/export` | 完整每日数据导出 |
| **赚钱效应历史** | `/market/emotion-money-date` | 赚钱效应趋势（多日对比） |
| **赚钱效应详情** | `/market/emotion-money-detail` | 赚钱效应分段统计 |
| **市场情绪统计** | `/advanced/market-mood-count` | 情绪周期量化统计 |
| **市场成交量** | `/advanced/market-scln` | 全市场成交量变化趋势 |
| **权重表现** | `/advanced/weight-performance` | 权重股表现，判断指数失真程度 |

#### 第二层：板块复盘

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **板块排行** | `/sector/ranking` | 当日板块涨跌排名，确认当日最强/最弱方向 |
| **板块连板** | `/ladder/sector` | 哪些板块涨停股最多？主线集中度如何？ |
| **板块涨停原因** | `/sector/boom-reason` | 理解每个板块为什么涨，判断持续性 |
| **板块强度** | `/sector/strength` | 各板块强度值对比 |
| **板块强度N日** | `/sector/strength-ndays` | 板块强度多日趋势，发现启动/衰退拐点 |
| **板块强度DF** | `/sector/strength-dataframe` | 板块强度详细时序分析 |
| **板块资金** | `/sector/capital` | 板块资金净流入排名，验证涨幅有资金驱动 |
| **板块全部股票** | `/sector/all-stocks` | 板块成分股完整列表 |
| **异动板块** | `/fengk/yd-plate`<br>`/fengk/yd-plate-info` | 盘中异动板块复盘 |

#### 第三层：连板梯队复盘

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **连板梯队** | `/ladder/market` | 最高板高度、各层级的股票数量 → 情绪周期定位 |
| **连板统计** | `/ladder/consecutive` | 各层级具体股票 → 龙头识别 |
| **炸板统计** | `/ladder/broken` | 炸板数量和原因 → 封板意愿分析 |
| **急跌统计** | `/ladder/sharp-withdrawal` | 急跌股列表 → 亏钱效应来源 |
| **涨停表达式** | `/advanced/zhangting-expression` | 涨停的结构化归因 |
| **历史涨幅详情** | `/advanced/his-zhangfu-detail` | 个股历史涨幅回溯 |
| **历史急跌** | `/advanced/his-sharp-withdrawal` | 历史急跌模式分析 |

#### 第四层：龙虎榜与游资

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **龙虎榜列表** | `/lhb/list` | 当日龙虎榜股票 |
| **龙虎榜详情** | `/lhb/detail` | 买入/卖出营业部明细 |
| **龙虎榜原始** | `/lhb/raw-list` | 原始龙虎榜数据 |
| **龙虎榜DataFrame** | `/lhb/dataframe` | 结构化龙虎榜数据 |
| **龙虎榜更新** | `/lhb/update-list` | 龙虎榜最新更新 |
| **龙虎榜标题** | `/lhb/top-title` | 龙虎榜重点标题 |
| **游资动向** | `/lhb/youzi-dongxiang` | 知名游资席位的买卖动态 |
| **机构列表** | `/advanced/agency-list` | 机构席位交易明细 |
| **营业部列表** | `/advanced/business-list` | 营业部交易数据 |

**龙虎榜复盘要点**:
1. 知名游资（如赵老哥、炒股养家）买入 → 关注次日溢价机会
2. 机构净买入 → 中线看好信号
3. 同一股票多路游资合力 → 强一致性信号
4. 游资连续买入同一板块多只股票 → 板块性机会确认

#### 第五层：深度复盘与次日准备

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **复盘列表** | `/fengk/list` | 当日复盘要点汇总 |
| **复盘精选** | `/advanced/fengk-best` | 精选复盘内容 |
| **盘后复盘** | `/advanced/disk-review` | 结构化盘后复盘数据 |
| **概念点** | `/advanced/concept-point` | 概念板块关联分析 |
| **关联关系** | `/advanced/relation` | 股票之间的关联（如同一实控人、同一产业链） |
| **调研** | `/advanced/interviews` | 机构调研动态，了解机构关注方向 |
| **新高分组** | `/advanced/newhigh-group-count`, `/advanced/newhigh-group-stocks` | 创新高股票分组，判断赚钱效应层次 |

---

## 二、情绪周期模型——核心应用框架

情绪周期是 A 股短线交易的核心框架。KPL 数据可以构建一个**五阶段情绪状态机**:

```
冰点 → 启动 → 主升 → 高潮 → 退潮 → (冰点)
```

### 2.1 情绪判断指标矩阵

| 指标 | 冰点 | 启动 | 主升 | 高潮 | 退潮 |
|------|------|------|------|------|------|
| 涨停数 (rise-fall) | < 20 | 20-40 | 40-80 | > 80 | 递减 |
| 跌停数 | > 30 | < 20 | < 10 | < 5 | 递增 |
| 最高板 (ladder/market) | 2-3板 | 3-4板 | 5-7板 | 7板+ | 断板 |
| 炸板率 (broken/total) | > 60% | 40-60% | 20-40% | < 20% | > 40% |
| 连板晋级率 (consecutive) | < 20% | 20-40% | 40-60% | > 60% | < 30% |
| 赚钱效应 (emotion-money) | 极差 | 转好 | 强 | 极强 | 转差 |
| 新高股数 (new-high) | < 50 | 50-100 | 100-200 | > 200 | 递减 |
| 板块强度集中度 (sector/strength) | 分散 | 开始集中 | 高度集中 | 扩散 | 回落 |
| 情绪指数 (market/mood) | 冰点 | 启动 | 主升 | 高潮 | 退潮 |

### 2.2 情绪周期对应的交易策略

| 阶段 | 仓位 | 操作策略 | 选股方向 | 数据来源优先级 |
|------|------|----------|----------|----------------|
| **冰点** | 10-20% | 轻仓试探，低吸为主 | 超跌反弹股、逆势走强股 | ladder/broken(炸板多→超跌), sector/strength(逆势强板块) |
| **启动** | 30-50% | 确认信号后加仓，追强龙头 | 首板/二板龙头，启动板块 | ladder/market(连板梯队), sector/ranking(板块排名) |
| **主升** | 60-80% | 重仓龙头，打板/低吸均可 | 最高板龙头，主线板块 | ladder/consecutive(龙头股), boom-reason(持续逻辑) |
| **高潮** | 逐步减至30% | 锁利为主，只出不进 | 兑现利润 | limit-up-down(涨停数见顶), broken(炸板开始增加) |
| **退潮** | 0-10% | 空仓观望，等下一轮 | 不做 | sharp-withdrawal(急跌增多), emotion-money(赚钱效应下降) |

---

## 三、选股体系——数据驱动的标的筛选

### 3.1 短线选股漏斗

```
全市场 5000+ 股票
    ↓ [板块排行 + 板块强度] → 锁定 3-5 个强势板块
    ↓ [板块成分股 + 连板统计] → 找出板块内最强 10-20 只
    ↓ [连板梯队 + 涨停基因] → 筛选龙头候选 3-5 只
    ↓ [大单K线 + 盘口 + 筹码] → 确认主力意图和技术形态
    ↓ [竞价异动 + 竞价历史] → 次日竞价确认
    → 最终买入 1-2 只
```

### 3.2 中线选股框架

| 维度 | 数据源 | 判断标准 |
|------|--------|----------|
| **基本面** | `/finance/summary`, `/finance/income`, `/finance/balance`, `/finance/cashflow` | 营收增长 > 20%，净利润增长 > 30%，ROE > 15% |
| **财务对比** | `/finance/compare` | 同行业财务指标横向对比 |
| **机构持仓** | `/stock/institutional-positions`, `/stock/holding-funds` | 机构持仓增加，知名基金新进 |
| **股东信息** | `/stock/gudong`, `/advanced/gudong-info`, `/advanced/gudong-renshu` | 股东人数减少（筹码集中），大股东增持 |
| **公司信息** | `/stock/company-info`, `/stock/gpcphbts-tag` | 行业地位、概念标签 |
| **调研动态** | `/advanced/interviews` | 近期机构密集调研 |
| **筹码分布** | `/advanced/chouma` | 底部筹码集中，上方套牢盘少 |
| **融资融券** | `/advanced/rqz-data` | 融资余额持续增加 |

### 3.3 个股K线形态分析

KPL 提供了**12 种维度的 K 线数据**，这是非常强大的多角度分析工具:

| K线类型 | 端点 | 分析用途 |
|---------|------|----------|
| 基础K线 | `/kline` | 标准 OHLCV |
| 大单K线 | `/advanced/dadan-kline` | 只统计大单的K线 → 过滤散户噪音 |
| 当日大单K线 | `/advanced/dadan-kline-today` | 日内大单分时 |
| 大单K线新 | `/advanced/dadan-kline-new` | 改进版大单K线 |
| 股价K线 | `/advanced/gujia-kline` | 股价走势对比 |
| 竞价量K线 | `/advanced/bidvol-kline` | 竞价量的历史趋势 |
| 对倒K线 | `/advanced/duidao-kline` | 检测主力对倒 |
| 托压单K线 | `/advanced/tuoyadan-kline` | 托单/压单变化 |
| 主力活跃K线 | `/advanced/main-activity-kline` | 主力活跃度趋势 |
| 涨停原因K线 | `/advanced/kline-zhangting-reason` | 历史涨停归因 |
| K线成交量预测 | `/advanced/kline-volume-forecast` | 量能预测 |
| 分时K线 | `/advanced/fenshi-kline-option` | 分时K线配置 |

**组合分析技巧**:
- **大单K线 vs 基础K线**: 如果基础K线涨但大单K线跌 → 散户推动，不可持续
- **对倒K线异常放大**: 主力可能在制造虚假成交量
- **托压单变化趋势**: 压单持续减少 + 托单增加 → 主力准备拉升
- **竞价量K线突破**: 竞价量创近期新高 + 价格突破 → 强信号

---

## 四、板块交易体系

### 4.1 板块强度跟踪系统

```python
# 每日盘后计算板块强度趋势
sector_strength_today = sector/ranking  # 当日排名
sector_strength_ndays = sector/strength-ndays  # N日趋势
sector_capital = sector/capital  # 资金流向

# 板块启动信号
def detect_sector_launch(sector_code):
    """板块启动 = 强度连续3日上升 + 资金由流出转流入"""
    strength_trend = get_ndays_strength(sector_code)
    capital_flow = get_capital_flow(sector_code)
    return (strength_trend[-3:] 递增) and (capital_flow 转正)

# 板块见顶信号
def detect_sector_top(sector_code):
    """板块见顶 = 强度仍在高位但资金开始流出 + 成分股开始分化"""
    strength = get_current_strength(sector_code)
    capital = get_capital_flow(sector_code)
    stocks_divergence = check_stocks_divergence(sector_code)
    return (strength > 80) and (capital 转负) and (stocks_divergence)
```

### 4.2 板块层级关系分析

```
一级行业 (/sector/plates)
  └── 子板块 (/sector/son-plates, /sector/son-plate-direct)
      └── 子概念 (/sector/sub-concepts)
  └── 父板块 (/sector/parent-plate)
```

**应用**: 当一个板块走强时，通过层级关系找到更精确的细分方向:
- 例: "新能源"走强 → 查看子板块 → 发现"光伏"比"锂电"更强 → 聚焦光伏龙头

---

## 五、ETF 与现货交易

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **ETF排行** | `/etf/ranking` | ETF涨幅排名 → 发现最强赛道ETF |
| **ETF全部** | `/etf/all` | 全市场ETF数据 |
| **现货列表** | `/xianhuo/list` | 现货市场数据 |
| **可转债选项** | `/advanced/convertible-bonds-option` | 可转债分析 |

**ETF 轮动策略**: 当判断某板块进入主升期但无法确定个股时，直接买入对应板块 ETF 是更安全的选择。

---

## 六、历史数据回填的价值

42 个支持回填的端点可以构建**历史情绪数据库**，用于:

### 6.1 回测验证

| 可回填数据 | 回测应用 |
|------------|----------|
| `/market/rise-fall` 涨跌停历史 | 验证"涨停数>80次日回调"等统计规律 |
| `/ladder/market` 连板历史 | 统计"5板以上龙头次日表现" |
| `/sector/ranking` 板块排名历史 | 验证"连续3日排名前5的板块第4日表现" |
| `/sector/strength` 板块强度历史 | 构建板块强度动量策略 |
| `/lhb/list` 龙虎榜历史 | 统计"游资买入后5日收益" |
| `/daily/export` 每日导出 | 完整的日度情绪指标序列 |
| `/advanced/disk-review` 盘后复盘 | 历史复盘内容回溯 |
| `/advanced/weight-performance` 权重表现 | 权重股与题材股的跷跷板效应 |

### 6.2 模式识别

通过历史数据可以发现反复出现的模式:
- **情绪冰点后的反弹规律**: 统计过去 N 次冰点后，哪种类型的股票反弹最强
- **板块轮动节奏**: A 板块主升 → B 板块接力 → C 板块补涨的传导路径
- **龙虎榜溢价**: 特定游资买入后的平均次日溢价率

---

## 七、L2 数据的应用（板块级可用）

虽然个股 L2 受权限限制，但以下 L2 数据可用:

| 数据源 | 端点 | 应用方式 |
|--------|------|----------|
| **逐笔委托** | `/l2/tick-orders` | 盘口逐笔成交，判断真实买卖力量 |
| **全部逐笔** | `/l2/tick-orders-all` | 全量逐笔数据 |
| **个股大单** | `/l2/stock-bigorder` | 大单实时推送 |
| **指数分时趋势** | `/l2/realtime/index-trend` | 指数级别的实时趋势 |
| **急跌监控** | `/l2/realtime/sharp-withdrawal` | 全市场急跌实时预警 |
| **全市场涨停** | `/l2/realtime/all-boards` | 实时涨停监控 |
| **指数列表** | `/l2/realtime/index-list` | 多指数实时状态 |

---

## 八、建议的自动化工作流

### 8.1 每日自动任务

| 时间 | 任务 | 涉及数据 |
|------|------|----------|
| **08:30** | 盘前扫描 | news-flash, news/plate, theme/hot, stock/message-bar |
| **09:15** | 竞价监控 | bidding-anomaly, morning-bidding-summary, bkjj-bl |
| **09:30** | 开盘确认 | market/mood, rise-fall, realtime-boards, index/intraday |
| **10:00** | 板块确认 | sector/ranking, sector/strength, ladder/market |
| **10:30** | 盘中扫描 | ladder/broken, ladder/consecutive, dingpan/radar |
| **11:00** | 半日小结 | market/rise-fall, sector/strength, dadan-kline |
| **13:00** | 午后开盘 | market/mood, index/intraday, ladder/realtime-boards |
| **14:00** | 尾盘前扫描 | sector/ranking, ladder/market, sector/strength |
| **14:45** | 尾盘决策 | dingpan/weipan, weipan-qiangchou, on-the-lhb |
| **15:30** | 自动复盘 | 全部P0/P1数据 → 生成九章复盘报告 |
| **20:00** | 龙虎榜分析 | lhb/list, lhb/detail, youzi-dongxiang |

### 8.2 关键告警规则

| 告警 | 条件 | 动作 |
|------|------|------|
| **情绪骤变** | market/mood 从主升/高潮变为退潮 | 立即减仓至30%以下 |
| **炸板激增** | ladder/broken 炸板数突然 > 50% | 停止打板，观望 |
| **急跌预警** | l2/realtime/sharp-withdrawal 触发 | 检查持仓是否受影响 |
| **龙头断板** | ladder/consecutive 最高板股票未连板 | 减仓该龙头 |
| **板块崩盘** | sector/strength 主线板块强度骤降 | 清仓该板块持仓 |
| **游资撤退** | lhb/youzi-dongxiang 知名游资净卖出 | 警惕次日低开 |

---

## 九、数据质量与注意事项

### 9.1 数据时效性

| 类别 | 更新频率 | 注意事项 |
|------|----------|----------|
| 实时数据（涨停/跌停/指数） | 秒级 | 仅限盘中 |
| 板块数据 | 分钟级 | 盘中有延迟 |
| 龙虎榜 | 盘后 18:00+ | 当日收盘后才发布 |
| 财务数据 | 季度/年度 | 更新频率低 |
| L2 数据 | 秒级 | 板块级可用，个股级受限 |

### 9.2 已知限制

1. **L2 个股数据不可用**: `/l2/stock-intraday` 和 `/l2/tick-history` 始终返回空
2. **main-force 模块已下线**: 3 个端点永久 404
3. **条件性数据**: 部分端点仅在特定交易日有数据（如龙虎榜需要当日有公告）
4. **板块代码格式**: `/sector/capital` 需要特定格式（如 "801001"），不是所有代码都支持
5. **指数列表**: `/index/list` 需要历史日期才能查询

---

## 十、数据采集系统现状

采集系统已修通。当前数据库状态：

| 指标 | 数值 |
|------|------|
| 数据库 | kpl_data.duckdb (71MB) |
| 表数 | 84 张，全部非空 |
| 日期范围 | 2025-06-25 ~ 2026-07-06 (250天) |

### 各维度覆盖

| 维度 | 数据量 | 日期范围 |
|------|--------|----------|
| 每日情绪 | 750 rows | 250 天 |
| 连板梯队 | 604 rows | 2026-06-01 ~ 07-06 |
| 龙虎榜 | 256 rows | 2026-06-26 ~ 07-06 |
| 板块排行 | 215 rows | 最新 |
| 个股深度 | 1,349 rows | 71 只 |
| 高级K线 | 600 rows | 50 只，6维度 |
| L2 板块分时 | 3,920 rows | 最新 |

### 运行方式



### 待补充

- 龙虎榜历史回填（更多日期）
- 基础K线不做（akshare 更全稳定）
- L2个股逐笔不可用（权限限制）
- /daily/export 为服务器端CSV生成，API不返回数据
