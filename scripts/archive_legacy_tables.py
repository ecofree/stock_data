"""Read-only inventory or a new verified export; never DROP source tables.

Name prefixes are inventory hints only. An explicit table allowlist is required
for copying. Constraints/index SQL is retained as metadata, never executed.
Successful data export is NOT permission or full-schema restore acceptance for
source retirement. No existing archive can be overwritten.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import duckdb


ROOT=Path(__file__).resolve().parents[1]
PREFIXES=('legacy_qds_', '_dedupe_archive_', '_corrupt_')


def quote(value):
    return '"'+value.replace('"','""')+'"'


def find_tables(con):
    names=[r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='BASE TABLE' ORDER BY table_name").fetchall()]
    return [(name,con.execute('SELECT count(*) FROM '+quote(name)).fetchone()[0])
            for name in names if name.startswith(PREFIXES)]


def copy_verified(db_path, tables, output):
    source=Path(db_path).resolve()
    target=Path(output).resolve()
    if not source.is_file() or target.exists():
        raise ValueError('existing source and a new archive directory required')
    if not tables or len(tables)!=len(set(tables)) or len(tables)>100 or not all(re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',t) for t in tables):
        raise ValueError('explicit bounded table allowlist required')
    manifest={'source':str(source),'tables':{},'source_deleted':False,
              'scope':'verified_data_export_not_source_retirement','schema_restore_verified':False}
    with duckdb.connect(str(source),read_only=True) as con:
        available={r[0] for r in con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='main' AND table_type='BASE TABLE'").fetchall()}
        if set(tables)-available:
            raise ValueError('unknown source table; nothing copied')
        target.mkdir(parents=True,exist_ok=False)
        con.execute('BEGIN TRANSACTION')
        try:
            for name in tables:
                sql_name=quote(name)
                parquet=target/(name+'.parquet')
                # COPY writes only a new external file; the source DB is opened
                # READ_ONLY and held at one consistent transaction snapshot.
                con.execute(f'COPY (SELECT * FROM {sql_name}) TO ? (FORMAT PARQUET)',[str(parquet)])
                difference=con.execute(f'''SELECT count(*) FROM (
                    (SELECT * FROM {sql_name} EXCEPT ALL SELECT * FROM read_parquet(?))
                    UNION ALL
                    (SELECT * FROM read_parquet(?) EXCEPT ALL SELECT * FROM {sql_name}))''',
                    [str(parquet),str(parquet)]).fetchone()[0]
                if difference:
                    raise ValueError('archive data multiset mismatch')
                columns=con.execute(f'DESCRIBE SELECT * FROM {sql_name}').fetchall()
                copied_columns=con.execute('DESCRIBE SELECT * FROM read_parquet(?)',[str(parquet)]).fetchall()
                if [(r[0],r[1]) for r in columns]!=[(r[0],r[1]) for r in copied_columns]:
                    raise ValueError('archive column type mismatch')
                with parquet.open('rb') as stream:
                    digest=hashlib.file_digest(stream,'sha256').hexdigest()
                manifest['tables'][name]={'rows':con.execute(f'SELECT count(*) FROM {sql_name}').fetchone()[0],
                    'file':parquet.name,'sha256':digest,'columns':columns,
                    'create_sql':con.execute('SELECT sql FROM duckdb_tables() WHERE schema_name=\'main\' AND table_name=?',[name]).fetchone()[0],
                    'indexes':[r[0] for r in con.execute('SELECT sql FROM duckdb_indexes() WHERE schema_name=\'main\' AND table_name=?',[name]).fetchall()]}
            con.execute('COMMIT')
        except BaseException as exc:
            con.execute('ROLLBACK')
            (target/'FAILED.json').write_text(json.dumps({'error_type':type(exc).__name__,'source_deleted':False}),encoding='utf-8')
            raise
    with (target/'completed.json').open('x',encoding='utf-8') as stream:
        json.dump(manifest,stream,ensure_ascii=True,sort_keys=True)
    return manifest


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',default=str(ROOT/'kpl_data.duckdb'))
    parser.add_argument('--execute',action='store_true',help='Retired destructive option; always rejected')
    parser.add_argument('--tables',nargs='+')
    parser.add_argument('--copy-to')
    args=parser.parse_args()
    if args.execute:
        parser.error('source deletion is retired; copy/verify/restore and separate retirement approval required')
    if args.copy_to:
        result=copy_verified(args.db,args.tables,args.copy_to)
        print(json.dumps({'scope':result['scope'],'copied_tables':len(result['tables']),'source_deleted':False}))
    else:
        if not Path(args.db).is_file():
            parser.error('source database does not exist')
        with duckdb.connect(args.db,read_only=True) as con:
            print(json.dumps({'scope':'inventory_hints_not_deletion_approval','tables':find_tables(con)}))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
