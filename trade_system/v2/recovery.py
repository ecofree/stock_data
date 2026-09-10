"""Offline, owner-locked full-file backup and isolated restore. Never delete source."""
import json
from pathlib import Path
import shutil

import duckdb
from trade_system.file_lock import FileLock
from .domain import canonical,identity,file_hash,now_utc


def catalog(db):
    with duckdb.connect(str(db),read_only=True,config={'enable_external_access':False}) as con:
        if not con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='v2_schema'").fetchone()[0]:
            raise ValueError('V2 database required')
        tables=con.execute("SELECT schema_name,table_name,sql FROM duckdb_tables() WHERE NOT internal ORDER BY schema_name,table_name").fetchall()
        counts={schema+'.'+name:con.execute('SELECT count(*) FROM "'+schema.replace('"','""')+'"."'+name.replace('"','""')+'"').fetchone()[0] for schema,name,_ in tables}
        return {'tables':tables,'rows':counts,
                'indexes':con.execute('SELECT schema_name,index_name,sql FROM duckdb_indexes() ORDER BY 1,2').fetchall(),
                'views':con.execute('SELECT schema_name,view_name,sql FROM duckdb_views() WHERE NOT internal ORDER BY 1,2').fetchall(),
                'sequences':con.execute('SELECT schema_name,sequence_name,sql FROM duckdb_sequences() ORDER BY 1,2').fetchall()}


def check_manifest(folder):
    folder=Path(folder).resolve(strict=True)
    manifest=json.loads((folder/'completed.json').read_text(encoding='utf-8'))
    if identity({k:v for k,v in manifest.items() if k!='manifest_id'})!=manifest.get('manifest_id'):
        raise ValueError('backup manifest checksum mismatch')
    paths=list(folder.rglob('*'))
    if len(paths)>100002 or any(p.is_symlink() for p in paths):
        raise ValueError('bounded non-symlink backup required')
    actual={p.relative_to(folder).as_posix():file_hash(p) for p in paths if p.is_file() and p!=folder/'completed.json'}
    if actual!=manifest['members']:
        raise ValueError('backup member checksum or membership mismatch')
    if manifest['scope']!='offline_v2_full_backup_not_account_acceptance':
        raise ValueError('wrong backup scope')
    if identity(catalog(folder/'database.duckdb'))!=manifest['catalog_hash']:
        raise ValueError('backup schema/row controls mismatch')
    return manifest


def backup(source,output):
    source=Path(source).resolve(strict=True);output=Path(output).resolve()
    if output.exists() or not source.is_file():
        raise ValueError('existing source and new output directory required')
    raw=source.parent/(source.name+'.raw')
    if output==raw or raw in output.parents or raw.is_symlink():
        raise ValueError('backup must not mutate or follow the source raw archive')
    with FileLock(source.with_suffix(source.suffix+'.owner.guard')):
        if source.with_suffix(source.suffix+'.wal').exists():
            raise ValueError('WAL present; require clean writer shutdown, never remove WAL')
        # Retain a native read lock for the entire source copy, including raw archives.
        with duckdb.connect(str(source),read_only=True,config={'enable_external_access':False}):
            controls=catalog(source)
            raw_files=list(raw.iterdir()) if raw.exists() else []
            if len(raw_files)>100000 or any(not p.is_file() or p.is_symlink() or len(p.name)!=64 for p in raw_files):
                raise ValueError('bounded flat raw hash archive required')
            size=source.stat().st_size+sum(p.stat().st_size for p in raw_files)
            if shutil.disk_usage(output.parent).free<size*1.2:
                raise ValueError('insufficient free space for complete backup')
            output.mkdir(parents=True,exist_ok=False)
            shutil.copyfile(source,output/'database.duckdb')
            (output/'database.duckdb.raw').mkdir()
            for path in raw_files:
                if file_hash(path)!=path.name:
                    raise ValueError('source raw archive corrupted; source unchanged')
                shutil.copyfile(path,output/'database.duckdb.raw'/path.name)
            if file_hash(source)!=file_hash(output/'database.duckdb'):
                raise ValueError('database copy checksum mismatch; incomplete directory retained')
            members={p.relative_to(output).as_posix():file_hash(p) for p in output.rglob('*') if p.is_file()}
            body={'scope':'offline_v2_full_backup_not_account_acceptance','created_at':now_utc().isoformat(),
                  'catalog_hash':identity(controls),'members':members,'source_modified':False,
                  'source':str(source),'contains_potential_account_data':True,'execution_ready':False}
            manifest={**body,'manifest_id':identity(body)}
            if identity(catalog(output/'database.duckdb'))!=body['catalog_hash']:
                raise ValueError('restored catalog mismatch')
            (output/'completed.json').write_text(canonical(manifest),encoding='utf-8')
    return check_manifest(output)


def restore(folder,output):
    folder=Path(folder).resolve(strict=True);output=Path(output).resolve()
    if output.exists() or folder in output.parents:
        raise ValueError('restore requires a new isolated directory; overwrite is forbidden')
    original=check_manifest(folder)
    size=sum((folder/name).stat().st_size for name in original['members'])
    if shutil.disk_usage(output.parent).free<size*1.2:
        raise ValueError('insufficient free space for restore')
    output.mkdir(parents=True,exist_ok=False)
    for name in original['members']:
        # Membership was derived from paths below the validated backup directory.
        target=output/name
        target.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(folder/name,target)
    if {name:file_hash(output/name) for name in original['members']}!=original['members']:
        raise ValueError('restore bytes differ; incomplete restore retained')
    if identity(catalog(output/'database.duckdb'))!=original['catalog_hash']:
        raise ValueError('restore structural controls differ')
    from .storage import Store
    from .paper_storage import load_paper
    summaries={}
    with Store(output/'database.duckdb') as store:
        exists=store.con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='paper_account'").fetchone()[0]
        if exists:
            for account, in store.con.execute('SELECT account_id FROM paper_account ORDER BY account_id').fetchall():
                summaries[account]=identity(load_paper(store,account,full_replay=True).summary())
    result={'scope':'isolated_restore_full_paper_replay_not_real_account_reconciliation',
            'backup_manifest_id':original['manifest_id'],'catalog_hash':original['catalog_hash'],
            'paper_replay_hashes':summaries,'source_deleted':False,'execution_ready':False}
    (output/'restored.json').write_text(canonical(result),encoding='utf-8')
    return result


def main():
    import argparse
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=['backup','restore'])
    parser.add_argument('--source',required=True)
    parser.add_argument('--output',required=True)
    args=parser.parse_args()
    try:
        result=(backup if args.operation=='backup' else restore)(args.source,args.output)
        print(canonical({k:v for k,v in result.items() if k not in ('members','source')}))
        return 0
    except Exception as exc:
        print(canonical({'ok':False,'error_type':type(exc).__name__,'execution_ready':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
