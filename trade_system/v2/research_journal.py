"""Append-only judgement versions and explicit before/after-entry classification."""
from copy import deepcopy
from pathlib import Path
import re

import pandas as pd

from .domain import identity
from .gap_evidence import read_json


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
