-- 0014: qlib_shadow_evaluation 版本化评估列（幂等；已发布后只增不改）。
-- 背景：evaluate_qlib_shadow 改为按 (model, method, quantile, cost) 追加式覆盖，
-- 不再全表 DELETE；cost 列持久化，避免把毛收益误读为可执行收益。
-- 注意：该表由 ensure_qlib_shadow_tables 运行时懒建，新库可能不存在，先建骨架。
CREATE TABLE IF NOT EXISTS qlib_shadow_evaluation (
    model_id VARCHAR,
    sample_start VARCHAR,
    sample_end VARCHAR,
    sample_count INTEGER,
    ic DOUBLE,
    rank_ic DOUBLE,
    avg_forward_return DOUBLE,
    top_quantile_return DOUBLE,
    bottom_quantile_return DOUBLE,
    top_bottom_spread DOUBLE,
    hit_rate DOUBLE,
    top_hit_rate DOUBLE,
    daily_top_hit_rate DOUBLE,
    max_drawdown DOUBLE
);
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS evaluation_method VARCHAR;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS quantile DOUBLE;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS round_trip_cost_bps DOUBLE;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS avg_net_return DOUBLE;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS net_top_bottom_spread DOUBLE;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS top_quantile_net_return DOUBLE;
ALTER TABLE qlib_shadow_evaluation ADD COLUMN IF NOT EXISTS bottom_quantile_net_return DOUBLE;
