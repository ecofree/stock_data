CREATE TABLE IF NOT EXISTS official_limit_pool (
    trade_date DATE,
    stock_code VARCHAR,
    stock_name VARCHAR,
    limit_up_time VARCHAR,
    continue_day_cnt INTEGER,
    limit_up_reason VARCHAR,
    close DOUBLE,
    pct_chg DOUBLE,
    seal_money DOUBLE,
    max_seal_money DOUBLE,
    is_st BOOLEAN DEFAULT false,
    source VARCHAR DEFAULT 'hithink',
    fetched_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, stock_code)
);

CREATE TABLE IF NOT EXISTS market_journal (
    trade_date DATE PRIMARY KEY,
    note VARCHAR,
    tags VARCHAR,
    created_at TIMESTAMP DEFAULT current_timestamp
);
