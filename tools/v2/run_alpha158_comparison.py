"""Freeze and run a separate Alpha158 vs small-price historical diagnostic."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.alpha158_research import expressions
from trade_system.v2.domain import canonical
from trade_system.v2.rolling_research import register_experiment,run_experiment


def run(source,root):
    source=Path(source).resolve(strict=True)
    meta=json.loads(source.with_suffix('.metadata.json').read_text(encoding='utf-8'))
    _,names=expressions()
    plan={'experiment_id':'alpha158-independent-20260911-v1','scope':'historical_research_only',
        'exposure_status':'previously_inspected_not_untouched','evaluation_asof':'2026-09-11T08:00:00+08:00',
        'label_definition':meta['label_definition'],'availability_assumption':meta['availability_assumption'],
        'required_export_contract':{'label_version':'market_session_aligned_v2','adjustment_available':True},
        'max_rows':10000,'train_observations':40,'valid_observations':15,'test_observations':20,
        'excluded_tail_observations':20,'max_folds':2,'num_boost_round':20,'num_threads':2,'seed':20260911,
        'top_k':10,'round_trip_cost_bps':[0,10,30,50],'comparison_baseline':'price_baseline',
        'variants':{'price_baseline':['ret_1d','ret_5d','volatility_5d','volume_z20','turnover_rate','volume_ratio'],
                    'alpha158':names}}
    folder=register_experiment(root,source,plan)
    (folder/'protocol.json').write_text(canonical(plan),encoding='utf-8')
    result=run_experiment(folder)
    return {'experiment':str(folder),'aggregate':result['aggregate'],'paired_comparisons':result['paired_comparisons'],
            'excluded_tail_scored':False,'execution_ready':False}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',required=True);p.add_argument('--registry',required=True)
    a=p.parse_args();print(json.dumps(run(a.source,a.registry)))
