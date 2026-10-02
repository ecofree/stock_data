"""TuShare conversions and retained valuation evidence; no acquisition or tasks.

The strict financial reader borrows a caller-owned connection and shares one
qualification implementation with the operational collector. Batch writes below
remain explicit caller-owned operations, never part of the evidence reader.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
import hashlib
import io
import json
import math
import re
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from threading import RLock
from typing import Any

import duckdb
from trade_system.units import _number

_num = _number


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
    if digits.startswith('92'):
        return f"{digits}.BJ"
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
    """Upsert supplied keys without delete/reinsert; caller owns transaction.

    DuckDB's indexed deletes may retain keys until commit. Deleting a whole
    slice and reinserting it can fail on a repeated production-size batch.
    """
    if not rows:
        return 0
    temp = "_history_batch"
    con.execute(f"DROP TABLE IF EXISTS {temp}")
    projection = ",".join(columns)
    con.execute(f"CREATE TEMP TABLE {temp} AS SELECT {projection} FROM {table} LIMIT 0")
    placeholders = ",".join("?" for _ in columns)
    con.executemany(f"INSERT INTO {temp}({projection}) VALUES ({placeholders})", rows)
    join = " AND ".join(f"target.{key}=batch.{key}" for key in replace_on)
    keys = ','.join(replace_on)
    if con.execute(f"SELECT 1 FROM {temp} GROUP BY {keys} HAVING count(*)>1 LIMIT 1").fetchone():
        raise ValueError('duplicate keys in replacement batch')
    if con.execute(f"SELECT 1 FROM {temp} WHERE " + ' OR '.join(f'{k} IS NULL' for k in replace_on)
                   + ' LIMIT 1').fetchone():
        raise ValueError('null keys in replacement batch')
    assignments = ','.join(f'{col}=batch.{col}' for col in columns if col not in replace_on)
    if assignments:
        con.execute(f"UPDATE {table} AS target SET {assignments} FROM {temp} AS batch WHERE {join}")
    con.execute(f"INSERT INTO {table}({projection}) SELECT {','.join('batch.'+c for c in columns)} "
                f"FROM {temp} AS batch WHERE NOT EXISTS (SELECT 1 FROM {table} AS target WHERE {join})")
    con.execute(f"DROP TABLE {temp}")
    return len(rows)


_VALUATION_PAGE_CACHE = OrderedDict()
_VALUATION_PDF_LOCK = RLock()
_PDF_MAX_BYTES = 32 * 1024 * 1024
_PDF_MAX_PAGE_BYTES = 4 * 1024 * 1024
_PDF_MAX_PAGE_TEXT = 300_000
_VALUATION_UNITS = {'price': 'yuan', 'total_share': '10000_shares',
    'float_share': '10000_shares', 'equity': 'yuan', 'other_equity': 'yuan',
    'total_mv': '10000_yuan', 'circ_mv': '10000_yuan', 'parent_profit': 'yuan',
    'ordinary_profit': 'yuan', 'other_equity_profit_distribution': 'yuan'}
# Native field meanings belong to the product, not to the caller's value_path.
_VALUATION_NATIVE_FIELDS = {
    'daily': {'price': 'close'},
    'daily_basic': {'price': 'close', 'total_share': 'total_share',
        'float_share': 'float_share', 'total_mv': 'total_mv', 'circ_mv': 'circ_mv'},
    'stk_premarket': {'total_share': 'total_share', 'float_share': 'float_share'},
    'balancesheet': {'equity': 'total_hldr_eqy_exc_min_int', 'other_equity': 'oth_eqt_tools'},
}
_FINANCIAL_LABELS = {
    'equity': ('归属于母公司所有者权益合计', '归属于母公司股东权益合计',
        '归属于母公司所有者权益（或股东权益）合计', 'parent equity'),
    'other_equity': ('其他权益工具', 'other equity'),
    'parent_profit': ('归属于母公司所有者的净利润', '归属于母公司股东的净利润', 'parent profit'),
    'ordinary_profit': ('归属于公司普通股股东的净利润', '归属于母公司普通股股东的净利润',
        '归属于普通股股东的净利润', 'ordinary profit'),
    'other_equity_profit_distribution': ('归属于其他权益工具持有者的净利润',
        '其他权益工具利润分配', 'other equity profit distribution'),
}


def _page_amounts(text):
    """Preserve accounting parentheses; never find a positive number inside a loss."""
    normalized = text.replace('，', ',').replace('−', '-').replace('（', '(').replace('）', ')')
    pattern = r'(?<![\d.,(])(?:\(\s*\d[\d,]*(?:\.\d+)?\s*\)|-?\d[\d,]*(?:\.\d+)?)(?![\d.,)])'
    values = []
    for token in re.findall(pattern, normalized):
        token = token.strip()
        negative = token.startswith('(')
        value = Decimal(token.strip('() ').replace(',', ''))
        values.append(-value if negative else value)
    return values


def _compact_text(text):
    return re.sub(r'\s+', '', str(text)).replace('，', ',').replace('−', '-').replace('（', '(').replace('）', ')')


def _column_period(header, statement_period):
    end = date.fromisoformat(_iso(statement_period))
    text = _compact_text(header)
    if text in {'期末余额', '本期发生额', '本期金额', '本报告期'}:
        return end.isoformat()
    if text == '期初余额':
        return f'{end.year-1}-12-31'
    if text in {'上期发生额', '上期金额', '上年同期'}:
        return end.replace(year=end.year-1).isoformat()
    exact = re.fullmatch(r'((?:19|20)\d{2})[-年](\d{1,2})[-月](\d{1,2})日?', text)
    if exact:
        return date(*(int(p) for p in exact.groups())).isoformat()
    period = re.fullmatch(r'((?:19|20)\d{2})年(?:1[-—至](3|6|9|12)月|(?:度|全年))', text)
    if period:
        month = int(period[2] or 12)
        return f'{period[1]}-{month:02d}-{31 if month in {3,12} else 30}'
    raise ValueError('financial period column meaning unproved')


def _document_pages(raw, digest, field, document_format):
    """Return only explicitly cited physical pages; no whole-PDF search fallback."""
    with _VALUATION_PDF_LOCK:
        return _document_pages_locked(raw, digest, field, document_format)


def _document_pages_locked(raw, digest, field, document_format):
    span = field.get('page_range', [field.get('page'), field.get('page')])
    if (not isinstance(span, list) or len(span) != 2
            or any(isinstance(p, bool) or not isinstance(p, int) for p in span)
            or not 1 <= span[0] <= span[1] or span[1] - span[0] > 3
            or (field.get('page') is not None and field['page'] != span[0])):
        raise ValueError('explicit physical page or bounded page range required')
    if len(raw) > _PDF_MAX_BYTES:
        raise ValueError('valuation document exceeds size limit')
    if document_format != 'pdf':
        if span != [1, 1]:
            raise ValueError('non-PDF document requires physical page one')
        return raw.decode('utf-8')
    missing = [page for page in range(span[0], span[1]+1) if (digest, page) not in _VALUATION_PAGE_CACHE]
    if missing:
        try:
            from pypdf import PdfReader, filters
            from pypdf import __version__ as pdf_version
            if tuple(int(p) for p in pdf_version.split('.')[:2]) < (6, 19):
                raise ValueError('patched pypdf 6.19 or newer required')
            # Keep the decoder's own allocation guard enabled and bounded.
            limit = filters.ZLIB_MAX_OUTPUT_LENGTH
            filters.ZLIB_MAX_OUTPUT_LENGTH = min(limit or _PDF_MAX_PAGE_BYTES, _PDF_MAX_PAGE_BYTES)
            try:
                reader = PdfReader(io.BytesIO(raw), strict=True)
                if reader.is_encrypted or len(reader.pages) > 800 or span[1] > len(reader.pages):
                    raise ValueError('valuation PDF page scope unavailable')
                for number in missing:
                    page = reader.pages[number-1]
                    content = page.get_contents()
                    if content is not None and len(content.get_data()) > _PDF_MAX_PAGE_BYTES:
                        raise ValueError('valuation page content exceeds limit')
                    value = page.extract_text() or ''
                    if len(value) > _PDF_MAX_PAGE_TEXT:
                        raise ValueError('valuation page text exceeds limit')
                    _VALUATION_PAGE_CACHE[digest, number] = value
                    while len(_VALUATION_PAGE_CACHE) > 256:
                        _VALUATION_PAGE_CACHE.popitem(last=False)
            finally:
                filters.ZLIB_MAX_OUTPUT_LENGTH = limit
        except ImportError as exc:
            raise ValueError('PDF extraction dependency unavailable; document unqualified') from exc
        except ValueError:
            raise
        except Exception as exc:
            raise ValueError('cited valuation PDF page cannot be extracted') from exc
    return '\n'.join(_VALUATION_PAGE_CACHE[digest, p] for p in range(span[0], span[1]+1))


def _pdf_financial_column_amount(raw, digest, field):
    """Locate a sparse financial cell from original PDF glyph coordinates.

    Declared blank cells and flattened text cannot establish the column. This
    narrowly supports right-aligned amounts beside physically verified dated
    headers; ambiguous geometry remains unqualified.
    """
    from pypdf import PdfReader, filters
    from pypdf._font import Font
    from pypdf._text_extraction import mult
    location = field['location']
    proof = location.get('pdf_column_layout', {})
    span = field.get('page_range', [field.get('page'), field.get('page')])
    header_page, row_page = proof.get('header_page'), proof.get('row_page')
    if (proof.get('schema') != 'physical_financial_columns_v1' or proof.get('alignment') != 'right'
            or any(isinstance(p,bool) or not isinstance(p,int) or not span[0] <= p <= span[1]
                   for p in (header_page,row_page)) or row_page < header_page
            or (field.get('value_physical_page') is not None and field['value_physical_page'] != row_page)):
        raise ValueError('explicit bounded PDF financial column geometry required')
    with _VALUATION_PDF_LOCK:
        limit = filters.ZLIB_MAX_OUTPUT_LENGTH
        filters.ZLIB_MAX_OUTPUT_LENGTH = min(limit or _PDF_MAX_PAGE_BYTES, _PDF_MAX_PAGE_BYTES)
        try:
            reader = PdfReader(io.BytesIO(raw),strict=True)
            if reader.is_encrypted or len(reader.pages)>800 or span[1]>len(reader.pages):
                raise ValueError('valuation PDF page scope unavailable')
            page_rows = {}
            for number in {header_page,row_page}:
                page = reader.pages[number-1]
                content = page.get_contents()
                if content is not None and len(content.get_data())>_PDF_MAX_PAGE_BYTES:
                    raise ValueError('valuation page content exceeds limit')
                segments, fonts, unsupported_operators = [], {}, []
                font_resources=page.get('/Resources',{}).get('/Font',{})
                text_state={'font':None,'size':None,'positioned':False}
                graphics_stack=[]
                def original_run(operands,cm,tm):
                    if (not text_state['positioned'] or len(operands)!=1
                            or text_state['font'] is None or text_state['size'] is None):
                        raise ValueError('original financial text run position unavailable')
                    font=text_state['font']
                    operand=operands[0]
                    if isinstance(operand,bytes):
                        if isinstance(font.encoding,str):
                            glyphs=operand.decode(font.encoding,errors='strict')
                        else:
                            glyphs=''.join(font.encoding[code] for code in operand)
                    elif isinstance(operand,str):
                        glyphs=operand
                    else:
                        raise ValueError('original financial glyph encoding unavailable')
                    matrix=mult(tm,cm)
                    if (len(segments)>=100000 or not font.interpretable
                            or not all(math.isfinite(float(n)) for n in matrix)
                            or matrix[0]<=0 or abs(matrix[1])>1e-6 or abs(matrix[2])>1e-6):
                        raise ValueError('financial glyph rotation or geometry ambiguous')
                    x,positions=float(matrix[4]),[]
                    for glyph in glyphs:
                        char=font.character_map.get(glyph,glyph)
                        if not isinstance(char,str) or len(char)!=1 or char in '\r\n':
                            raise ValueError('explicit original financial glyph mapping required')
                        # Advance all original glyphs, including literal leading
                        # and trailing spaces. Extractor-generated spaces are not
                        # PDF operands and cannot shift a cell into another column.
                        width=font.character_widths.get(glyph,font.character_widths['default'])
                        end=x+float(width)*float(text_state['size'])*float(matrix[0])/1000
                        if not math.isfinite(end) or end<x:
                            raise ValueError('invalid financial glyph width')
                        if not char.isspace():
                            positions.append((_compact_text(char),x,end))
                        x=end
                    if positions:
                        segments.append((float(matrix[5]),float(matrix[4]),positions))
                    # Consecutive text-show operators without a new original
                    # text position need cursor advancement, which is unsupported.
                    text_state['positioned']=False
                def operand_before(operator, operands, cm, tm):
                    # The width model below supports only default spacing.
                    # Check nested Forms too; pypdf may catch visitor exceptions
                    # there, so reject after extraction rather than in a visitor.
                    values = None
                    defaults = None
                    if operator in {b'Tc',b'Tw',b'Tz',b'Ts'}:
                        values,defaults=operands,[100 if operator==b'Tz' else 0]
                    elif operator==b'Tr':
                        if (len(operands)!=1 or float(operands[0]) not in {0,1,2}):
                            unsupported_operators.append(operator)
                    elif operator in {b'"',b"'",b'Do'}:
                        # Composite text-show and Form-resource inheritance are
                        # deliberately unsupported by this bounded glyph reader.
                        unsupported_operators.append(operator)
                    elif operator==b'TJ':
                        if len(operands)!=1 or not isinstance(operands[0],list):
                            unsupported_operators.append(operator)
                            return
                        values=[value for value in operands[0] if not isinstance(value,(str,bytes))]
                        defaults=[0]*len(values)
                    if values is not None:
                        try:
                            valid=(len(values)==len(defaults) and all(math.isfinite(float(value))
                                   and float(value)==default for value,default in zip(values,defaults)))
                        except (TypeError,ValueError,OverflowError):
                            valid=False
                        if not valid:
                            unsupported_operators.append(operator)
                    try:
                        if operator==b'q':
                            graphics_stack.append((text_state['font'],text_state['size']))
                        elif operator==b'Q':
                            text_state['font'],text_state['size']=graphics_stack.pop()
                        elif operator==b'Tf':
                            if len(operands)!=2 or operands[0] not in font_resources:
                                raise ValueError('original financial font resource unavailable')
                            key=operands[0]
                            if key not in fonts:
                                fonts[key]=Font.from_font_resource(font_resources[key])
                            size=float(operands[1])
                            if not math.isfinite(size) or size<=0:
                                raise ValueError('invalid original financial font size')
                            text_state.update(font=fonts[key],size=size)
                        elif operator==b'gs':
                            state=page.get('/Resources',{}).get('/ExtGState',{}).get(operands[0],{})
                            if '/Font' in state:
                                raise ValueError('graphics-state financial font unsupported')
                        elif operator in {b'BT',b'Tm',b'Td',b'TD',b'T*'}:
                            text_state['positioned']=True
                        elif operator==b'ET':
                            text_state['positioned']=False
                        elif operator==b'Tj':
                            original_run(operands,cm,tm)
                        elif operator==b'TJ' and not unsupported_operators:
                            # Only zero adjustments are supported. Joining the
                            # original strings preserves their actual glyph spaces.
                            parts=[value for value in operands[0] if isinstance(value,(str,bytes))]
                            if parts and all(isinstance(part,type(parts[0])) for part in parts):
                                original_run([parts[0][:0].join(parts)],cm,tm)
                            elif parts:
                                raise ValueError('mixed original financial glyph encoding unsupported')
                    except (KeyError,IndexError,TypeError,ValueError,OverflowError,UnicodeError):
                        unsupported_operators.append(operator)
                extracted=page.extract_text(visitor_operand_before=operand_before)
                if unsupported_operators:
                    raise ValueError('financial glyph text spacing or adjustment unsupported')
                if len(extracted)>_PDF_MAX_PAGE_TEXT:
                    raise ValueError('valuation page text exceeds limit')
                rows=[]
                for y,x,positions in sorted(segments,key=lambda item:(-item[0],item[1])):
                    row=next((r for r in rows if abs(r[0]-y)<=1),None)
                    if row is None:
                        row=[y,[]]
                        rows.append(row)
                    row[1].append((x,positions))
                page_rows[number]=[(y,[p for _,parts in sorted(pieces) for p in parts]) for y,pieces in rows]
        finally:
            filters.ZLIB_MAX_OUTPUT_LENGTH=limit
    headers=[r for r in page_rows[header_page]
             if ''.join(c for c,_,_ in r[1])==_compact_text(location['header_excerpt'])]
    rows=[r for r in page_rows[row_page]
          if ''.join(c for c,_,_ in r[1])==_compact_text(location['row_excerpt'])]
    header_text=_document_pages(raw,digest,{'page':header_page},'pdf')
    if (len(headers)!=1 or len(rows)!=1
            or any(_compact_text(location[k]) not in _compact_text(header_text)
                   for k in ('basis_label','unit_header'))
            or (header_page==row_page and rows[0][0]>=headers[0][0])):
        raise ValueError('financial physical header and row identity ambiguous')
    chars=headers[0][1]
    text=''.join(c for c,_,_ in chars)
    edges=[]
    for column in location['column_headers']:
        token=_compact_text(column)
        start=text.find(token)
        if start<0 or text.count(token)!=1:
            raise ValueError('financial physical date header ambiguous')
        edges.append(chars[start+len(token)-1][2])
    gaps=[b-a for a,b in zip(edges,edges[1:])]
    if not gaps or min(gaps)<=0:
        raise ValueError('financial physical column order ambiguous')
    # Map each amount using its actual rendered right edge. A wide tolerance
    # cannot turn a prior-period zero into a current-period zero.
    row_chars=[]
    for char,x,end in rows[0][1]:
        if row_chars and x-row_chars[-1][2]>2.5:
            row_chars.append((' ',row_chars[-1][2],x))
        row_chars.append((char,x,end))
    text=''.join(c for c,_,_ in row_chars)
    label_pattern=r'\s*'.join(re.escape(ch) for ch in _compact_text(location['label']))
    label_match=re.search(label_pattern,text)
    if label_match is None:
        raise ValueError('financial physical label unavailable')
    start=label_match.end()
    note=location.get('note_column')
    if note is not None:
        note_pattern=r'^\s*'+r'\s*'.join(re.escape(ch) for ch in _compact_text(note['cell_excerpt']))+r'(?=\s|$)'
        note_match=re.match(note_pattern,text[start:])
        if note_match is None:
            raise ValueError('financial physical note cell unavailable')
        start+=note_match.end()
    amounts={}
    for match in re.finditer(r'\(?-?\d[\d,]*(?:\.\d+)?\)?',text[start:]):
        end_index=start+match.end()-1
        edge=row_chars[end_index][2]
        index=min(range(len(edges)),key=lambda n:abs(edges[n]-edge))
        if abs(edges[index]-edge)>min(gaps)/4 or index in amounts:
            raise ValueError('financial physical amount column ambiguous')
        values=_page_amounts(match.group())
        if len(values)!=1:
            raise ValueError('financial physical amount invalid')
        amounts[index]=values[0]
    if location['column_index'] not in amounts:
        raise ValueError('selected financial physical column is blank')
    return amounts[location['column_index']]


def _validate_page_value(raw, digest, field, document_format, *, subtraction=False):
    text = _document_pages(raw, digest, field, document_format)
    if not str(field.get('excerpt', '')).strip():
        raise ValueError('dated page extraction required')
    location = field.get('location')
    row_text = text
    if location is not None:
        label = str(location.get('label', '')).strip()
        row_text = str(location.get('row_excerpt', '')).strip()
        headers = location.get('column_headers', [])
        header_row = str(location.get('header_excerpt', '')).strip()
        index = location.get('column_index')
        compact = _compact_text(text)
        if (not label or not row_text or _compact_text(label) not in _compact_text(row_text)
                or _compact_text(row_text) not in compact or not header_row
                or _compact_text(header_row) not in compact or not isinstance(headers, list)
                or not headers or any(not str(h).strip() for h in headers)
                or isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(headers)
                or location.get('column_header') != headers[index]):
            raise ValueError('financial row and period column binding missing or mismatched')
        positions = [_compact_text(header_row).find(_compact_text(h)) for h in headers]
        if any(p < 0 for p in positions) or positions != sorted(set(positions)):
            raise ValueError('financial column order unproved')
        for anchor in ('basis_label', 'unit_header', 'period_header'):
            if not str(location.get(anchor, '')).strip() or _compact_text(location[anchor]) not in compact:
                raise ValueError('financial basis, unit or period anchor absent from cited pages')
        # Match the actual label across PDF line breaks; never substitute a
        # canonical label or interpret a note number as a financial column.
        label_pattern = r'\s*'.join(re.escape(ch) for ch in label if not ch.isspace())
        label_match = re.search(label_pattern, row_text)
        if label_match is None:
            raise ValueError('actual financial row label binding missing')
        row_text = row_text[label_match.end():]
        note = location.get('note_column')
        if note is not None:
            if not isinstance(note, dict):
                raise ValueError('explicit financial note column binding required')
            note_header, cell = str(note.get('header', '')).strip(), str(note.get('cell_excerpt', '')).strip()
            note_position = _compact_text(header_row).find(_compact_text(note_header))
            if (note_header != '附注' or not cell or not re.search(r'\d', cell)
                    or not 0 <= note_position < positions[0]):
                raise ValueError('financial note header or exact cell binding mismatch')
            note_pattern = r'^\s*' + r'\s*'.join(re.escape(ch) for ch in cell if not ch.isspace()) + r'(?=\s|$)'
            note_match = re.match(note_pattern, row_text)
            if note_match is None:
                raise ValueError('financial note cell absent after actual row label')
            row_text = row_text[note_match.end():]
    value = field.get('value')
    if value is None:
        # An empty cell remains unknown. Separate absence reviews own any zero.
        return
    try:
        wanted = Decimal(str(value))
        if not wanted.is_finite() or isinstance(value, bool):
            raise InvalidOperation
        amounts = _page_amounts(row_text)
        if location is not None:
            if len(amounts) != len(headers):
                if document_format != 'pdf' or not location.get('pdf_column_layout'):
                    raise ValueError('financial numeric columns incomplete or ambiguous')
                amounts = [_pdf_financial_column_amount(raw,digest,field)]
            else:
                amounts = [amounts[location['column_index']]]
    except (InvalidOperation, ValueError, TypeError, IndexError) as exc:
        raise ValueError('valuation extraction amount invalid') from exc
    if not any(abs(amount-wanted) < Decimal('0.000001') or
               (subtraction and abs(amount+wanted) < Decimal('0.000001')) for amount in amounts):
        raise ValueError('valuation amount absent from cited physical page range')


def _official_field_binding(source, field, item):
    """Check declarations against each other and the cited row, not financial truth."""
    extracted = source.get('fields', {}).get(field, {})
    unit = extracted.get('unit') or source.get('extraction_review', {}).get('unit')
    declared = source.get('extraction_review', {}).get('unit')
    if (unit != _VALUATION_UNITS[field] or item.get('unit') != unit
            or (declared is not None and declared != unit)):
        raise ValueError('official source and valuation input unit mismatch')
    if extracted.get('semantic', field) != field:
        raise ValueError('official source field semantic mismatch')
    if field in {'equity', 'other_equity', 'parent_profit', 'ordinary_profit', 'other_equity_profit_distribution'}:
        location = extracted.get('location', {})
        statement = source.get('statement', {})
        label = _compact_text(location.get('label', ''))
        if (not location or not any(label.startswith(_compact_text(s)) for s in _FINANCIAL_LABELS[field])
                or location.get('basis') != 'consolidated'
                or statement.get('basis') != 'consolidated'
                or _iso(location.get('period_end', '')) != _iso(statement.get('period', ''))
                or _column_period(location.get('column_header', ''), statement['period']) != _iso(statement['period'])):
            raise ValueError('explicit consolidated financial field and period column required')
        if _compact_text(location.get('unit_header', '')).replace('：', ':') not in {
                '单位:元', '单位:人民币元', 'yuan'}:
            raise ValueError('financial column yuan unit anchor required')
    return extracted


def _iso(value: str | date) -> str:
    raw = "".join(ch for ch in str(value) if ch.isdigit())
    if len(raw) >= 8:
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    raise ValueError(f"invalid date: {value}")


def _ymd(value: str | date) -> str:
    return _iso(value).replace("-", "")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def derive_earnings_valuation(total_mv, earnings, *, ts_code, trade_date):
    """Static annual and rolling twelve-month ordinary profit are separate facts.

    The caller must bind each component to reviewed original documents. This
    arithmetic function cannot turn an annualized interim result into TTM PE.
    """
    day = date.fromisoformat(_iso(trade_date))
    result = {'values': {}, 'field_status': {}, 'earnings_evidence': {}, 'missing_inputs': {}}
    for mode, field in (('static', 'pe'), ('ttm', 'pe_ttm')):
        proof = earnings.get(mode, {})
        failures = []
        periods = proof.get('periods', [])
        annual_ttm = mode == 'ttm' and str(proof.get('selected_period', '')).endswith('-12-31')
        expected_count = 1 if mode == 'static' or annual_ttm else 3
        if (proof.get('definition') != 'ordinary_shareholder_profit_v1'
                or proof.get('qualified') is not True or proof.get('ts_code') != ts_code
                or proof.get('trade_date') != day.isoformat() or len(periods) != expected_count):
            failures.append('reviewed_ordinary_profit_and_revision_inventory_required')
        values = []
        spans = []
        for component in periods:
            try:
                start, end = (date.fromisoformat(component[key]) for key in ('period_start', 'period_end'))
                parent, allocation, ordinary = (_num(component.get(key)) for key in
                    ('parent_profit', 'other_equity_profit_distribution', 'ordinary_profit'))
                has_direct = ordinary is not None and math.isfinite(ordinary)
                has_subtraction = (parent is not None and allocation is not None
                    and math.isfinite(parent) and math.isfinite(allocation) and allocation >= 0)
                if (start.month != 1 or start.day != 1 or start > end or end >= day
                        or not (has_direct or has_subtraction)):
                    raise ValueError('incomplete income component')
                calculated = parent-allocation if has_subtraction else ordinary
                if not math.isfinite(calculated) or (has_direct and has_subtraction
                        and not math.isclose(calculated, ordinary, rel_tol=0, abs_tol=0.01)):
                    raise ValueError('ordinary profit paths disagree or overflow')
                # Parent profit may accompany a direct ordinary disclosure as
                # an audit fact. It does not manufacture a distribution input.
                if (('ordinary_profit' in component and not has_direct)
                        or ('parent_profit' in component and (parent is None or not math.isfinite(parent)))
                        or ('other_equity_profit_distribution' in component and not has_subtraction)):
                    raise ValueError('incomplete alternative ordinary profit path')
                values.append(ordinary if has_direct else calculated)
                spans.append((start, end))
            except (KeyError, TypeError, ValueError):
                failures.append('dated_profit_or_other_equity_distribution_missing')
        if len(spans) == expected_count:
            annual = spans[0]
            if annual[1].month != 12 or annual[1].day != 31 or annual[0].year != annual[1].year:
                failures.append('full_annual_profit_required')
            if mode == 'static' and (proof.get('latest_applicable_period') != annual[1].isoformat()
                    or annual[1].year not in {day.year-1, day.year-2}):
                failures.append('latest_closed_annual_period_required')
            if annual_ttm and (proof.get('latest_applicable_period') != annual[1].isoformat()
                    or annual[1].year not in {day.year-1, day.year-2}):
                failures.append('latest_applicable_annual_ttm_required')
            if mode == 'ttm' and not annual_ttm:
                current, prior = spans[1:]
                if (current[1].year != annual[1].year+1 or current[0].year != current[1].year
                        or prior[0].year != annual[1].year or prior[1].year != annual[1].year
                        or (current[1].month, current[1].day) != (prior[1].month, prior[1].day)
                        or (current[1].month, current[1].day) not in {(3,31),(6,30),(9,30)}
                        or proof.get('selected_period') != current[1].isoformat()
                        or proof.get('latest_applicable_period') != current[1].isoformat()
                        or (day-current[1]).days > 366):
                    failures.append('matched_annual_and_current_prior_ytd_required')
        if _num(total_mv) is None or not math.isfinite(total_mv) or total_mv <= 0:
            failures.append('same_session_market_value_required')
        result['earnings_evidence'][mode] = {'periods': periods,
            'definition': proof.get('definition'), 'revision_inventory': proof.get('revision_inventory')}
        if failures:
            result['field_status'][field] = 'unknown'
            result['missing_inputs'][field] = sorted(set(failures))
            continue
        profit = values[0] if expected_count == 1 else values[0] + values[1] - values[2]
        if not math.isfinite(profit):
            result['field_status'][field] = 'unknown'
            result['missing_inputs'][field] = ['nonfinite_ordinary_profit']
            continue
        result['earnings_evidence'][mode]['ordinary_profit_yuan'] = profit
        if profit <= 0:
            result['field_status'][field] = 'not_applicable_nonpositive_ordinary_profit'
        else:
            value = total_mv*10000/profit
            if math.isfinite(value) and value > 0:
                result['values'][field] = value
                result['field_status'][field] = 'derived_static_annual' if mode == 'static' else 'derived_ttm'
            else:
                result['field_status'][field] = 'unknown'
                result['missing_inputs'][field] = ['nonfinite_earnings_ratio']
    return result


def derive_valuation(ts_code, trade_date, inputs, *, observed_at):
    """Separate calculated values from dated, compatible valuation evidence.

    Inputs carry original receipt hashes and clocks. This never overwrites a
    provider row or certifies daily_basic; consumers must retain the provenance.
    Negative adjusted equity yields a signed PB with a risk status, not zero.
    """
    day = date.fromisoformat(_iso(trade_date))
    cutoff = datetime.fromisoformat(str(observed_at))
    if cutoff.tzinfo is None:
        raise ValueError('explicit observation timezone required')
    from zoneinfo import ZoneInfo
    market_zone = ZoneInfo('Asia/Shanghai')
    if day > cutoff.astimezone(market_zone).date():
        raise ValueError('future valuation session')
    units = {'price': 'yuan', 'total_share': '10000_shares', 'float_share': '10000_shares',
             'equity': 'yuan', 'other_equity': 'yuan', 'total_mv': '10000_yuan', 'circ_mv': '10000_yuan'}
    semantics = {'price': {'official_close', 'suspension_reference'},
                 'total_share': {'total_shares'}, 'float_share': {'circulating_shares'},
                 'equity': {'parent_equity'}, 'other_equity': {'other_equity_tools'},
                 'total_mv': {'total_market_value'}, 'circ_mv': {'circulating_market_value'}}
    values, failures, clocks, available_clocks, receipts = {}, {}, [], [], {}
    result = {'ts_code': ts_code, 'trade_date': day.isoformat(), 'values': {},
              'field_status': {}, 'missing_inputs': failures, 'certifies_daily_basic': False,
              'observation_at': cutoff.isoformat(), 'input_receipts': receipts}
    native_market_values = all(f in inputs for f in ('total_mv', 'circ_mv'))
    required = (('total_mv', 'circ_mv', 'equity', 'other_equity') if native_market_values
                else ('price', 'total_share', 'float_share', 'equity', 'other_equity'))
    for field in required:
        item = inputs.get(field, {})
        value = _num(item.get('value'))
        reasons = []
        if value is None or not math.isfinite(value):
            reasons.append('missing_or_nonfinite')
        if item.get('ts_code') != ts_code or item.get('unit') != units[field]:
            reasons.append('identity_or_unit')
        if item.get('semantic') not in semantics[field]:
            reasons.append('definition_unverified')
        digest = item.get('receipt_sha256', '')
        if (not isinstance(digest, str) or len(digest) != 64
                or any(ch not in '0123456789abcdef' for ch in digest)):
            reasons.append('receipt_missing')
        try:
            source_day = date.fromisoformat(item['source_date'])
            through = date.fromisoformat(item['valid_through'])
            available = datetime.fromisoformat(item['available_at'])
            received = datetime.fromisoformat(item['received_at'])
            if (available.tzinfo is None or received.tzinfo is None
                    or not source_day <= day <= through
                    or available > cutoff or received > cutoff
                    or available.astimezone(market_zone).date() > day):
                reasons.append('input_not_available_at_observation')
            if field in {'price', 'total_share', 'float_share', 'total_mv', 'circ_mv'} and source_day != day:
                if not (field == 'price' and item.get('semantic') == 'suspension_reference'
                        and item.get('price_date') == source_day.isoformat()):
                    reasons.append('same_session_input_required')
            if not reasons:
                clocks.append(received)
                available_clocks.append(available)
        except (KeyError, TypeError, ValueError):
            reasons.append('input_time_missing')
        if reasons:
            failures[field] = sorted(set(reasons))
        else:
            values[field] = value
            receipts[field] = digest
    price = inputs.get('price', {})
    if not native_market_values and price.get('semantic') == 'suspension_reference':
        # Reuse of yesterday's price is an explicit valuation policy, not a
        # fabricated same-day trade. Both dated suspension and action review
        # must be supported; the original price date is retained.
        for field, expected in (('suspension', 'full_day_suspended'),
                                ('corporate_actions', 'reference_price_applicable')):
            proof = inputs.get(field, {})
            digest = proof.get('receipt_sha256', '')
            if (proof.get('ts_code') != ts_code or proof.get('trade_date') != day.isoformat()
                    or proof.get('status') != expected or not isinstance(digest, str)
                    or len(digest) != 64 or any(ch not in '0123456789abcdef' for ch in digest)):
                failures[field] = ['dated_evidence_required']
            else:
                receipts[field] = digest
            try:
                proof_at = datetime.fromisoformat(proof['received_at'])
                if proof_at.tzinfo is None or proof_at > cutoff:
                    raise ValueError('proof after observation')
                clocks.append(proof_at)
            except (KeyError, TypeError, ValueError):
                failures[field] = ['proof_time_missing_or_future']
        try:
            if date.fromisoformat(price['price_date']) > day:
                raise ValueError('future reference price')
            result['price_date'] = price['price_date']
        except (KeyError, TypeError, ValueError):
            failures['price_date'] = ['original_price_date_required']
        if any(f in failures for f in ('suspension', 'corporate_actions', 'price_date')):
            values.pop('price', None)
    if 'price' in values and values['price'] <= 0:
        failures['price'] = ['nonpositive_price']
        values.pop('price')
    for field in ('total_share', 'float_share'):
        if field in values and values[field] <= 0:
            failures[field] = ['nonpositive_shares']
            values.pop(field)
    if ('total_share' in values and 'float_share' in values
            and values['float_share'] > values['total_share']):
        failures['float_share'] = ['exceeds_total_shares']
        values.pop('float_share')
    for target, share in (('total_mv', 'total_share'), ('circ_mv', 'float_share')):
        if 'price' in values and share in values:
            value = values['price'] * values[share]  # yuan * 10000 shares -> 10000 yuan
            if math.isfinite(value):
                result['values'][target] = value
                result['field_status'][target] = 'derived'
    if native_market_values:
        for field in ('total_mv', 'circ_mv'):
            if field in values and values[field] > 0:
                result['values'][field] = values[field]
                result['field_status'][field] = 'observed'
            elif field in values:
                failures[field] = ['nonpositive_market_value']
        if all(f in result['values'] for f in ('total_mv', 'circ_mv')):
            if result['values']['circ_mv'] > result['values']['total_mv']:
                failures['circ_mv'] = ['exceeds_total_market_value']
                result['values'].pop('circ_mv')
    if all(f in values for f in ('equity', 'other_equity')):
        if any(inputs['equity'].get(f) != inputs['other_equity'].get(f)
               for f in ('source_date',)) or (
                inputs['equity'].get('receipt_sha256') != inputs['other_equity'].get('receipt_sha256')
                and (not inputs['equity'].get('statement_identity') or
                     inputs['equity'].get('statement_identity') != inputs['other_equity'].get('statement_identity'))):
            failures['equity'] = ['inconsistent_financial_statement']
            values.pop('equity')
    if all(f in values for f in ('equity', 'other_equity')) and 'total_mv' in result['values']:
        denominator = values['equity'] - values['other_equity']
        if values['other_equity'] < 0 or not math.isfinite(denominator):
            failures['other_equity'] = ['invalid_adjusted_equity']
        elif denominator == 0:
            result['field_status']['pb'] = 'undefined_zero_adjusted_equity'
        else:
            pb = result['values']['total_mv'] * 10000 / denominator
            if math.isfinite(pb):
                result['values']['pb'] = pb
                result['field_status']['pb'] = ('negative_adjusted_equity' if denominator < 0 else 'derived')
    for field in ('pb', 'total_mv', 'circ_mv', 'pe'):
        result['field_status'].setdefault(field, 'unknown')
    result['value_screen_eligible'] = result['field_status']['pb'] == 'derived'
    result['valuation_risk'] = ('negative_equity' if result['field_status']['pb'] == 'negative_adjusted_equity'
                               else 'undefined_pb' if result['field_status']['pb'] == 'undefined_zero_adjusted_equity'
                               else None)
    result['input_received_at_min'] = min(clocks).isoformat() if clocks else None
    result['input_received_at_max'] = max(clocks).isoformat() if clocks else None
    result['input_available_at_min'] = min(available_clocks).isoformat() if available_clocks else None
    result['input_available_at_max'] = max(available_clocks).isoformat() if available_clocks else None
    result['status'] = ('derived_core_fields' if all(f in result['values'] for f in ('pb', 'total_mv', 'circ_mv'))
                        and not failures else 'incomplete')
    return result


def _read_valuation_document_original(payload):
    """Recheck immutable original bytes, independently of an extraction."""
    from urllib.parse import urlparse
    document = payload['document']
    url = urlparse(document['url'])
    catalogue = payload.get('kind') == 'disclosure_inventory'
    official_catalogue = (catalogue and url.hostname == 'www.cninfo.com.cn'
        and url.path == '/new/hisAnnouncement/query' and document.get('format') == 'json')
    if (url.scheme != 'https' or (not official_catalogue and url.hostname not in
            {'static.cninfo.com.cn','disc.static.szse.cn','www.sse.com.cn','static.sse.com.cn'})):
        raise ValueError('official valuation document origin required')
    path = Path(document['path'])
    if path.stat().st_size > _PDF_MAX_BYTES:
        raise ValueError('valuation document exceeds size limit')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != document['sha256']:
        raise ValueError('official valuation document changed')
    if document.get('format') == 'pdf' and not raw.startswith(b'%PDF-'):
        raise ValueError('official valuation PDF format mismatch')
    if not payload.get('ts_code') or not payload.get('as_of'):
        raise ValueError('official document dated identity required')
    return raw, catalogue, official_catalogue


def _validate_valuation_document(payload):
    """Check official original and every declared extraction, not review truth."""
    from urllib.parse import urlparse
    raw, catalogue, official_catalogue = _read_valuation_document_original(payload)
    document = payload['document']
    for field in payload.get('fields', {}).values():
        _validate_page_value(raw, document['sha256'], field, document.get('format'))
    # A component arithmetic reconciliation also needs the actual cited values;
    # a negative treasury-stock deduction may be printed as a positive subtotal.
    for field in payload.get('other_equity_absence_review', {}).get('equity_components', []):
        _validate_page_value(raw, document['sha256'], field, document.get('format'),
                             subtraction=field.get('name') == 'treasury_stock')
    related = payload.get('related_original_document_receipts', [])
    if not isinstance(related, list) or (payload.get('coverage') ==
            'all_price_and_share_affecting_actions' and not related):
        raise ValueError('original corporate action documents required')
    for source in related:
        origin = urlparse(source['url'])
        source_path = Path(source['path'])
        if source_path.stat().st_size > _PDF_MAX_BYTES:
            raise ValueError('corporate action document exceeds size limit')
        source_raw = source_path.read_bytes()
        if (origin.scheme != 'https' or origin.hostname not in
                {'static.cninfo.com.cn','disc.static.szse.cn','www.sse.com.cn','static.sse.com.cn'}
                or source.get('code') != payload['ts_code']
                or hashlib.sha256(source_raw).hexdigest() != source['sha256']
                or not source_raw.startswith(b'%PDF-')
                or source.get('announcement',{}).get('secCode') != payload['ts_code'].split('.')[0]):
            raise ValueError('original corporate action document changed or mismatched')
        received = datetime.fromisoformat(source.get('received_at', ''))
        parent_received = datetime.fromisoformat(payload['received_at'])
        if received.tzinfo is None or parent_received.tzinfo is None or received > parent_received:
            raise ValueError('corporate action arrival omitted from input clock')
    if catalogue:
        # The manifest is a local index of original public responses, not an
        # official response itself. Revalidate every page on every read; a
        # boolean review or the supplier's totalpages cannot establish coverage.
        if not official_catalogue or payload.get('fields'):
            raise ValueError('official disclosure catalogue page manifest required')
        manifest = json.loads(raw)
        pages = manifest.get('source_pages', [])
        if (manifest.get('schema') != 'official_disclosure_page_set_v1' or not pages
                or manifest.get('ts_code') != payload['ts_code']):
            raise ValueError('dated disclosure page scope required')
        identities, total, org = [], None, None
        for number, page in enumerate(pages, 1):
            params = page['params']
            stock, org_id = params['stock'].split(',')
            start, through = params['seDate'].split('~')
            expected_column = {'SZ':'szse','SH':'sse'}.get(payload['ts_code'].split('.')[1])
            if (stock != payload['ts_code'].split('.')[0] or not org_id
                    or (org is not None and org != org_id)
                    or params.get('tabName') != 'fulltext' or int(params['pageNum']) != number
                    or start != payload['window_from'] or through != payload['window_through']
                    or through != payload['as_of'] or params.get('category') or params.get('searchkey')
                    or params.get('column') not in (None,'',expected_column)
                    or set(params) - {'stock','tabName','column','pageSize','pageNum','seDate','isHLtitle'}
                    or page.get('http_status') != 200):
                raise ValueError('disclosure catalogue query scope mismatch')
            org = org_id
            page_raw = Path(page['path']).read_bytes()
            if hashlib.sha256(page_raw).hexdigest() != page['sha256']:
                raise ValueError('official disclosure catalogue page changed')
            body = json.loads(page_raw)
            declared = body['totalAnnouncement']
            rows = body.get('announcements') or []
            if (isinstance(declared, bool) or not isinstance(declared, int) or declared < 0
                    or (total is not None and total != declared)
                    or body.get('hasMore') is not (number < len(pages))
                    or any(r.get('secCode') != stock or r.get('orgId') != org
                           or not r.get('announcementId') for r in rows)):
                raise ValueError('disclosure catalogue pagination or identity mismatch')
            total = declared
            identities.extend(r['announcementId'] for r in rows)
        if len(identities) != total or len(set(identities)) != total:
            raise ValueError('disclosure catalogue incomplete or duplicate pages')
    return payload


def _verified_financial_extraction_replacements(proofs, documents, code, day):
    """Accept a reviewed same-PDF extraction correction, never an issuer revision.

    Both wrappers must already be hash-bound observations in this review. The
    superseded wrapper still supplies its original identity and arrival clock;
    only its erroneous numeric extraction is replaced by a strictly checked one.
    """
    if not isinstance(proofs, list):
        raise ValueError('financial extraction replacements must be an explicit list')
    replacements = {}
    financial = set(_FINANCIAL_LABELS)
    for proof in proofs:
        if not isinstance(proof, dict) or proof.get('schema') != 'same_document_extraction_replacement_v1':
            raise ValueError('explicit same-document extraction replacement schema required')
        old_hash, new_hash = proof.get('superseded_receipt_sha256'), proof.get('replacement_receipt_sha256')
        if (any(not isinstance(h, str) or not re.fullmatch('[0-9a-f]{64}', h) for h in (old_hash,new_hash))
                or old_hash == new_hash or old_hash in replacements
                or old_hash not in documents or new_hash not in documents or not str(proof.get('reason','')).strip()):
            raise ValueError('financial extraction replacement hash bindings missing or ambiguous')
        old, new = documents[old_hash][0], documents[new_hash][0]
        for source in (old,new):
            _read_valuation_document_original(source)
            statement, fields = source.get('statement', {}), source.get('fields', {})
            if (source.get('schema') != 'official_valuation_document_v1' or source.get('kind') is not None
                    or source.get('ts_code') != code or proof.get('ts_code') != code
                    or source['document'].get('format') != 'pdf'
                    or source['document'].get('sha256') != proof.get('document_sha256')
                    or statement.get('basis') != 'consolidated' or proof.get('basis') != 'consolidated'
                    or _iso(statement.get('period','')) != _iso(proof.get('period',''))
                    or _iso(statement.get('announcement_date','')) != _iso(proof.get('announcement_date',''))
                    or not fields or set(fields) - financial
                    or not _iso(statement.get('period','')) <= _iso(statement.get('announcement_date','')) <= day):
                raise ValueError('financial extraction replacement original identity or statement mismatch')
        if (old['statement'].get('period_start') != new['statement'].get('period_start')
                or not set(old['fields']).issubset(new['fields'])):
            raise ValueError('replacement cannot omit a superseded financial field or period')
        _validate_valuation_document(new)
        for name in new['fields']:
            _official_field_binding(new, name, {'unit':'yuan'})
        replacements[old_hash] = new_hash
    if set(replacements).intersection(replacements.values()):
        raise ValueError('financial extraction replacement chains or cycles forbidden')
    return replacements


def _verified_native_balance_date_conflicts(proofs, documents, code, day):
    """Bind a relay-only announcement-date conflict to an actual official report.

    This does not change the raw rows or their clocks and is never an income
    revision exemption. Complete retained rows, not selected equal amounts,
    must agree except for their explicit announcement/update metadata.
    """
    from urllib.parse import urlparse
    from zoneinfo import ZoneInfo
    if not isinstance(proofs,list):
        raise ValueError('native balance date conflicts must be an explicit list')
    reconciled={}
    for proof in proofs:
        if (not isinstance(proof,dict) or proof.get('schema')!='native_balance_disclosure_date_conflict_v1'
                or proof.get('ts_code')!=code or not str(proof.get('reason','')).strip()):
            raise ValueError('explicit reviewed native balance date conflict required')
        hashes=[proof.get(k) for k in ('native_receipt_sha256','official_statement_receipt_sha256',
                                     'official_catalogue_receipt_sha256')]
        if any(not isinstance(h,str) or not re.fullmatch('[0-9a-f]{64}',h) or h not in documents for h in hashes):
            raise ValueError('native disclosure date conflict receipt bindings missing')
        native,statement,catalogue=[documents[h][0] for h in hashes]
        rows=native.get('rows',[])
        indices=[proof.get(k) for k in ('earlier_row_index','later_row_index')]
        if (native.get('api')!='balancesheet' or native.get('error_type') or indices[0]==indices[1]
                or any(isinstance(i,bool) or not isinstance(i,int) or not 0<=i<len(rows) for i in indices)):
            raise ValueError('native disclosure date conflict exact original rows required')
        earlier,later=[rows[i] for i in indices]
        official_day=_iso(proof.get('official_announcement_date',''))
        period=_iso(proof.get('period',''))
        earlier_day,later_day=[_iso(r.get('f_ann_date') or r.get('ann_date','')) for r in (earlier,later)]
        metadata={'ann_date','f_ann_date','update_flag'}
        required={'ts_code','end_date','report_type','total_hldr_eqy_exc_min_int','oth_eqt_tools',*metadata}
        if (set(earlier)!=set(later) or not required<=set(earlier)
                or any(earlier[k]!=later[k] for k in set(earlier)-metadata)
                or any(r.get('ts_code')!=code or _iso(r.get('end_date',''))!=period
                       or str(r.get('report_type')) not in {'1','4'}
                       or _iso(r.get('ann_date',''))!=_iso(r.get('f_ann_date','')) for r in (earlier,later))
                or not period<=earlier_day==official_day<later_day<=day):
            raise ValueError('native disclosure conflict differs in financial or statement fields')
        _validate_valuation_document(statement)
        _validate_valuation_document(catalogue)
        identity=statement.get('statement',{})
        if (statement.get('schema')!='official_valuation_document_v1' or statement.get('ts_code')!=code
                or statement.get('document',{}).get('format')!='pdf' or identity.get('basis')!='consolidated'
                or _iso(identity.get('period',''))!=period
                or _iso(identity.get('announcement_date',''))!=official_day
                or catalogue.get('kind')!='disclosure_inventory' or catalogue.get('ts_code')!=code
                or catalogue.get('catalogue_complete') is not True or catalogue.get('as_of')!=day
                or catalogue.get('window_from','9999-12-31')>period
                or catalogue.get('window_through','')!=day):
            raise ValueError('official statement and complete disclosure conflict scope required')
        for name,native_name in (('equity','total_hldr_eqy_exc_min_int'),('other_equity','oth_eqt_tools')):
            native_value=earlier.get(native_name)
            if name=='other_equity' and native_value is None:
                continue  # A raw null supplies no numeric fact or absence zero.
            actual=_official_field_binding(statement,name,{'unit':'yuan'}).get('value')
            if (isinstance(native_value,bool) or isinstance(actual,bool) or _num(native_value) is None
                    or _num(actual) is None or abs(Decimal(str(native_value))-Decimal(str(actual)))>Decimal('0.01')):
                raise ValueError('native disclosure date conflict amount differs from official statement')
        manifest=json.loads(Path(catalogue['document']['path']).read_text(encoding='utf-8'))
        matched=[]
        official_path=urlparse(statement['document']['url']).path.lstrip('/')
        for page in manifest['source_pages']:
            for announcement in json.loads(Path(page['path']).read_text(encoding='utf-8')).get('announcements') or []:
                stamp=announcement.get('announcementTime')
                if isinstance(stamp,(int,float)) and not isinstance(stamp,bool) and math.isfinite(stamp):
                    announced=datetime.fromtimestamp(stamp/1000,timezone.utc).astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()
                else:
                    announced=_iso(announcement.get('announcement_date') or stamp or '')
                title=str(announcement.get('announcementTitle',''))
                url_path=urlparse(str(announcement.get('adjunctUrl',''))).path.lstrip('/')
                if announced==official_day and url_path==official_path and '报告' in title and '摘要' not in title:
                    matched.append(announcement['announcementId'])
                # Conservative: any correction in the disputed interval, or
                # another report for the selected financial year, still blocks.
                correction=any(token in title for token in ('更正','修订','差错','重述','补充'))
                report=str(date.fromisoformat(period).year) in title and '报告' in title
                if official_day<announced<=later_day and (correction or report):
                    raise ValueError('official catalogue contains possible disputed-date revision')
        if len(matched)!=1 or (hashes[0],indices[1]) in reconciled:
            raise ValueError('official native-date report identity absent or ambiguous')
        reconciled[hashes[0],indices[1]]=official_day
    return reconciled




class RetainedValuationReader:
    """Read-only financial evidence authority shared with the collector.

    A caller-owned connection is borrowed. No schema initialization, provider
    client, acquisition, transaction or business write is exposed here.
    """
    def __init__(self, connection, *, provider='xiaodefa'):
        self._reader_connection = connection
        self._reader_provider = provider
        for table, columns in {
                'history_fetch_checkpoint': 'dataset,trade_date,page_no,status',
                'tushare_trade_cal': 'exchange,cal_date,is_open',
                'tushare_stock_basic': 'ts_code,list_date,delist_date',
                'multi_source_observation': 'data_type,provider,payload_json,observed_at',
        }.items():
            connection.execute(f'SELECT {columns} FROM {table} LIMIT 0')

    @property
    def _valuation_con(self):
        return self._reader_connection

    @property
    def _valuation_provider(self):
        return self._reader_provider

    @staticmethod
    def _valuation_scope_error(message):
        return ValueError(message)

    def _retained_financial_sources(self, code, day, observed_at, kind, *, extraction_replacements=None,
                                    native_date_conflicts=None):
        """Only already-arrived, applicable consolidated facts may revoke a review."""
        from zoneinfo import ZoneInfo
        cutoff = datetime.fromisoformat(observed_at)
        if cutoff.tzinfo is None:
            raise ValueError('explicit revision observation timezone required')
        native = 'tushare_balancesheet' if kind == 'balance' else 'tushare_income'
        fields = {'equity', 'other_equity'} if kind == 'balance' else {
            'parent_profit', 'ordinary_profit', 'other_equity_profit_distribution'}
        rows = self._valuation_con.execute('SELECT data_type,payload_json,payload_hash,observed_at '
            'FROM multi_source_observation WHERE data_type IN (?,\'valuation_source_document\') '
            'AND observed_at<=?', [native, cutoff.astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)]).fetchall()
        for data_type, raw, digest, arrival in rows:
            if hashlib.sha256(raw.encode()).hexdigest() != digest:
                continue
            payload = json.loads(raw)
            if data_type == native:
                for row_index,row in enumerate(payload.get('rows', [])):
                    if row.get('ts_code') != code or str(row.get('report_type')) not in {'1', '4'}:
                        continue  # A parent-company or unknown-basis row is not a consolidated revision.
                    announcement = _iso(row.get('f_ann_date') or row.get('ann_date', ''))
                    period = _iso(row['end_date'])
                    native_announcement=announcement
                    if kind=='balance':
                        announcement=(native_date_conflicts or {}).get((digest,row_index),announcement)
                    if period <= announcement <= day:
                        yield dict(digest=digest, period=period, announcement=announcement,
                                   native_announcement=native_announcement,document_sha=None, arrival=str(arrival))
            elif (payload.get('schema') == 'official_valuation_document_v1'
                    and payload.get('ts_code') == code and fields.intersection(payload.get('fields', {}))):
                statement = payload.get('statement', {})
                if statement.get('basis') != 'consolidated':
                    continue
                announcement = _iso(statement['announcement_date'])
                period = _iso(statement['period'])
                if period <= announcement <= day:
                    if digest in (extraction_replacements or {}):
                        _read_valuation_document_original(payload)
                    else:
                        _validate_valuation_document(payload)
                    yield dict(digest=digest, period=period, announcement=announcement,
                               document_sha=payload['document']['sha256'], arrival=str(arrival))


    def _assert_revision_inventory(self, code, day, observed_at, kind, inventory, documents, oldest, selected=(),
                                   *, extraction_replacements=None, native_date_conflicts=None):
        receipts = inventory.get('statement_receipts', [])
        # Re-extraction of one immutable PDF is not another issuer revision.
        original_files = {documents[h][0].get('document', {}).get('sha256')
            for h in receipts if h in documents}
        sources = list(self._retained_financial_sources(code, day, observed_at, kind,
                      extraction_replacements=extraction_replacements,native_date_conflicts=native_date_conflicts))
        for source in sources:
            if source['period'] >= oldest and source['digest'] not in receipts:
                if source['document_sha'] and source['document_sha'] in original_files:
                    continue
                raise ValueError('financial revision inventory omits retained applicable consolidated statement')
        for period, announcement in selected:
            if any(s['period'] == period and s['announcement'] > announcement for s in sources):
                raise ValueError('financial inputs superseded by retained consolidated revision')
        if kind == 'balance' and selected and any(s['period'] > max(p for p, _ in selected) for s in sources):
            raise ValueError('latest known applicable financial period not selected')


    @staticmethod
    def _latest_income_period(catalogues, day, mode):
        """Derive reported periods from the hash-checked, complete original catalogue."""
        from zoneinfo import ZoneInfo
        periods = []
        for catalogue in catalogues:
            body = json.loads(Path(catalogue['document']['path']).read_text(encoding='utf-8'))
            pages = [body]
            if body.get('schema') == 'official_disclosure_page_set_v1':
                pages = [json.loads(Path(p['path']).read_text(encoding='utf-8'))
                         for p in body['source_pages']]
            for page in pages:
                for row in page.get('announcements', []):
                    title = str(row.get('announcementTitle', ''))
                    year = re.search(r'((?:19|20)\d{2})\s*年?', title)
                    if not year:
                        continue
                    announcement = row.get('announcementTime')
                    if isinstance(announcement, (int, float)) and not isinstance(announcement, bool):
                        if not math.isfinite(announcement):
                            raise ValueError('disclosure announcement time invalid')
                        announced = datetime.fromtimestamp(announcement / 1000, timezone.utc).astimezone(
                            ZoneInfo('Asia/Shanghai')).date().isoformat()
                    else:
                        announced = _iso(row.get('announcement_date') or announcement or '')
                    if announced > day:
                        continue
                    if '半年度报告' in title:
                        end = '06-30'
                    elif '第一季度报告' in title or '一季度报告' in title:
                        end = '03-31'
                    elif '第三季度报告' in title or '三季度报告' in title:
                        end = '09-30'
                    elif '年度报告' in title:
                        end = '12-31'
                    else:
                        continue
                    if mode == 'static' and end != '12-31':
                        continue
                    period = f'{year[1]}-{end}'
                    if period < announced:
                        periods.append(period)
        if not periods:
            raise ValueError('latest applicable income period not evidenced by disclosure catalogue')
        return max(periods)


    def _valuation_review(self, review, trade_date, observed_at):
        """Validate an explicitly reviewed bundle against retained source bytes.

        SHA bindings establish integrity, not the truth of a review. The named
        reviewer owns the financial revision inventory and policy interpretation;
        a supplier response cannot self-authorize either. No bare qualified flag
        or unbound numeric input is accepted.
        """
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Shanghai')
        day = _iso(trade_date)
        cutoff = datetime.fromisoformat(observed_at)
        if cutoff.tzinfo is None:
            raise ValueError('explicit observation timezone required')
        if review.get('schema') not in {'reviewed_valuation_inputs_v1','reviewed_valuation_inputs_v2'} or review.get('trade_date') != day:
            raise ValueError('valuation review schema/session mismatch')
        reviewed = datetime.fromisoformat(review['reviewed_at'])
        if not str(review.get('reviewed_by', '')).strip() or reviewed.tzinfo is None or reviewed > cutoff:
            raise ValueError('dated reviewer required')
        code = review['ts_code']
        if code != stock_code_to_ts_code(code):
            raise ValueError('canonical valuation identity required')
        documents,receipt_types = {},{}
        for digest in review['source_receipts']:
            records = self._valuation_con.execute(
                'SELECT payload_json,observed_at,data_type FROM multi_source_observation WHERE payload_hash=?',
                [digest]).fetchall()
            valid = [(raw, arrival.replace(tzinfo=zone) if arrival.tzinfo is None else arrival,kind)
                     for raw, arrival,kind in records if hashlib.sha256(raw.encode()).hexdigest() == digest]
            valid = [(raw, arrival,kind) for raw, arrival,kind in valid if arrival <= reviewed]
            if not valid:
                raise ValueError('review source receipt absent, changed or received after review')
            raw, arrival,_ = min(valid, key=lambda item: item[1])
            payload = json.loads(raw)
            documents[digest] = (payload, arrival.isoformat())
            receipt_types[digest]={kind for _,_,kind in valid}
        replacements = _verified_financial_extraction_replacements(
            review.get('financial_extraction_replacements', []), documents, code, day)
        for digest, (payload, _) in documents.items():
            if payload.get('schema') == 'official_valuation_document_v1':
                if digest in replacements:
                    _read_valuation_document_original(payload)
                else:
                    _validate_valuation_document(payload)
        date_proofs=review.get('native_disclosure_date_conflicts',[])
        if not isinstance(date_proofs,list) or any('tushare_balancesheet' not in
                receipt_types.get(proof.get('native_receipt_sha256'),set())
                for proof in date_proofs if isinstance(proof,dict)):
            raise ValueError('retained native balancesheet observation required for date conflict')
        date_conflicts=_verified_native_balance_date_conflicts(date_proofs,documents,code,day)
        inventory = review['financial_inventory']
        used_receipts = set(inventory.get('statement_receipts', []))
        used_receipts.update(item.get('receipt_sha256') for item in review.get('inputs', {}).values())
        for proof in review.get('earnings_reviews', {}).values():
            used_receipts.update(proof.get('revision_inventory', {}).get('statement_receipts', []))
            for period in proof.get('periods', []):
                used_receipts.update(item.get('receipt_sha256') for item in period.get('inputs', {}).values())
        if used_receipts.intersection(replacements):
            raise ValueError('superseded extraction cannot supply a reviewed financial input')
        scopes = {'all_published_consolidated_revisions'}
        if review['schema'] == 'reviewed_valuation_inputs_v2':
            scopes.add('latest_applicable_consolidated_statement_and_corrections')
        if (inventory.get('as_of') != day or inventory.get('scope') not in scopes
                or not inventory.get('selection_reason') or not inventory.get('document_receipts')
                or any(h not in documents for h in inventory['document_receipts'])
                or inventory.get('selected_statement_receipt') not in documents
                or inventory.get('selected_statement_receipt') not in inventory.get('statement_receipts', [])
                or any(h not in documents for h in inventory.get('statement_receipts', []))):
            raise ValueError('complete dated financial revision inventory required')
        if inventory.get('scope') == 'latest_applicable_consolidated_statement_and_corrections':
            period = _iso(inventory['selected_period'])
            catalogues = [documents[h][0] for h in inventory['document_receipts']]
            if not any(p.get('schema') == 'official_valuation_document_v1'
                and p.get('kind') == 'disclosure_inventory' and p.get('ts_code') == code
                and p.get('as_of') == day and p.get('catalogue_complete') is True
                and p.get('window_from','9999-12-31') <= period
                and p.get('window_through') == day and p.get('document',{}).get('sha256')
                for p in catalogues):
                raise ValueError('selected statement and dated correction catalogue required')
        inputs = json.loads(_json(review['inputs']))
        statement_identities = {}
        for field, item in inputs.items():
            digest = item['receipt_sha256']
            if digest not in documents:
                raise ValueError('unbound valuation input')
            payload, arrival = documents[digest]
            item['received_at'] = arrival  # The bundle cannot refresh source age.
            if field in {'suspension', 'corporate_actions'}:
                if not item.get('review_basis'):
                    raise ValueError('documented suspension/action interpretation required')
                continue
            if payload.get('schema') == 'official_valuation_document_v1':
                if payload['ts_code'] != code:
                    raise ValueError('official extraction identity mismatch')
                extracted = _official_field_binding(payload, field, item)
                if item.get('evidence_kind') == 'evidenced_absence_zero':
                    absence = payload.get('other_equity_absence_review', {})
                    components = absence.get('equity_components', [])
                    amounts = [_num(c.get('value')) for c in components]
                    parent_value = _num(payload.get('fields',{}).get('equity',{}).get('value'))
                    if (field != 'other_equity' or item.get('value') != 0 or extracted.get('value') is not None
                        or not absence.get('no_instrument_disclosure') or not absence.get('equity_changes_review')
                        or not components or any(v is None or not math.isfinite(v) for v in amounts)
                        or parent_value is None or abs(sum(amounts)-parent_value) > 0.01
                        or any(not c.get('page') or not c.get('excerpt') for c in components)):
                        raise ValueError('blank field is not evidenced absence zero')
                    for component in components:
                        location = component.get('location', {})
                        if (not location or location.get('basis') != 'consolidated'
                                or _iso(location.get('period_end', '')) != _iso(payload['statement']['period'])):
                            raise ValueError('absence component financial row and period binding required')
                elif (_num(extracted.get('value')) is None or
                      _num(extracted['value']) != _num(item.get('value'))):
                    raise ValueError('valuation value does not match official extraction')
                if field in {'equity','other_equity'}:
                    statement = payload['statement']
                    announcement, period = _iso(statement['announcement_date']), _iso(statement['period'])
                    if (statement.get('basis') != 'consolidated' or period > announcement
                            or announcement > day or item['source_date'] != announcement):
                        raise ValueError('official financial period or basis mismatch')
                    item['statement_identity'] = [code,period,announcement]
                    statement_identities[field] = item['statement_identity']
                elif payload.get('as_of') != day:
                    raise ValueError('official market input validity unproved')
                continue
            value = payload
            parent = None
            for part in item['value_path']:
                parent, value = value, value[part]
            if isinstance(parent, dict) and parent.get('ts_code', code) != code:
                raise ValueError('valuation source identity mismatch')
            if isinstance(value, bool) or _num(value) is None or _num(value) != _num(item.get('value')):
                raise ValueError('valuation value does not match retained source')
            if not isinstance(parent, dict) or parent.get('ts_code') != code:
                raise ValueError('valuation source row identity required')
            product = payload.get('api')
            native = _VALUATION_NATIVE_FIELDS.get(product, {}).get(field)
            if (native is None or item['value_path'][-1] != native
                    or item.get('unit') != _VALUATION_UNITS[field]
                    or (item.get('source_product') is not None and item['source_product'] != product)):
                raise ValueError('valuation native product, field or unit mapping mismatch')
            if field in {'equity', 'other_equity'}:
                announcement = _iso(parent.get('f_ann_date') or parent['ann_date'])
                if (item['value_path'][-1] != native or str(parent.get('report_type')) not in {'1', '4'}
                        or _iso(parent['end_date']) > announcement or announcement > day
                        or item['source_date'] != announcement):
                    raise ValueError('financial period, announcement or field mapping mismatch')
                item['statement_identity'] = [code,_iso(parent['end_date']),announcement]
                statement_identities[field] = item['statement_identity']
            else:
                if not parent.get('trade_date'):
                    raise ValueError('valuation source session required')
                source_day = _iso(parent['trade_date'])
                expected_day = item.get('price_date') if item.get('semantic') == 'suspension_reference' else day
                if source_day != expected_day:
                    raise ValueError('valuation source session mismatch')
        statement = inventory['selected_statement_receipt']
        if (inventory.get('scope') == 'latest_applicable_consolidated_statement_and_corrections'
            and (statement_identities.get('equity', [None,None])[1] != _iso(inventory['selected_period'])
                 or statement_identities.get('other_equity', [None,None])[1] != _iso(inventory['selected_period']))):
            raise ValueError('selected financial period differs from valued inputs')
        if any(inputs.get(f, {}).get('receipt_sha256') != statement for f in ('equity', 'other_equity')):
            if (review['schema'] != 'reviewed_valuation_inputs_v2'
                or statement_identities.get('equity') != statement_identities.get('other_equity')
                or any(inputs.get(f,{}).get('receipt_sha256') not in inventory['statement_receipts']
                       for f in ('equity','other_equity'))):
                raise ValueError('financial inputs differ from reviewed statement')
        # Later known originals revoke qualification; future arrivals do not
        # rewrite what was known at a historical observation.
        oldest = (_iso(inventory['selected_period']) if inventory.get('selected_period')
                  else min(identity[1] for identity in statement_identities.values()))
        self._assert_revision_inventory(code, day, observed_at, 'balance', inventory, documents, oldest,
                                        selected=[identity[1:] for identity in statement_identities.values()],
                                        extraction_replacements=replacements,native_date_conflicts=date_conflicts)
        # A same-day zero-volume vendor quote is not proof of a same-day
        # traded close. Calling it official_close cannot bypass the suspension
        # reference/corporate-action policy. Native same-day market values
        # remain a separate intake path that need not use a reference price.
        suspension = self._suspension_rows(day) or []
        full_day_halt = any(r['ts_code'] == code and r.get('suspend_type') == 'S'
            and r.get('suspend_timing') in (None, '') for r in suspension)
        if (full_day_halt and 'price' in inputs
                and inputs['price'].get('semantic') != 'suspension_reference'):
            raise ValueError('full-day suspension requires explicit reference price policy')
        # A claimed full-day suspension cannot override observed trading.
        if inputs.get('price', {}).get('semantic') == 'suspension_reference':
            if review['schema'] == 'reviewed_valuation_inputs_v2':
                action_input = inputs.get('corporate_actions', {})
                action = documents.get(action_input.get('receipt_sha256'), ({},None))[0]
                if (action.get('ts_code') != code or action.get('window_from','9999-12-31') > inputs['price']['price_date']
                    or action.get('window_through') != day
                    or action.get('coverage') != 'all_price_and_share_affecting_actions'
                    or action.get('reviewed_effect') != 'reference_price_applicable'):
                    raise ValueError('complete dated corporate action window required')
            if not any(r['ts_code'] == code and r.get('suspend_type') == 'S'
                       and r.get('suspend_timing') in (None, '') for r in suspension):
                raise ValueError('full-day suspension source missing')
            snapshot = self._valuation_con.execute(
                "SELECT payload_hash FROM multi_source_observation WHERE data_type='tushare_suspend_d_snapshot' "
                "AND status='qualified' AND provider=? AND json_extract_string(payload_json,'$.params.trade_date')=? "
                'ORDER BY observed_at DESC LIMIT 1',
                [self._valuation_provider, _ymd(day)]).fetchone()
            if not snapshot or inputs.get('suspension', {}).get('receipt_sha256') != snapshot[0]:
                raise ValueError('suspension proof does not bind qualified snapshot')
            traded = self._valuation_con.execute(
                'SELECT count(*) FROM tushare_daily WHERE date=? AND ts_code=? AND (volume>0 OR turnover>0)',
                [day, code]).fetchone()[0]
            if traded:
                raise ValueError('suspension conflicts with retained trading')
        result = derive_valuation(code, day, inputs, observed_at=observed_at)
        earnings = self._reviewed_earnings(review.get('earnings_reviews', {}), documents, code, day,
                                          observed_at=observed_at, extraction_replacements=replacements)
        earnings_result = derive_earnings_valuation(result['values'].get('total_mv'), earnings,
                                                  ts_code=code, trade_date=day)
        result['values'].update(earnings_result['values'])
        result['field_status'].update(earnings_result['field_status'])
        result['earnings_evidence'] = earnings_result['earnings_evidence']
        result['earnings_missing_inputs'] = earnings_result['missing_inputs']
        if (review.get('pb_policy') == 'signed_or_known_undefined_v1'
                and review['schema'] == 'reviewed_valuation_inputs_v2'
                and result['field_status']['pb'] == 'undefined_zero_adjusted_equity'
                and not result['missing_inputs']
                and all(f in result['values'] for f in ('total_mv','circ_mv'))):
            result['status'] = 'derived_core_fields_with_known_undefined_pb'
            result['pb_applicability'] = 'known_undefined_zero_equity'
        result['reviewed_by'] = review['reviewed_by']
        result['reviewed_at'] = review['reviewed_at']
        result['as_known_at'] = observed_at
        result['historical_recalculation'] = cutoff.astimezone(zone).date().isoformat() > day
        result['financial_inventory'] = inventory
        result['financial_extraction_replacements'] = review.get('financial_extraction_replacements', [])
        result['native_disclosure_date_conflicts'] = date_proofs
        result['source_receipts'] = sorted(documents)
        # Revision and action evidence is also an input. Its actual arrival
        # participates even when it does not supply a numeric field.
        source_arrivals = [datetime.fromisoformat(arrival) for _, arrival in documents.values()]
        result['input_received_at_min'] = min(source_arrivals).isoformat()
        result['input_received_at_max'] = max(source_arrivals).isoformat()
        result['valuation_eligible'] = result['status'] in {'derived_core_fields','derived_core_fields_with_known_undefined_pb'}
        # Daily-basic raw provenance is never relabelled as a provider result.
        result['qualification'] = 'reviewed_derived_valuation' if result['valuation_eligible'] else 'incomplete'
        return result


    def _reviewed_earnings(self, proofs, documents, code, day, *, observed_at=None, extraction_replacements=None):
        """Bind profit, allocation and every period's corrections independently.

        Missing PE evidence does not discard qualified PB. Malformed evidence is
        reported as unknown; it is never replaced by equity, EPS times current
        shares, dynamic PE, or annualization of a half-year result.
        """
        results = {}
        for mode in ('static', 'ttm'):
            proof = proofs.get(mode, {})
            out = dict(proof, qualified=False, ts_code=code, trade_date=day)
            try:
                known_at = observed_at or max(arrival for _, arrival in documents.values())
                cutoff = datetime.fromisoformat(known_at)
                if cutoff.tzinfo is None:
                    raise ValueError('explicit earnings observation timezone required')
                inventory = proof['revision_inventory']
                components = proof['periods']
                if (proof.get('definition') != 'ordinary_shareholder_profit_v1'
                        or inventory.get('as_of') != day
                        or inventory.get('scope') != 'all_published_consolidated_income_revisions'
                        or not inventory.get('selection_reason') or not components):
                    raise ValueError('dated income revision inventory missing')
                receipts = inventory['statement_receipts']
                catalogues = [documents[h][0] for h in inventory['document_receipts']]
                for digest in inventory['document_receipts']:
                    arrival = datetime.fromisoformat(documents[digest][1])
                    if arrival.tzinfo is None or arrival > cutoff:
                        raise ValueError('income catalogue received after observation')
                    _validate_valuation_document(documents[digest][0])
                oldest = min(component['period_start'] for component in components)
                if (any(h not in documents for h in receipts) or not any(
                    p.get('kind') == 'disclosure_inventory' and p.get('ts_code') == code
                    and p.get('as_of') == day and p.get('catalogue_complete') is True
                    and p.get('window_from', '9999-12-31') <= oldest
                    and p.get('window_through') == day for p in catalogues)):
                    raise ValueError('complete dated income correction catalogue missing')
                periods = []
                for component in components:
                    values = {}
                    identity = None
                    hashes = []
                    supplied = component['inputs']
                    direct = 'ordinary_profit' in supplied
                    subtraction = 'other_equity_profit_distribution' in supplied or not direct
                    if not direct and not subtraction:
                        raise ValueError('ordinary profit source path required')
                    if subtraction and not all(f in supplied for f in ('parent_profit', 'other_equity_profit_distribution')):
                        raise ValueError('complete parent profit and distribution path required')
                    fields = (['ordinary_profit'] if direct else []) + (
                        ['parent_profit', 'other_equity_profit_distribution'] if subtraction else
                        ['parent_profit'] if 'parent_profit' in supplied else [])
                    for field in fields:
                        item = component['inputs'][field]
                        digest = item['receipt_sha256']
                        source, arrival = documents[digest]
                        received = datetime.fromisoformat(arrival)
                        if received.tzinfo is None or received > cutoff:
                            raise ValueError('income source received after observation')
                        statement = source['statement']
                        announcement = _iso(statement['announcement_date'])
                        current_identity = (statement.get('period_start'), _iso(statement['period']), announcement)
                        if (source.get('schema') != 'official_valuation_document_v1'
                                or source.get('ts_code') != code or digest not in receipts
                                or statement.get('basis') != 'consolidated'
                                or current_identity[:2] != (component['period_start'], component['period_end'])
                                or component['period_end'] > announcement or announcement > day
                                or item.get('unit') != 'yuan' or item.get('semantic') != field
                                or (identity is not None and identity != current_identity)):
                            raise ValueError('income identity, period or definition mismatch')
                        extracted = _official_field_binding(source, field, item)
                        number = _num(extracted.get('value'))
                        if (number is None or not math.isfinite(number) or isinstance(item.get('value'), bool)
                                or number != _num(item.get('value'))):
                            raise ValueError('income or allocation not evidenced')
                        _validate_valuation_document(source)
                        identity = current_identity
                        hashes.append(digest)
                        values[field] = number
                        values[field+'_received_at'] = arrival
                    if direct and subtraction and not math.isclose(values['ordinary_profit'],
                            values['parent_profit']-values['other_equity_profit_distribution'],
                            rel_tol=0, abs_tol=0.01):
                        raise ValueError('direct ordinary profit and subtraction path disagree')
                    if direct and 'parent_profit' in values:
                        difference = values['parent_profit']-values['ordinary_profit']
                        if not math.isfinite(difference):
                            raise ValueError('parent and ordinary profit reconciliation overflow')
                        values['ordinary_profit_reconciliation'] = dict(
                            method='parent_minus_explicit_ordinary_profit',difference_yuan=difference,
                            is_raw_distribution=False,statement_receipts=hashes[:])
                    periods.append(dict(values, period_start=component['period_start'],
                                        period_end=component['period_end'], announcement_date=identity[2],
                                        statement_receipts=hashes))
                self._assert_revision_inventory(code, day, known_at, 'income', inventory, documents, oldest,
                    selected=[(p['period_end'], p['announcement_date']) for p in periods],
                    extraction_replacements=extraction_replacements)
                latest = self._latest_income_period(catalogues, day, mode)
                selected = periods[0]['period_end'] if mode == 'static' else proof.get('selected_period')
                if selected != latest:
                    raise ValueError('selected earnings period is not latest applicable disclosed period')
                out.update(qualified=True, periods=periods, latest_applicable_period=latest,
                           as_known_at=known_at)
            except (KeyError, ValueError, TypeError, IndexError, OSError, OverflowError) as exc:
                out['qualification_error'] = str(exc)
            results[mode] = out
        return results


    def qualified_valuation_reviews(self, trade_date, *, observed_at=None):
        """Revalidate underlying receipts on every read; never trust cached green."""
        now = observed_at or datetime.now(timezone.utc).isoformat()
        results, seen = {}, set()
        self._valuation_review_failures = {}
        records = self._valuation_con.execute(
            "SELECT payload_json,payload_hash FROM multi_source_observation WHERE data_type='valuation_review' "
            "AND json_extract_string(payload_json,'$.review.trade_date')=? ORDER BY observed_at DESC,rowid DESC",
            [_iso(trade_date)]).fetchall()
        for raw, digest in records:
            code = None
            try:
                payload = json.loads(raw)
                code = payload['review']['ts_code']
                if code in seen:
                    continue
                seen.add(code)  # A broken newer review must not revive an older one.
                if hashlib.sha256(raw.encode()).hexdigest() != digest:
                    self._valuation_review_failures[code] = 'valuation review payload hash mismatch'
                    continue
                result = self._valuation_review(payload['review'], trade_date, now)
                if result['valuation_eligible']:
                    results[code] = dict(result, review_sha256=digest)
                else:
                    self._valuation_review_failures[code] = 'reviewed core inputs remain unqualified'
            except (KeyError, TypeError, ValueError, IndexError, AttributeError, ImportError, OSError) as exc:
                if code:
                    self._valuation_review_failures[code] = str(exc)
                continue
        return results


    def valuation_completion_report(self, trade_date, codes, *, observed_at=None):
        """Read retained receipts once; never issue a diagnostic request here.

        BAK PB/BVPS is useful arithmetic evidence but has no verified daily_basic
        denominator mapping. A prior close cannot silently become today's close.
        Every unresolved security remains in the report, including null-PB rows.
        """
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Shanghai')
        at = datetime.fromisoformat(observed_at) if observed_at else datetime.now(timezone.utc)
        if at.tzinfo is None:
            raise ValueError('explicit observation timezone required')
        day = _iso(trade_date)
        wanted = set(codes)
        provider = self._valuation_provider
        retained = {code: {} for code in sorted(wanted)}
        rejected = []
        types = ('tushare_daily_basic', 'tushare_daily', 'tushare_stk_premarket', 'tushare_bak_basic')
        receipts = self._valuation_con.execute(
            "SELECT data_type,payload_json,payload_hash,observed_at FROM multi_source_observation "
            "WHERE data_type IN (?,?,?,?) AND provider=? "
            "AND json_extract_string(payload_json,'$.params.trade_date')=? "
            "ORDER BY observed_at DESC,payload_hash", [*types, provider, _ymd(trade_date)]).fetchall()
        for typ, raw, digest, arrival in receipts:
            received = arrival.replace(tzinfo=zone) if arrival.tzinfo is None else arrival
            if hashlib.sha256(raw.encode()).hexdigest() != digest or received > at:
                rejected.append({'receipt_sha256': digest, 'reason': 'hash_or_arrival_invalid'})
                continue
            payload = json.loads(raw)
            api = typ.removeprefix('tushare_')
            if payload.get('api') != api or payload.get('error_type'):
                continue
            rows = payload.get('rows', [])
            identities = [r.get('ts_code') for r in rows if isinstance(r, dict)]
            for row in rows:
                code = row.get('ts_code')
                if code not in wanted or api in retained[code]:
                    continue
                if identities.count(code) != 1 or row.get('trade_date') != _ymd(trade_date):
                    rejected.append({'receipt_sha256': digest, 'ts_code': code, 'reason': 'identity_or_date'})
                    continue
                retained[code][api] = {'row': row, 'receipt_sha256': digest, 'received_at': received.isoformat()}
        # Financial inputs have announcement/report dates, not trade_date.
        # Only retained original consolidated statements are eligible; absent
        # other-equity instruments remain unknown, never assumed to be zero.
        financial = {}
        for raw, digest, arrival in self._valuation_con.execute(
                "SELECT payload_json,payload_hash,observed_at FROM multi_source_observation "
                "WHERE data_type='tushare_balancesheet' AND provider=? ORDER BY observed_at DESC",
                [provider]).fetchall():
            received = arrival.replace(tzinfo=zone) if arrival.tzinfo is None else arrival
            if hashlib.sha256(raw.encode()).hexdigest() != digest or received > at:
                continue
            payload = json.loads(raw)
            if payload.get('api') != 'balancesheet' or payload.get('error_type'):
                continue
            for row in payload.get('rows', []):
                code = row.get('ts_code')
                if code not in wanted or str(row.get('report_type')) != '1':
                    continue
                try:
                    announcement = _iso(row.get('f_ann_date') or row['ann_date'])
                    period = _iso(row['end_date'])
                    date.fromisoformat(announcement)
                    date.fromisoformat(period)
                except (KeyError, TypeError, ValueError):
                    continue
                if period > announcement or announcement > day:
                    continue
                rank = (period, announcement, received)
                if code not in financial or rank > financial[code]['rank']:
                    financial[code] = dict(row=row, receipt_sha256=digest, received_at=received.isoformat(),
                                           rank=rank, announcement=announcement)
        output = {}
        reviewed_values = self.qualified_valuation_reviews(day, observed_at=at.isoformat())
        for code, sources in retained.items():
            inputs = {}
            for field, native, semantic, unit in (
                    ('price', 'close', 'official_close', 'yuan'),
                    ('total_share', 'total_share', 'total_shares', '10000_shares'),
                    ('float_share', 'float_share', 'circulating_shares', '10000_shares')):
                source = sources.get('daily_basic', {})
                if source.get('row', {}).get(native) is None:
                    source = sources.get('daily' if field == 'price' else 'stk_premarket', {})
                value = source.get('row', {}).get(native)
                if value is not None:
                    inputs[field] = dict(value=value, unit=unit, semantic=semantic, ts_code=code,
                        source_date=day, valid_through=day, available_at=day+'T15:00:00+08:00',
                        received_at=source['received_at'], receipt_sha256=source['receipt_sha256'])
            native_source = sources.get('daily_basic', {})
            for field, semantic in (('total_mv', 'total_market_value'), ('circ_mv', 'circulating_market_value')):
                value = native_source.get('row', {}).get(field)
                if value is not None:
                    inputs[field] = dict(value=value, unit='10000_yuan', semantic=semantic, ts_code=code,
                        source_date=day, valid_through=day, available_at=day+'T15:00:00+08:00',
                        received_at=native_source['received_at'], receipt_sha256=native_source['receipt_sha256'])
            balance = financial.get(code)
            if balance:
                for field, native, semantic in (('equity', 'total_hldr_eqy_exc_min_int', 'parent_equity'),
                                                ('other_equity', 'oth_eqt_tools', 'other_equity_tools')):
                    if balance['row'].get(native) is not None:
                        inputs[field] = dict(value=balance['row'][native], unit='yuan', semantic=semantic,
                            ts_code=code, source_date=balance['announcement'], valid_through=day,
                            available_at=balance['announcement']+'T23:59:59+08:00',
                            received_at=balance['received_at'], receipt_sha256=balance['receipt_sha256'])
            result = derive_valuation(code, day, inputs, observed_at=at.isoformat())
            if balance:
                result['financial_input'] = dict(receipt_sha256=balance['receipt_sha256'],
                    report_period=balance['row']['end_date'], announcement_date=balance['announcement'],
                    status='latest_retained_original_statement_not_full_revision_inventory')
                # A latest *retained* report is not proof that no newer or
                # restated report was published by this session. Preserve the
                # useful calculation, but require the dated revision inventory.
                result['missing_inputs']['financial_revision_inventory'] = ['dated_complete_revision_inventory_required']
                result['status'] = 'incomplete'
            if code in reviewed_values:
                result = reviewed_values[code]
            result['review_revalidation_error'] = self._valuation_review_failures.get(code)
            if balance:
                # A retained financial reference is useful to explain a gap,
                # but is never a substitute for a reviewed current denominator.
                result['financial_reference'] = {
                    'report_period': _iso(balance['row']['end_date']),
                    'announcement_date': balance['announcement'],
                    'received_at': balance['received_at'],
                    'receipt_sha256': balance['receipt_sha256'], 'basis': 'consolidated',
                    'parent_equity_yuan': balance['row'].get('total_hldr_eqy_exc_min_int'),
                    'other_equity_tools_yuan': balance['row'].get('oth_eqt_tools'),
                    'status': 'latest_retained_reference_not_latest_applicable_confirmation',
                    'supplies_numeric_qualification': False,
                }
            result['native_valuation'] = {
                'values': {field: sources.get('daily_basic', {}).get('row', {}).get(field)
                           for field in ('pb', 'total_mv', 'circ_mv', 'pe')},
                'receipt_sha256': sources.get('daily_basic', {}).get('receipt_sha256'),
                'received_at': sources.get('daily_basic', {}).get('received_at'),
                'source_trade_date': day, 'status': 'retained_source_not_batch_certification',
            }
            result['retained_sources'] = {api: {k: v for k, v in source.items() if k != 'row'}
                                          for api, source in sources.items()}
            native_pb = _num(sources.get('daily_basic', {}).get('row', {}).get('pb'))
            raw_pb = sources.get('daily_basic', {}).get('row', {}).get('pb')
            result['native_pb_status'] = ('finite' if native_pb is not None and math.isfinite(native_pb)
                                         else 'row_absent' if 'daily_basic' not in sources
                                         else 'provider_null' if raw_pb is None else 'invalid_value')
            pre = sources.get('stk_premarket', {}).get('row', {})
            bak = sources.get('bak_basic', {}).get('row', {})
            price, bvps, pb = (_num(pre.get('pre_close')), _num(bak.get('bvps')), _num(bak.get('pb')))
            if all(v is not None and math.isfinite(v) for v in (price, bvps, pb)) and price > 0 and bvps != 0:
                implied = price / bvps
                if math.isfinite(implied):
                    result['alternative_pb_check'] = dict(reported_pb=pb, reference_price_div_bvps=implied,
                        agrees_at_reported_precision=abs(implied-pb) <= 0.005000001,
                        status='arithmetic_only', required=['denominator_definition', 'suspension_price_policy',
                                                           'corporate_action_review', 'original_price_date'])
            output[code] = result
        return {'trade_date': day, 'observed_at': at.isoformat(), 'rows': output,
                'rejected_receipts': rejected, 'market_requests': 0, 'certifies_daily_basic': False}


    def valuation_capability_report(self, trade_date, codes, *, observed_at=None):
        """Isolate valuation uses without certifying a market-wide snapshot.

        Reference calculations and availability alone cannot supply PB. Native
        fields need a retained same-session receipt and matching committed fact;
        reviewed derived fields retain the original review's strict boundaries.
        Quote, trading and primary-flow qualification are assessed elsewhere.
        """
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Shanghai')
        report = self.valuation_completion_report(trade_date, codes, observed_at=observed_at)
        at = datetime.fromisoformat(report['observed_at'])
        day = report['trade_date']
        historical = day < at.astimezone(zone).date().isoformat()
        wanted = sorted(report['rows'])
        try:
            expected = self._expected_stock_codes(day, 'daily_basic')
        except (ValueError, KeyError, duckdb.Error, RuntimeError):
            expected = None
        committed = {}
        if wanted:
            try:
                for row in self._valuation_con.execute(
                    'SELECT ts_code,pb,total_mv,circ_mv,pe,fetched_at FROM tushare_daily_basic '
                    'WHERE date=? AND ts_code IN (' + ','.join('?' for _ in wanted) + ')',
                    [day, *wanted]).fetchall():
                    if row[0] in committed:
                        committed[row[0]] = None
                    else:
                        committed[row[0]] = row
            except duckdb.Error:
                pass  # No native fact table does not revoke a valid derived review.
        review_receipts = {row[0] for row in self._valuation_con.execute(
            "SELECT payload_hash FROM multi_source_observation WHERE data_type='valuation_review' "
            "AND observed_at<=?", [at.astimezone(zone).replace(tzinfo=None)]).fetchall()}
        rows, qualified_reviews = {}, {}
        for code, item in report['rows'].items():
            scope_ok = expected is not None and code in expected
            reviewed = (bool(item.get('valuation_eligible')) and scope_ok
                        and item.get('review_sha256') in review_receipts)
            if reviewed:
                qualified_reviews[code] = item
            native = item['native_valuation']
            original = native['values']
            canonical = committed.get(code)
            native_matches = False
            if canonical and native['receipt_sha256']:
                fetched = canonical[5]
                if isinstance(fetched, datetime):
                    fetched = fetched.replace(tzinfo=zone) if fetched.tzinfo is None else fetched
                    native_matches = fetched <= at
            fields = {}
            for index, field in enumerate(('pb', 'total_mv', 'circ_mv', 'pe'), 1):
                raw_value = _num(original.get(field))
                numeric = raw_value is not None and math.isfinite(raw_value)
                if field in {'total_mv', 'circ_mv'}:
                    numeric = numeric and raw_value > 0
                matched = (scope_ok and native_matches and numeric
                           and _num(canonical[index]) == raw_value)
                if field == 'circ_mv':
                    total = _num(original.get('total_mv'))
                    matched = matched and total is not None and math.isfinite(total) and raw_value <= total
                value = item.get('values', {}).get(field) if reviewed else None
                review_status = item.get('field_status', {}).get(field, 'unknown')
                review_numeric = value is not None and math.isfinite(value)
                qualified = (reviewed and review_numeric) or matched
                known_undefined = reviewed and review_status in {
                    'undefined_zero_adjusted_equity', 'not_applicable_nonpositive_ordinary_profit'}
                fields[field] = {
                    'status': (review_status if reviewed and review_numeric else
                               'qualified_native_field' if matched else
                               review_status if known_undefined else 'unknown'),
                    'value': value if reviewed and review_numeric else raw_value if matched else None,
                    'numeric_qualified_for_session': bool(qualified),
                    'numeric_available_currently': bool(qualified and not historical),
                    'known_undefined': bool(known_undefined),
                    'receipt_sha256': (item.get('review_sha256') if reviewed else
                                       native['receipt_sha256'] if matched else None),
                }
            ttm = item.get('values', {}).get('pe_ttm') if reviewed else None
            ttm_qualified = ttm is not None and math.isfinite(ttm)
            ttm_status = item.get('field_status', {}).get('pe_ttm', 'unknown') if reviewed else 'unknown'
            fields['pe_ttm'] = {'status': ttm_status,
                'value': ttm if ttm_qualified else None, 'numeric_qualified_for_session': ttm_qualified,
                'numeric_available_currently': bool(ttm_qualified and not historical),
                'known_undefined': ttm_status == 'not_applicable_nonpositive_ordinary_profit'}
            pb = fields['pb']
            native_core = all(fields[f]['numeric_qualified_for_session'] for f in ('pb','total_mv','circ_mv'))
            reasons = {} if reviewed or native_core else dict(item.get('missing_inputs') or {})
            if not scope_ok:
                reasons['identity_scope'] = ['expected_universe_unverified' if expected is None else 'outside_expected_universe']
            if not pb['numeric_qualified_for_session'] and not pb.get('known_undefined'):
                reasons.setdefault('pb', ['no_qualified_same_session_pb'])
            if item.get('valuation_eligible') and scope_ok and not reviewed:
                reasons['review'] = ['review_not_registered_at_observation']
            if item.get('review_revalidation_error'):
                reasons['review'] = [item['review_revalidation_error']]
            earnings_missing = dict(item.get('earnings_missing_inputs') or {})
            for field in ('pe', 'pe_ttm'):
                if not fields[field]['numeric_qualified_for_session'] and not fields[field]['known_undefined']:
                    earnings_missing.setdefault(field, ['qualified_profit_or_same_session_field_missing'])
            rows[code] = {
                'trade_date': day, 'historical_as_of': historical,
                'included_in_expected_universe': None if expected is None else code in expected,
                'qualified_core_for_session': bool(reviewed or native_core), 'fields': fields,
                'current_core_available': bool((reviewed or native_core) and not historical),
                'value_screen_eligible_for_session': bool(pb['numeric_qualified_for_session'] and pb['value'] > 0),
                'value_screen_eligible': bool(not historical and pb['numeric_qualified_for_session'] and pb['value'] > 0),
                'reasons': reasons, 'earnings_missing_inputs': earnings_missing,
                'financial_reference': item.get('financial_reference'),
                'native_pb_status': item.get('native_pb_status'),
                'input_received_at_min': item.get('input_received_at_min'),
                'input_received_at_max': item.get('input_received_at_max'),
                'native_received_at': native['received_at'],
                'review_revalidation_error': item.get('review_revalidation_error'),
                'does_not_assess': ['quote', 'trade', 'primary_flow', 'execution'],
            }
        failed = any(row['review_revalidation_error'] for row in rows.values())
        status = ('partial' if any(row['qualified_core_for_session'] for row in rows.values()) else 'unknown') if failed else 'assessed'
        return {'schema': 'per_security_valuation_capabilities_v1', 'trade_date': day, 'status': status,
            'observed_at': at.isoformat(), 'historical_as_of': historical, 'rows': rows,
            'reviewed_valuation': qualified_reviews,
            'scope': 'valuation_only', 'market_requests': 0, 'business_rows_written': 0,
            'certifies_daily_basic': False, 'changes_expected_universe': False}


    def _expected_stock_codes(self, trade_date, dataset=None):
        expected = {r[0] for r in self._valuation_con.execute(
            "SELECT DISTINCT ts_code FROM tushare_stock_basic WHERE ts_code IS NOT NULL "
            "AND (list_date IS NULL OR list_date<=CAST(? AS DATE)) "
            "AND (delist_date IS NULL OR delist_date>CAST(? AS DATE))", [_iso(trade_date)] * 2).fetchall()}
        reference = self._reference_version(trade_date) or {}
        if not reference:
            latest_reference = self._reference_version() or {}
            if latest_reference.get('membership_only'):
                raise self._valuation_scope_error('historical listing dates unavailable for membership-only identities')
        if reference.get('membership_only') and reference.get('membership_date') != _iso(trade_date):
            raise self._valuation_scope_error('historical listing dates unavailable for membership-only identities')
        if reference.get('membership_date') == _iso(trade_date):
            expected -= set(reference.get('not_listed', []))
        if dataset in {'daily', 'moneyflow'}:
            rows = self._suspension_rows(trade_date)
            if rows is not None:
                suspended = {r['ts_code'] for r in rows if r.get('trade_date') == _ymd(trade_date)
                             and r.get('suspend_type') == 'S' and r.get('suspend_timing') in (None, '')}
                traded = {r[0] for r in self._valuation_con.execute(
                    "SELECT ts_code FROM tushare_daily WHERE date=? AND (volume>0 OR turnover>0)",
                    [_iso(trade_date)]).fetchall()}
                # Independent full-day evidence needs no invented zero-price row.
                # Conflicting traded evidence keeps the instrument required.
                expected -= suspended - traded
        return expected


    def _suspension_rows(self, trade_date):
        receipt = self._valuation_con.execute(
            "SELECT payload_json,payload_hash FROM multi_source_observation WHERE data_type='tushare_suspend_d_snapshot' "
            "AND status='qualified' AND provider=? AND json_extract_string(payload_json,'$.params.trade_date')=? "
            "ORDER BY observed_at DESC LIMIT 1",
            [self._valuation_provider, _ymd(trade_date)]).fetchone()
        if not receipt or hashlib.sha256(receipt[0].encode()).hexdigest() != receipt[1]:
            return None
        payload = json.loads(receipt[0])
        rows = payload.get('rows')
        if not isinstance(rows, list) or len({r.get('ts_code') for r in rows}) != len(rows) or any(
                not r.get('ts_code') or r.get('trade_date') != _ymd(trade_date)
                or r.get('suspend_type') not in {'S', 'R'} for r in rows):
            return None
        return rows


    def _reference_version(self, trade_date=None):
        from trade_system.ths_quality import qualified_stock_reference
        return qualified_stock_reference(self._valuation_con,
            provider=self._valuation_provider,
            membership_date=_iso(trade_date) if trade_date is not None else None)
