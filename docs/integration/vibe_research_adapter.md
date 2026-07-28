# Vibe-Research Adapter

本阶段只吸收 Vibe-Research 的投研与复盘能力，不把它变成主交易信号系统。

## 已接入边界

- `backend/news_sources.json`：读取行业、RSS 源和 fetch 配置。
- `news_radar_item`：保存客观资讯雷达条目。
- `research_note`：保存操盘研究笔记。
- `research_report_file`：保存研报/公告/附件索引。
- `v_operator_research_context`：把新闻、笔记和研报统一成操盘上下文视图。

## 安全约束

- 默认不联网抓取，只读取 Vibe 配置或已有 radar cache。
- 不读取、不输出、不保存密钥。
- 不生成买卖指令。
- 不改写 raw 行情数据。
- AI 只能用于摘要、归因和复盘，不参与自动交易。

## 后续增强

- Phase 16 将把研究上下文接入候选股报告。
- Phase 17 将在 HTML 操盘台展示盘前催化、风险事件和盘后研究记录。
