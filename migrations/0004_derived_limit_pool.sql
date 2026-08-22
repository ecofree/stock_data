CREATE TABLE IF NOT EXISTS derived_limit_up_daily (
    trade_date DATE,
    stock_code VARCHAR,
    stock_name VARCHAR,
    close DOUBLE,
    pct_chg DOUBLE,
    board_level INTEGER,
    source_note VARCHAR DEFAULT 'derived_from_kline',
    created_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(trade_date, stock_code)
);
