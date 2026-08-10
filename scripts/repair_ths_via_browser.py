"""Repair THS concept-member snapshots through an authenticated Chrome session.

The public THS detail endpoint currently allows the first five pages without a
login and then returns a small upass response.  When an operator has an
authenticated THS tab open, Chrome can fetch the remaining pages with its
session cookies.  This script uses the local CDP bridge only for that explicit
repair; it never copies cookies to disk or prints them.

The database is changed only after every requested page for every requested
concept has been fetched and parsed.  A failed/partial browser crawl therefore
cannot replace a previously known snapshot with truncated data.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schema import init_schema
from trade_system.ths_history import _ths_index_members


DEFAULT_TARGET = "D62CACAC3A2DC89A129F90B183E00169"


def _cdp_eval(target: str, source: str, timeout: int = 180) -> object:
    request = urllib.request.Request(
        f"http://localhost:3456/eval?target={target}",
        data=source.encode("utf-8"),
        method="POST",
        headers={"Content-Type": "text/plain; charset=utf-8"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(f"CDP eval failed: {payload['error']}")
    value = payload.get("value")
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _browser_fetch_chunk(target: str, concept_id: str, start_page: int,
                         end_page: int, delay_ms: int = 120) -> dict[str, object]:
    # TextDecoder is important: THS serves GB18030 while Chrome otherwise
    # exposes the AJAX response as mojibake.  Only constituent fields are
    # returned to Python; browser cookies stay inside Chrome.
    source = f"""
