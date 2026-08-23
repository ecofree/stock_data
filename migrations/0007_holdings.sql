CREATE TABLE IF NOT EXISTS holdings (
    stock_code VARCHAR,
    stock_name VARCHAR DEFAULT '',
    entry_date DATE,
    entry_price DOUBLE,
    shares INTEGER DEFAULT 0,
    stop_loss_price DOUBLE,
    target_price DOUBLE,
    status VARCHAR DEFAULT 'open',
    exit_date DATE,
    exit_price DOUBLE,
    notes VARCHAR DEFAULT '',
    updated_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY(stock_code, entry_date)
);
