"""One input contract for retained KPL sector projections, before publication."""
import math
from trade_system.market_contract import market_count, validate_requested_day


def identity(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).strip():
        raise ValueError('sector response: invalid identity')
    return str(value).strip()


def envelope(data, day, sector=None):
    validate_requested_day(data, day)
    if isinstance(data, dict) and sector is not None:
        for key in ('sector_code', 'plate_code'):
            if key in data and identity(data[key]) != identity(sector):
                raise ValueError('sector response: wrong sector identity')


def number(value, *, count=False, nonnegative=False):
    if value is None:
        return None
    if count:
        return market_count(value)
    if isinstance(value, bool):
        raise ValueError('sector response: boolean numeric value')
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise ValueError('sector response: invalid numeric value') from exc
    if not math.isfinite(result) or (nonnegative and result < 0):
        raise ValueError('sector response: invalid numeric value')
    return result


def members(data, day, sector):
    """Return typed members. Missing quote dates never refresh quote evidence."""
    if data is None:
        return []
    envelope(data, day, sector)
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get('stocks', data.get('data'))
    else:
        rows = None
    if not isinstance(rows, list):
        raise ValueError('sector response: member list required')
    seen, result = set(), []
    dated = isinstance(data, dict) and any(k in data for k in ('date','trade_date','source_date'))
    for row in rows:
        if isinstance(row, dict):
            envelope(row, day, sector)
            code = row.get('stock_code', row.get('code', row.get('股票代码')))
            name = row.get('stock_name', row.get('name', row.get('股票名称', '')))
            values = (row.get('change_pct', row.get('涨跌幅')),
                      row.get('turnover', row.get('成交额')),
                      row.get('market_cap', row.get('流通市值')))
            row_dated = dated or any(k in row for k in ('date','trade_date','source_date'))
        elif isinstance(row, (tuple,list)) and 2 <= len(row) <= 5:
            code, name = row[:2]
            values = tuple(row[2:]) + (None,) * (5-len(row))
            row_dated = dated
        else:
            raise ValueError('sector response: invalid member row')
        code = identity(code)
        if code in seen or not isinstance(name, str):
            raise ValueError('sector response: duplicate identity or invalid name')
        seen.add(code)
        parsed = tuple(number(value, nonnegative=i > 0) for i,value in enumerate(values))
        result.append((day, identity(sector), code, name, *(parsed if row_dated else (None,None,None))))
    return result


def strength(data, day, sector):
    if data is None:
        return None
    if not isinstance(data, dict):
        raise ValueError('sector response: strength object required')
    envelope(data, day, sector)
    if not any(k in data for k in ('date','trade_date','source_date')):
        raise ValueError('sector response: explicit strength date required')
    fields = (('strength','强度'), ('zhangting','涨停'), ('fengban','封板率'),
              ('dieting','跌停'), ('up_count','上涨家数'), ('down_count','下跌家数'))
    values = [number(data.get(key, data.get(alias)), count=i in (1,3,4,5))
              for i,(key,alias) in enumerate(fields)]
    if all(value is None for value in values):
        raise ValueError('sector response: missing strength observations')
    return (day, identity(sector), *values)
