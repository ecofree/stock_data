"""Isolated source-release activation/rollback; never rewrites live tasks or data."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.publisher import safe_child, publish, read_current
from trade_system.v2.domain import canonical, file_hash


def stage(archive,destination):
    destination=Path(destination).resolve()
    destination.mkdir(parents=True,exist_ok=False)
    with zipfile.ZipFile(archive) as bundle:
        entries=bundle.infolist()
        if len(entries)>300 or sum(e.file_size for e in entries)>50000000:
            raise ValueError('release size budget exceeded')
        if len({e.filename for e in entries})!=len(entries):raise ValueError('duplicate release member')
        for entry in entries:
            if entry.is_dir() or (entry.external_attr>>16)&0o170000==0o120000:
                raise ValueError('only ordinary release files accepted')
            path=safe_child(destination,entry.filename)
            path.parent.mkdir(parents=True,exist_ok=True)
            with path.open('xb') as out:out.write(bundle.read(entry))
    manifest=json.loads((destination/'research-release.json').read_text())
    if set(p.relative_to(destination).as_posix() for p in destination.rglob('*') if p.is_file())!=set(manifest['files'])|{'research-release.json'}:
        raise ValueError('release membership mismatch')
    for name,digest in manifest['files'].items():
        if file_hash(safe_child(destination,name))!=digest:raise ValueError('release hash mismatch')
    return {'directory':str(destination),'archive_sha256':file_hash(archive),'files':len(entries)}


def rehearse(previous,candidate,output,runtime,workspace):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=False)
    workspace=Path(workspace).resolve(strict=True)
    pointers=['research-current.json','research-candidate.json','prediction-current.json']
    before={p:file_hash(workspace/p) for p in pointers if (workspace/p).exists()}
    events_before={p.relative_to(workspace).as_posix():file_hash(p) for p in (workspace/'notes').rglob('*.json')}
    releases={}
    for name,archive in [('previous',previous),('candidate',candidate)]:
        info=stage(archive,output/name)
        entry=Path(info['directory'])/'run_research.py'
        result=subprocess.run([str(runtime),'-I',str(entry),'status','--output',str(workspace)],capture_output=True,text=True,encoding='utf-8',timeout=90)
        (output/(name+'-status.log')).write_text(result.stdout+result.stderr,encoding='utf-8')
        if result.returncode:raise ValueError(name+' release status failed')
        rendered=subprocess.run([str(runtime),'-I',str(entry),'render','--output',str(workspace),
            '--destination',str(output/(name+'-render'))],capture_output=True,text=True,encoding='utf-8',timeout=90)
        (output/(name+'-render.log')).write_text(rendered.stdout+rendered.stderr,encoding='utf-8')
        if rendered.returncode:raise ValueError(name+' release render failed')
        info['render']=json.loads(rendered.stdout)
        releases[name]=info
    events=[]
    for generation,name in enumerate(['previous','candidate','previous'],1):
        receipt=publish(output/'deployment','rehearsal-'+str(generation),
            {'release.json':canonical(releases[name]).encode()},generation=generation)
        manifest,files=read_current(output/'deployment')
        if json.loads(files['release.json'])!=releases[name]:raise ValueError('activation verification failed')
        events.append({'generation':manifest['generation'],'release':name,'manifest_sha256':receipt['manifest_sha256']})
    after={p:file_hash(workspace/p) for p in before}
    if after!=before:raise ValueError('frozen workspace pointers changed')
    events_after={p.relative_to(workspace).as_posix():file_hash(p) for p in (workspace/'notes').rglob('*.json')}
    if events_after!=events_before:raise ValueError('human records changed during release rehearsal')
    report={'scope':'isolated_release_artifact_switch_and_rollback_not_windows_task_cutover',
        'events':events,'frozen_pointers_unchanged':True,'releases':releases,
        'human_event_files_checked':len(events_before),'human_event_bytes_unchanged':True,
        'new_schema_display_requires_reader':'0.3.11; older readers retain but do not display attention subdirectories',
        'rollback_verified':True,'production_cutover':False,'execution_ready':False}
    (output/'rehearsal.json').write_text(canonical(report),encoding='utf-8')
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for name in ['previous','candidate','output','runtime','workspace']:p.add_argument('--'+name,required=True,type=Path)
    a=p.parse_args();r=rehearse(a.previous,a.candidate,a.output,a.runtime,a.workspace)
    print(json.dumps({'rollback_verified':r['rollback_verified'],'production_cutover':False}))
