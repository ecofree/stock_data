"""Read-only research query server over the DuckDB database (localhost only).

Endpoints:
  GET  /            minimal query console page
  GET  /api/tables  table/view catalog
  POST /api/query   {"sql": "..."} -> {columns, rows, elapsed_ms}

Safety model:
- The connection is opened read_only.
- Only a single SELECT/WITH statement is allowed; writes/DDL/ATTACH are
  rejected by keyword guard AND by the read-only engine.
- Results clamp to LIMIT 500 when the statement has no explicit LIMIT.

Run: D:\\anaconda\\python.exe scripts\\research_server.py --port 8765
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb  # noqa: E402

MAX_ROWS = 500
FORBIDDEN_RE = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|detach|copy|export|"
    r"import|install|load|call|set|reset|pragma|vacuum|checkpoint|begin|"
    r"commit|rollback)\b",
    re.IGNORECASE,
)
COMMENT_RE = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)


class QueryRejected(ValueError):
    pass


def guard_sql(sql: str) -> str:
    """Validate and normalize a user SELECT. Raises QueryRejected."""
    cleaned = COMMENT_RE.sub(" ", sql or "").strip()
    if not cleaned:
        raise QueryRejected("empty statement")
    # allow exactly one optional trailing semicolon
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].strip()
    if ";" in cleaned:
        raise QueryRejected("multiple statements are not allowed")
    first_word = cleaned.split(None, 1)[0].lower()
    if first_word not in ("select", "with"):
        raise QueryRejected("only SELECT / WITH statements are allowed")
    match = FORBIDDEN_RE.search(cleaned)
    if match:
        raise QueryRejected(f"keyword '{match.group(0)}' is not allowed")
    if not re.search(r"\blimit\b", cleaned, re.IGNORECASE):
        cleaned += f" LIMIT {MAX_ROWS}"
    return cleaned


def run_query(con: duckdb.DuckDBPyConnection, sql: str) -> dict:
    guarded = guard_sql(sql)
    started = time.perf_counter()
    cur = con.execute(guarded)
    columns = [d[0] for d in cur.description]
    rows = cur.fetchmany(MAX_ROWS + 1)
    truncated = len(rows) > MAX_ROWS
    rows = rows[:MAX_ROWS]
    return {
        "sql": guarded,
        "columns": columns,
        "rows": [[_safe(v) for v in row] for row in rows],
        "truncated": truncated,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


def _safe(value):
    if hasattr(value, "isoformat"):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")[:200]
    return value


_PAGE = """<!doctype html><html><head><meta charset='utf-8'>
<title>stock_data research console</title>
<style>
body{font-family:Consolas,monospace;background:#111;color:#ddd;margin:20px}
textarea{width:100%;height:120px;background:#1b1b1b;color:#eee;border:1px solid #444}
button{padding:6px 14px;margin-top:6px}
table{border-collapse:collapse;margin-top:12px;font-size:13px}
td,th{border:1px solid #3a3a3a;padding:3px 8px}
th{background:#222}
.err{color:#f66;white-space:pre-wrap}
.meta{color:#888;font-size:12px;margin-top:4px}
</style></head><body>
<h3>stock_data research console <span class='meta'>(read-only, localhost)</span></h3>
<textarea id='q'>SELECT trade_date, phase, limit_up_count, premium_pct, score
FROM market_cycle_phase ORDER BY trade_date DESC LIMIT 20</textarea><br>
<button onclick='run()'>Run</button> <span id='meta' class='meta'></span>
<div id='out'></div>
<script>
async function run(){
 const q=document.getElementById('q').value;
 const out=document.getElementById('out'), meta=document.getElementById('meta');
 try{
  const r=await fetch('/api/query',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({sql:q})});
  const data=await r.json();
  if(data.error){out.innerHTML="<p class='err'>"+data.error+"</p>";meta.textContent='';return;}
  meta.textContent=data.rows.length+' rows, '+data.elapsed_ms+' ms'
    +(data.truncated?' (clamped)':'');
  let html='<table><tr>'+data.columns.map(c=>'<th>'+c+'</th>').join('')+'</tr>';
  for(const row of data.rows){html+='<tr>'+row.map(v=>'<td>'+(v===null?'':v)+'</td>').join('')+'</tr>';}
  out.innerHTML=html+'</table>';
 }catch(e){out.innerHTML="<p class='err'>"+e+"</p>";}
}
</script></body></html>"""


def make_handler(con: duckdb.DuckDBPyConnection):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: str, ctype: str = "application/json"):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path == "/":
                self._send(200, _PAGE, "text/html; charset=utf-8")
            elif self.path == "/api/tables":
                rows = con.execute(
                    """SELECT table_type, table_name FROM information_schema.tables
                       WHERE table_schema='main' ORDER BY table_type, table_name"""
                ).fetchall()
                self._send(200, json.dumps({"tables": [list(r) for r in rows]}))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path != "/api/query":
                self._send(404, json.dumps({"error": "not found"}))
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                result = run_query(con, str(payload.get("sql", "")))
                self._send(200, json.dumps(result, ensure_ascii=False))
            except QueryRejected as exc:
                self._send(400, json.dumps({"error": str(exc)}))
            except Exception as exc:
                self._send(400, json.dumps({"error": f"{type(exc).__name__}: {exc}"}))

        def log_message(self, fmt, *args):  # quiet
            pass

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(PROJECT_ROOT / "kpl_data.duckdb"))
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    con = duckdb.connect(args.db, read_only=True)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(con))
    print(f"research console: http://127.0.0.1:{args.port}/ (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
