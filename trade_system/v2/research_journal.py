"""Append-only judgement versions and explicit before/after-entry classification."""
from copy import deepcopy
from pathlib import Path
import re

import pandas as pd

from .domain import identity
from .gap_evidence import read_json


def retry_note(output, request_id, command):
    """Recover an acknowledged or interrupted request without making a second event.

    The event itself is the authority; no separately committed receipt index.
    Called under judgement.guard, including on retries after a process restart.
    """
    if not request_id:
        return None
    if not isinstance(request_id, str) or not re.fullmatch('[a-f0-9]{32}', request_id):
        raise ValueError('invalid judgement request id')
    for path in (Path(output)/'notes').glob('*.json'):
        note = verify_note(read_json(path)[0])
        if note.get('request_id') == request_id:
            if note.get('command_id') != identity(command):
                raise ValueError('request id already used for different judgement; start a new draft')
            return note['note_id']
    return None


def append_note(output, note):
    """Flush a complete event before atomically exposing its final filename."""
    import os
    import uuid
    from .domain import canonical
    folder = Path(output)/'notes'
    folder.mkdir(exist_ok=True)
    target = folder/(note['note_id']+'.json')
    if target.exists():
        if verify_note(read_json(target)[0]) != note:
            raise ValueError('existing judgement differs')
        return
    staging = folder/('.pending-'+uuid.uuid4().hex)
    # Incomplete staging files remain invisible and retained for diagnosis.
    with staging.open('x', encoding='utf-8') as stream:
        stream.write(canonical(note))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(staging, target)


def read_note(output, note_id):
    if not isinstance(note_id, str) or not re.fullmatch('[a-f0-9]{64}', note_id):
        raise ValueError('invalid judgement receipt id')
    note=verify_note(read_json(Path(output)/'notes'/(note_id+'.json'))[0])
    if note['note_id']!=note_id:raise ValueError('judgement receipt identity mismatch')
    return note


def save_review(output, values):
    """An explicit human observation, separate from computed future-price labels."""
    from trade_system.file_lock import FileLock
    from .domain import now_utc
    output=Path(output)
    with FileLock(output/'judgement.guard'):
        note=read_note(output,values.get('note_id'))
        request_id=values.get('request_id')
        if not isinstance(request_id,str) or not re.fullmatch('[a-f0-9]{32}',request_id):
            raise ValueError('invalid review request id')
        if values.get('conclusion') not in ('pending','triggered','not_triggered','unclear'):
            raise ValueError('explicit condition review required')
        for key in ('reviewer','evidence'):
            if not isinstance(values.get(key),str) or not 1<=len(values[key].strip())<=4000:
                raise ValueError('explicit bounded human review required')
        command={key:values[key] for key in ('note_id','conclusion','reviewer','evidence')}
        folder=output/'notes'/'reviews'
        folder.mkdir(exist_ok=True)
        for path in folder.glob('*.json'):
            old=read_review(output,path.stem)
            if old['request_id']==request_id:
                if old['command_id']!=identity(command):raise ValueError('review request reused with different content')
                return old['review_id']
        review=dict(command,request_id=request_id,command_id=identity(command),
            prediction_id=note['prediction_id'],instrument=note['instrument'],
            received_at=now_utc().isoformat(),identity_scope='caller_declared_not_authenticated',
            scope='human_condition_observation_not_price_label_or_account_acceptance',execution_ready=False)
        review['review_id']=identity(review)
        # Reuse the single-event durable writer without exposing review JSON as notes.
        import os
        import uuid
        from .domain import canonical
        staging=folder/('.pending-'+uuid.uuid4().hex)
        with staging.open('x',encoding='utf-8') as stream:
            stream.write(canonical(review));stream.flush();os.fsync(stream.fileno())
        os.replace(staging,folder/(review['review_id']+'.json'))
        return review['review_id']


def read_review(output,review_id):
    if not isinstance(review_id,str) or not re.fullmatch('[a-f0-9]{64}',review_id):
        raise ValueError('invalid review receipt id')
    review=read_json(Path(output)/'notes'/'reviews'/(review_id+'.json'))[0]
    if review.get('review_id')!=review_id or review_id!=identity({k:v for k,v in review.items() if k!='review_id'}):
        raise ValueError('human review changed')
    note=read_note(output,review['note_id'])
    if any(note[k]!=review[k] for k in ('prediction_id','instrument')):
        raise ValueError('human review parent differs')
    return review


def verify_note(note):
    if note['note_id']!=identity({k:v for k,v in note.items() if k!='note_id'}):raise ValueError('judgement record changed')
    return note


def find_prediction(output, prediction_id):
    if not isinstance(prediction_id,str) or not re.fullmatch('[a-f0-9]{64}',prediction_id):raise ValueError('invalid prediction id')
    for path in (Path(output)/'observations').glob('*/predictions.json'):
        value=read_json(path)[0]
        if value.get('prediction_id')==prediction_id:
            if prediction_id!=identity({k:v for k,v in value.items() if k!='prediction_id'}):raise ValueError('archived prediction changed')
            return value
    raise ValueError('unknown archived prediction')


def timing(received_at, entry_at):
    if not entry_at:return 'timing_unverified'
    return 'before_entry' if pd.Timestamp(received_at)<pd.Timestamp(entry_at) else 'after_entry'


def bind(output,note,prediction,supersedes=''):
    note.update(prediction_date=prediction['date'],model_id=prediction.get('model_id'),
        timing=timing(note['received_at'],prediction.get('scheduled_entry_at')),
        scheduled_entry_at=prediction.get('scheduled_entry_at'),supersedes=supersedes or None)
    if supersedes:
        if not re.fullmatch('[a-f0-9]{64}',supersedes):raise ValueError('invalid parent note id')
        path=Path(output)/'notes'/(supersedes+'.json')
        if not path.is_file():raise ValueError('unknown judgement parent')
        previous=verify_note(read_json(path)[0])
        if any(previous[k]!=note[k] for k in ('prediction_id','instrument','operator')):
            raise ValueError('revision must preserve prediction, security and declared author')
        if pd.Timestamp(previous['received_at'])>pd.Timestamp(note['received_at']):raise ValueError('revision predates parent')
        for item in path.parent.glob('*.json'):
            if read_json(item)[0].get('supersedes')==supersedes:raise ValueError('parent already revised; refresh and use latest version')


def annotate(notes):
    notes=[dict(verify_note(n)) for n in notes]
    superseded={n.get('supersedes') for n in notes}
    return [dict(n,is_latest=n['note_id'] not in superseded) for n in notes]


def attach(reviews,notes):
    result=deepcopy(reviews)
    for review in result:
        review['judgement_count']=sum(n['prediction_id']==review['prediction_id'] for n in notes)
        for row in review['rows']:
            bound=[dict(n) for n in notes if n['prediction_id']==review['prediction_id'] and n['instrument']==row['instrument']]
            entry=row.get('entry_date')
            for note in bound:
                note['verified_timing']=timing(note['received_at'],entry+'T09:30:00+08:00' if entry else None)
            before=[n for n in bound if n['verified_timing']=='before_entry']
            row['judgements']=bound
            row['prospective_judgement']=max(before,key=lambda n:n['received_at'])['note_id'] if before else None
            row['invalidation_review']='manual_review_required' if bound else 'no_human_judgement'
    return result