(async()=>{{
  const code={json.dumps(concept_id)};
  const start={int(start_page)};
  const end={int(end_page)};
  const delay={max(0, int(delay_ms))};
  const sleep=(ms)=>new Promise(resolve=>setTimeout(resolve,ms));
  const out={{concept_id:code, start_page:start, end_page:end, fetched_pages:0, advertised_pages:0, rows:[], errors:[]}};
  const seen=new Set();
  let next=start;
  const fetchPage=async(p)=>{{
    let last='';
    for(let attempt=0;attempt<3;attempt++){{
      try{{
        const r=await fetch('/gn/detail/field/199112/order/desc/page/'+p+'/ajax/1/code/'+code,
          {{credentials:'include',headers:{{'X-Requested-With':'XMLHttpRequest','Accept':'text/html, */*; q=0.01'}}}});
        const buf=await r.arrayBuffer();
        const html=new TextDecoder('gb18030').decode(buf);
        const doc=new DOMParser().parseFromString(html,'text/html');
        const pageInfo=(doc.querySelector('.page_info')?.textContent||'').match(/\\/\\s*(\\d+)/);
        if(pageInfo) out.advertised_pages=Math.max(out.advertised_pages,Number(pageInfo[1])||0);
        const rows=[];
        for(const tr of [...doc.querySelectorAll('tbody tr')]){{
          const cells=[...tr.querySelectorAll('td')].map(x=>x.textContent.trim());
          const stock=(cells[1]||'').match(/\\d{{6}}/); const name=(cells[2]||'').trim();
          if(stock && !seen.has(stock[0])){{ seen.add(stock[0]); rows.push({{code:stock[0],name,rank:Number(cells[0])||0}}); }}
        }}
        if(r.status!==200 || !html.toLowerCase().includes('m-pager-table') || !rows.length){{
          last='page '+p+' status='+r.status+' rows='+rows.length+' html='+html.length;
          throw new Error(last);
        }}
        return rows;
      }}catch(e){{ last=String(e); await sleep(400*(attempt+1)); }}
    }}
    throw new Error(last);
  }};
  const worker=async()=>{{
    while(true){{
      const p=next++;
      if(p>end) return;
      try{{
        const rows=await fetchPage(p);
        out.rows.push(...rows);
      }}catch(e){{ out.errors.push(String(e)); }}
      if(delay) await sleep(delay);
    }}
  }};
  await Promise.all(Array.from({{length:Math.min(4,end-start+1)}},()=>worker()));
  out.fetched_pages=out.errors.length ? 0 : end;
    return JSON.stringify(out);
}})()
"""
    result = _cdp_eval(target, source, timeout=max(60, (end_page - start_page + 1) * 3))
    if not isinstance(result, dict):
        raise RuntimeError("CDP returned an invalid THS payload")
    return result


def _browser_fetch(target: str, concept_id: str, expected_pages: int,
                   delay_ms: int = 120, chunk_pages: int = 20) -> dict[str, object]:
    """Fetch in bounded CDP responses so large boards do not hit the bridge limit."""
    all_rows: list[dict[str, object]] = []
    seen: set[str] = set()
    fetched = 0
    errors: list[object] = []
    total_pages = max(1, int(expected_pages or 1))
    start = 1
    discovered_pages = 0
    while start <= total_pages:
        end = min(total_pages, start + max(1, int(chunk_pages)) - 1)
        payload = _browser_fetch_chunk(target, concept_id, start, end, delay_ms)
        discovered_pages = max(discovered_pages, int(payload.get("advertised_pages") or 0))
        if start == 1 and discovered_pages > total_pages:
            # Stale-cache repair historically stored a synthetic one-page
            # checkpoint.  The live page-1 response contains the authoritative
            # pager denominator; expand the crawl before accepting the result.
            total_pages = discovered_pages
        chunk_fetched = int(payload.get("fetched_pages") or 0)
        if chunk_fetched != end or payload.get("errors"):
            errors.extend(payload.get("errors") or [f"chunk pages={start}-{end} fetched={chunk_fetched}"])
            break
        for row in payload.get("rows") or []:
            code = str(row.get("code") or "")
            if code and code not in seen:
                seen.add(code)
                all_rows.append(row)
        fetched = end
        start = end + 1
    all_rows.sort(key=lambda row: (int(row.get("rank") or 0), str(row.get("code") or "")))
    return {"concept_id": concept_id, "expected_pages": total_pages,
            "fetched_pages": fetched, "rows": all_rows, "errors": errors}


def _parse_codes(raw: str) -> list[str]:
    codes = [item.strip() for item in raw.split(",") if item.strip()]
    if not codes:
        raise ValueError("at least one concept code is required")
    if any(not re.fullmatch(r"\d{6}", code) for code in codes):
        raise ValueError("concept codes must be six digits, e.g. 300900")
    return list(dict.fromkeys(codes))


def _parse_blockrank_map(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in (raw or "").split(","):
        if not item.strip():
            continue
        code, sep, index = item.partition("=")
        if not sep or not re.fullmatch(r"\d{6}", code.strip()) or not index.strip().isdigit():
            raise ValueError("--blockrank-index must use CODE=INDEX[,CODE=INDEX]")
        result[code.strip()] = index.strip()
    return result


def _cached_complete_members(con: duckdb.DuckDBPyConnection, requested_date: str,
                             concept_id: str, max_age_days: int = 14) -> tuple[list[dict[str, str]], str] | None:
    candidate = con.execute(
        """
        SELECT h.trade_date, count(*) AS rows, max(c.member_rows) AS expected
        FROM ths_concept_stock_history h
        JOIN ths_concept_member_checkpoint c
          ON c.trade_date=h.trade_date AND c.concept_code=h.concept_code
        WHERE h.trade_date < CAST(? AS DATE) AND h.concept_code=? AND c.status='success'
        GROUP BY h.trade_date
        HAVING count(*)=max(c.member_rows) AND count(*)>0
        ORDER BY h.trade_date DESC LIMIT 1
        """, [requested_date, concept_id]
    ).fetchone()
    if not candidate:
        return None
    source_date = str(candidate[0])[:10]
    age = (date.fromisoformat(requested_date) - date.fromisoformat(source_date)).days
    if age < 0 or age > max_age_days:
        return None
    rows = con.execute(
        "SELECT stock_code,stock_name FROM ths_concept_stock_history "
        "WHERE trade_date=? AND concept_code=? ORDER BY concept_rank,stock_code",
        [source_date, concept_id],
    ).fetchall()
    members = [{"code": str(row[0]), "name": str(row[1] or "")} for row in rows]
    return (members, source_date) if members else None


def repair(db_path: str | Path, trade_date: str, target: str, codes: list[str],
           delay_ms: int = 120, allow_stale_cache: bool = False,
           blockrank_map: dict[str, str] | None = None) -> dict[str, object]:
    db_path = str(db_path)
    con = duckdb.connect(db_path)
    try:
        init_schema(con)
        date_text = str(trade_date)[:10]
        if date_text != trade_date:
            date_text = date.fromisoformat(date_text).isoformat()
        metadata = {}
        for code in codes:
            row = con.execute(
                "SELECT d.concept_name, d.rank, pages_expected FROM ths_concept_daily d "
                "LEFT JOIN ths_concept_member_checkpoint c "
                "ON c.trade_date=d.trade_date AND c.concept_code=d.concept_code "
                "WHERE d.trade_date=? AND d.concept_code=? LIMIT 1",
                [date_text, f"THS-{code}"],
            ).fetchone()
            if not row and allow_stale_cache:
                # An interrupted pre-transaction repair may have removed the
                # current concept row before the process stopped.  Recover its
                # label/rank from the newest historical complete snapshot so a
                # stale fallback can restore the row atomically.
                row = con.execute(
                    "SELECT concept_name, rank, stock_count FROM ths_concept_daily "
                    "WHERE concept_code=? ORDER BY trade_date DESC LIMIT 1",
                    [f"THS-{code}"],
                ).fetchone()
            if not row:
                raise RuntimeError(f"concept {code} is not present for {date_text}")
            metadata[code] = (str(row[0] or ""), int(row[1] or 0), int(row[2] or 0))

        payloads: dict[str, dict[str, object]] = {}
        providers: dict[str, str] = {}
        source_dates: dict[str, str] = {}
        for code in codes:
            expected = metadata[code][2]
            if expected <= 0:
                raise RuntimeError(f"concept {code} has no advertised page count")
            if blockrank_map and code in blockrank_map:
                members, advertised = _ths_index_members(blockrank_map[code])
                if not members:
                    raise RuntimeError(f"THS blockrank returned no members for {code}")
                metadata[code] = (metadata[code][0], metadata[code][1], 1)
                payloads[code] = {"concept_id": code, "expected_pages": 1, "fetched_pages": 1,
                                  "rows": members, "errors": [], "blockrank_index": blockrank_map[code],
                                  "advertised_count": advertised}
                providers[code] = "ths_index_blockrank"
                continue
            try:
                payload = _browser_fetch(target, code, expected, delay_ms)
                expected = int(payload.get("expected_pages") or expected)
                fetched = int(payload.get("fetched_pages") or 0)
                rows = payload.get("rows") or []
                errors = payload.get("errors") or []
                if errors or fetched != expected or not rows:
                    raise RuntimeError(
                        f"concept {code} incomplete: pages={fetched}/{expected}, "
                        f"rows={len(rows)}, error={str(errors[:1])[:180]}"
                    )
                providers[code] = "ths_browser_cdp"
            except Exception:
                if not allow_stale_cache:
                    raise
                cached = _cached_complete_members(con, date_text, f"THS-{code}")
                if not cached:
                    raise
                members, source_date = cached
                metadata[code] = (metadata[code][0], metadata[code][1], 1)
                expected = 1
                source_dates[code] = source_date
                providers[code] = "ths_cached_weekly"
                payload = {"concept_id": code, "expected_pages": 1, "fetched_pages": 1,
                           "rows": members, "errors": [], "stale_fallback": True}
            payloads[code] = payload

        fetched_at = datetime.now().isoformat(sep=" ", timespec="seconds")
        verified = date_text == date.today().isoformat()
        crawler_version = "ths_browser_cdp_v1"
        # DuckDB versions used by the project can retain stale entries in a
        # user-created unique index after an interrupted large replacement.
        # Rebuild that optional index around the atomic replacement instead of
        # letting a phantom key abort the repair.
        business_index = "uq_ths_concept_member_business"
        had_business_index = bool(con.execute(
            "SELECT count(*) FROM duckdb_indexes() WHERE index_name=?", [business_index]
        ).fetchone()[0])
        # No row is visible to other processes until every requested concept,
        # its members, and the aggregate checkpoint are complete.  This is
        # essential because a killed process otherwise leaves a concept row
        # with a misleading stock_count and a truncated member table.
        con.execute("BEGIN TRANSACTION")
        try:
            if had_business_index:
                con.execute(f"DROP INDEX {business_index}")
            target_ids = [f"THS-{code}" for code in codes]
            placeholders = ",".join("?" for _ in target_ids)
            # Rebuild the member table inside the same transaction.  This
            # compacts any rows left by an interrupted append and avoids a
            # stale DuckDB index entry surviving a DELETE+INSERT replacement.
            con.execute(
                f"CREATE OR REPLACE TABLE ths_concept_stock_history AS "
                f"SELECT * FROM ths_concept_stock_history "
                f"WHERE NOT (trade_date=? AND concept_code IN ({placeholders}))",
                [date_text, *target_ids],
            )
            for code in codes:
                concept_id = f"THS-{code}"
                concept_name, concept_rank, expected = metadata[code]
                payload = payloads[code]
                expected = int(payload.get("expected_pages") or expected)
                rows = payload["rows"]
                provider = providers[code]
                stale_fallback = provider == "ths_cached_weekly"
                raw = {
                    "requested_date": date_text,
                    "fetched_date": date.today().isoformat(),
                    "date_verified": verified,
                    "effective_date_unknown": True,
                    "mode": "full",
                    "member_source": "browser_cdp" if not stale_fallback else "cached_weekly",
                    "ths_concept_id": code,
                    "pages_expected": expected,
                    "pages_fetched": int(payload["fetched_pages"]),
                    "provider": provider,
                    "browser_target": target if provider == "ths_browser_cdp" else "",
                    "blockrank_index": str(payload.get("blockrank_index") or ""),
                    "advertised_member_count": payload.get("advertised_count"),
                    "stale_fallback": stale_fallback,
                    "source_snapshot_date": source_dates.get(code, ""),
                    "error": "",
                }
                con.execute("DELETE FROM ths_concept_daily WHERE trade_date=? AND concept_code=?", [date_text, concept_id])
                con.execute(
                    "INSERT INTO ths_concept_daily(trade_date,concept_code,concept_name,rank,stock_count,source,raw_json,date_verified,fetched_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    [date_text, concept_id, concept_name, concept_rank, len(rows), provider, json.dumps(raw, ensure_ascii=False), verified, fetched_at],
                )
                for rank, member in enumerate(rows, 1):
                    member_raw = dict(raw, member_rank=rank)
                    con.execute(
                        "INSERT INTO ths_concept_stock_history(trade_date,concept_code,concept_name,stock_code,stock_name,concept_rank,source,raw_json,date_verified,fetched_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        [date_text, concept_id, concept_name, str(member["code"]), str(member.get("name") or ""), rank,
                         provider, json.dumps(member_raw, ensure_ascii=False), verified, fetched_at],
                    )
                con.execute(
                    "INSERT INTO ths_concept_member_checkpoint(trade_date,concept_code,concept_name,status,pages_expected,pages_fetched,member_rows,attempts,last_error,updated_at,provider,crawler_version,catalog_hash) "
                    "VALUES (?,?,?,?,?,?,?,?,?,current_timestamp,?,?,?) "
                    "ON CONFLICT(trade_date,concept_code) DO UPDATE SET concept_name=excluded.concept_name,status=excluded.status,"
                    "pages_expected=excluded.pages_expected,pages_fetched=excluded.pages_fetched,member_rows=excluded.member_rows,"
                    "attempts=ths_concept_member_checkpoint.attempts+1,last_error=excluded.last_error,updated_at=excluded.updated_at,"
                    "provider=excluded.provider,crawler_version=excluded.crawler_version,catalog_hash=excluded.catalog_hash",
                    [date_text, concept_id, concept_name, "success_stale" if stale_fallback else "success", expected, int(payload["fetched_pages"]), len(rows), 1, "", provider, crawler_version, ""],
                )

            total, success, bad = con.execute(
                "SELECT count(*), sum(CASE WHEN status='success' THEN 1 ELSE 0 END), "
                "sum(CASE WHEN status<>'success' THEN 1 ELSE 0 END) "
                "FROM ths_concept_member_checkpoint WHERE trade_date=?", [date_text]
            ).fetchone()
            concept_count, member_count = con.execute(
                "SELECT count(*), coalesce(sum(stock_count),0) FROM ths_concept_daily WHERE trade_date=?", [date_text]
            ).fetchone()
            snapshot_status = "success" if int(bad or 0) == 0 and int(total or 0) == int(success or 0) else "partial"
            con.execute(
                "INSERT INTO history_fetch_checkpoint(dataset,trade_date,page_no,status,rows_written,attempts,last_error,updated_at) "
                "VALUES ('ths_concept_snapshot',?,0,?,?,1,?,current_timestamp) "
                "ON CONFLICT(dataset,trade_date,page_no) DO UPDATE SET status=excluded.status,rows_written=excluded.rows_written,"
                "attempts=history_fetch_checkpoint.attempts+1,last_error=excluded.last_error,updated_at=excluded.updated_at",
                [date_text, snapshot_status, int(concept_count or 0) + int(member_count or 0), "" if snapshot_status == "success" else "remaining concept checkpoints are not success"],
            )
            # A successful repair promotes the member business key to the
            # canonical unique index even when a legacy DB did not have it.
            con.execute(
                f"CREATE UNIQUE INDEX {business_index} ON ths_concept_stock_history(trade_date,concept_code,stock_code)"
            )
            con.commit()
        except Exception:
            con.rollback()
            if had_business_index:
                try:
                    con.execute(
                        f"CREATE UNIQUE INDEX {business_index} ON ths_concept_stock_history(trade_date,concept_code,stock_code)"
                    )
                except Exception:
                    # Preserve the original repair error; the integrity audit
                    # will explicitly report a missing/rebuildable index.
                    pass
            raise
        return {"trade_date": date_text, "status": snapshot_status, "concepts_repaired": len(codes),
                "concept_count": int(concept_count or 0), "member_count": int(member_count or 0),
                "checkpoint_total": int(total or 0), "checkpoint_success": int(success or 0),
                "verified": verified, "providers": providers}
    finally:
        con.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="kpl_data.duckdb")
    parser.add_argument("--date", required=True, help="storage date, YYYY-MM-DD")
    parser.add_argument("--target", default=DEFAULT_TARGET, help="attached THS Chrome target id")
    parser.add_argument("--codes", default="300891,300900,301365,308753")
    parser.add_argument("--delay-ms", type=int, default=120)
    parser.add_argument("--allow-stale-cache", action="store_true",
                        help="use a recent complete weekly snapshot when the logged-in browser is blocked")
    parser.add_argument("--blockrank-index", default="",
                        help="use THS blockrank directly, CODE=INDEX[,CODE=INDEX]")
    args = parser.parse_args()
    try:
        result = repair(args.db, args.date, args.target, _parse_codes(args.codes), args.delay_ms,
                        allow_stale_cache=args.allow_stale_cache,
                        blockrank_map=_parse_blockrank_map(args.blockrank_index))
    except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
        print(f"THS browser repair failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
