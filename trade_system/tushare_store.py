"""Pure TuShare field conversion and batch storage; no acquisition or task planning."""
from __future__ import annotations

from datetime import date
from trade_system.units import _number


def iso_date(value):
    raw = "".join(c for c in str(value or "") if c.isdigit())
    if len(raw) < 8:
        return None
    return date.fromisoformat(f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}").isoformat()


def stock_code_to_ts_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if "." in value:
        digits, suffix = value.split(".", 1)
        digits = "".join(ch for ch in digits if ch.isdigit()).zfill(6)[-6:]
        return f"{digits}.{suffix}"
    digits = "".join(ch for ch in value if ch.isdigit())
    if digits and len(digits) <= 6:
        digits = digits.zfill(6)
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def ts_code_to_stock_code(ts_code: str) -> str:
    return str(ts_code or "").split(".", 1)[0]


def index_code_to_ts_code(code: str) -> str:
    value = str(code or "").strip().upper()
    if "." in value:
        return value
    if value.startswith(("SH", "SZ", "BJ")) and len(value) > 2:
        return f"{value[2:]}.{value[:2]}"
    return stock_code_to_ts_code(value)


def ts_code_to_index_code(ts_code: str) -> str:
    value = str(ts_code or "").strip().upper()
    if "." not in value:
        return value
    digits, suffix = value.split(".", 1)
    return f"{suffix}{digits}"


MARKET_FIELDS = {
    "daily": "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg",
    "index_daily": "ts_code,trade_date,open,high,low,close,vol,amount,pct_chg",
    "daily_basic": "ts_code,trade_date,turnover_rate,volume_ratio,pe,pb,total_mv,circ_mv",
    "adj_factor": "ts_code,trade_date,adj_factor",
}


def market_batch(dataset, rows, provider):
    """One field/unit contract for both date-wide and instrument-scoped reads."""
    fields = MARKET_FIELDS[dataset].split(",")[2:]
    values = fields if dataset in {"daily_basic", "adj_factor"} else [
        "open", "high", "low", "close", "volume", "turnover", "change_pct"]
    is_index = dataset == "index_daily"
    columns = ["ts_code", "index_code" if is_index else "stock_code", "date", *values]
    price = dataset in {"daily", "index_daily"}
    if price:
        columns += ["volume_unit", "amount_unit", "adjustment", "provider"]
    out = []
    for row in rows:
        code = row["ts_code"]
        record = (code, ts_code_to_index_code(code) if is_index else ts_code_to_stock_code(code),
                  iso_date(row["trade_date"]), *(_number(row.get(f)) for f in fields))
        if price:
            record += ("hands", "thousand_yuan", "none", provider)
        out.append(record)
    return out, columns


def store_reference(store, dataset, rows):
    if dataset == "trade_cal":
        columns = ["exchange", "cal_date", "is_open", "pretrade_date"]
        out = [(r.get("exchange") or "SSE", iso_date(r.get("cal_date")),
                bool(int(r["is_open"])) if str(r.get("is_open")) in {"0", "1"} else None,
                iso_date(r.get("pretrade_date"))) for r in rows if r.get("cal_date")]
        keys = ["exchange", "cal_date"]
    elif dataset == "stock_basic":
        columns = ["ts_code", "stock_code", "stock_name", "area", "industry", "market", "list_date", "delist_date"]
        out = [(r["ts_code"], r.get("symbol") or ts_code_to_stock_code(r["ts_code"]),
                r.get("name") or "", r.get("area") or "", r.get("industry") or "",
                r.get("market") or "", iso_date(r.get("list_date")),
                iso_date(r.get("delist_date")) if r.get("list_status") == "D" else None) for r in rows if r.get("ts_code")]
        keys = ["ts_code"]
    else:
        raise ValueError(f"unsupported reference dataset: {dataset}")
    return store.insert_rows("tushare_" + dataset, out, columns, replace_on=keys)


def bulk_replace(con, table, rows, columns, replace_on):
    """Replace only supplied keys; the caller owns the transaction."""
    if not rows:
        return 0
    temp = "_history_batch"
    con.execute(f"DROP TABLE IF EXISTS {temp}")
    projection = ",".join(columns)
    con.execute(f"CREATE TEMP TABLE {temp} AS SELECT {projection} FROM {table} LIMIT 0")
    placeholders = ",".join("?" for _ in columns)
    con.executemany(f"INSERT INTO {temp}({projection}) VALUES ({placeholders})", rows)
    join = " AND ".join(f"target.{key}=batch.{key}" for key in replace_on)
    con.execute(f"DELETE FROM {table} AS target WHERE EXISTS (SELECT 1 FROM {temp} AS batch WHERE {join})")
    con.execute(f"INSERT INTO {table}({projection}) SELECT {projection} FROM {temp}")
    con.execute(f"DROP TABLE {temp}")
    return len(rows)
