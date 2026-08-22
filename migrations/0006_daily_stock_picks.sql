CREATE TABLE IF NOT EXISTS daily_stock_picks (
    trade_date DATE,
    stock_code VARCHAR,
    stock_name VARCHAR,
    rank INTEGER,
    total_score DOUBLE,
    board INTEGER,
    limit_up_reason VARCHAR,
    factor_json VARCHAR,
    llm_bull_case VARCHAR,
    llm_risk VARCHAR,
    llm_watch_condition VARCHAR,
    phase VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, stock_code)
);
