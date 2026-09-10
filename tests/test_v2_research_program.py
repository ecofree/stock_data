from copy import deepcopy
import json

import pytest

from trade_system.v2 import research_program as program
from trade_system.v2.domain import identity
from trade_system.v2.gap_evidence import read_json
from trade_system.v2.portfolio_replay import POLICY
from trade_system.v2.portfolio_diagnostic import SCOPE


def fixture():
    days=['2026-04-01','2026-04-02','2026-04-03']
    bundle={'predictions':{v:[[days[0],'000001',1]] for v in ('a','b','c')},
        'calendar':days,'identity_map':{'000001':'SZ.000001'},
        'bars':[{'date':d,'instrument':'SZ.000001','open_fen':1000,'close_fen':1100,'factor':'1'} for d in days[1:]]}
    base={'scope':SCOPE,'exposure_status':'previously_inspected_not_untouched',
        'fill_assumption':'unconstrained_next_open_and_due_close','calendar_assumption':'SSE_sessions_shared_not_exchange_certified',
        'missing_mark_policy':'stop_account','corporate_action_policy':'stop_on_held_factor_change',
        'initial_cash_fen':1000000,'top_k':1,'max_positions':2,'ticket_bps':5000,'buy_lot':100,'hold_sessions':2,
        't_plus_sessions':1,'fees':{'version':'fixture','commission_bps':'0','minimum_commission_fen':0,'transfer_bps':'0','sell_tax_bps':'0'}}
    return {'bundle':bundle,'base_policy':base,'fields':{'repair_asof':'2026-09-10T01:00:00Z','cases':[]},'prior_actions':[]}


def registered(tmp_path,monkeypatch):
    payload=fixture()
    monkeypatch.setattr(program,'source_inputs',lambda p:deepcopy(payload))
    folder=tmp_path/'program'
    program.register({'policy':POLICY},folder,clock=lambda:'2026-09-10T02:00:00Z')
    return folder,payload


def test_whole_variant_run_recomputes_and_cannot_overwrite(tmp_path,monkeypatch):
    folder,_=registered(tmp_path,monkeypatch)
    actual=program.execute(folder)
    assert set(actual)=={'a','b','c'}
    assert program.read_run(folder,recompute=True)==actual
    with pytest.raises(FileExistsError):program.execute(folder)


def test_frozen_input_change_refused(tmp_path,monkeypatch):
    folder,_=registered(tmp_path,monkeypatch)
    data=read_json(folder/'inputs.json')[0];data['bundle']['predictions'].pop('c')
    (folder/'inputs.json').write_text(json.dumps(data),encoding='utf-8')
    with pytest.raises(ValueError,match='inputs changed'):program.execute(folder)


def test_resealed_false_return_fails_recomputation(tmp_path,monkeypatch):
    folder,_=registered(tmp_path,monkeypatch);program.execute(folder)
    out=folder/'run';result=read_json(out/'a.json')[0];result['portfolio_return']='999'
    (out/'a.json').write_text(json.dumps(result),encoding='utf-8')
    manifest=read_json(out/'completed.json')[0];manifest.pop('manifest_id')
    manifest['artifact_hashes']['a.json']=program.file_hash(out/'a.json')
    (out/'completed.json').write_text(json.dumps({**manifest,'manifest_id':identity(manifest)}),encoding='utf-8')
    with pytest.raises(ValueError,match='reproducible'):program.read_run(folder,recompute=True)


def test_source_change_and_preknowledge_registration_rejected(tmp_path,monkeypatch):
    folder,payload=registered(tmp_path,monkeypatch)
    with pytest.raises(ValueError,match='precedes'):program.register({'policy':POLICY},tmp_path/'early',clock=lambda:'2026-09-09T02:00:00Z')
    payload['prior_actions'].append({'changed':True})
    with pytest.raises(ValueError,match='parents changed'):program.load(folder)
