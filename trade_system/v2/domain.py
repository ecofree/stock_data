"""Strict boundary types shared by replay, accounts and decisions."""
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re


def utc(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError('timezone-aware timestamp required')
    return value.astimezone(timezone.utc)


def now_utc():
    return datetime.now(timezone.utc)


def number(value):
    if isinstance(value, bool) or value is None:
        raise ValueError('finite numeric value required')
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError('invalid number') from exc
    if not result.is_finite():
        raise ValueError('finite numeric value required')
    return result


def money(value):
    value = number(value) * 100
    if value < 0 or value != value.to_integral_value() or value > 10**16:
        raise ValueError('nonnegative money with at most two decimal places required')
    return int(value)


def quantity(value):
    if type(value) is not int or not 0 <= value <= 10**12:
        raise ValueError('nonnegative integer quantity required')
    return value


def instrument(value):
    if not isinstance(value, str) or not re.fullmatch(r'(SH|SZ|BJ)\.[0-9]{6}', value):
        raise ValueError('explicit exchange and six digit instrument required')
    return value


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def identity(value):
    return hashlib.sha256(canonical(value).encode('utf-8')).hexdigest()
