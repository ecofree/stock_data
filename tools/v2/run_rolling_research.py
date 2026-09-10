"""Register first, then run an isolated immutable QLib walk-forward experiment."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.ml.feature_artifacts import resolve_feature_path
from trade_system.v2.rolling_research import register_experiment, run_experiment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command',required=True)
    register = sub.add_parser('register')
    register.add_argument('--features',type=Path,required=True)
    register.add_argument('--plan',type=Path,required=True)
    register.add_argument('--registry',type=Path,required=True)
    run = sub.add_parser('run')
    run.add_argument('--experiment',type=Path,required=True)
    args = parser.parse_args()
    if args.command=='register':
        path = resolve_feature_path(args.features)
        plan = json.loads(args.plan.read_text(encoding='utf-8'))
        print(json.dumps({'experiment':str(register_experiment(args.registry,path,plan))}))
    else:
        result = run_experiment(args.experiment)
        print(json.dumps({k:result[k] for k in ('registration_id','runtime','sampled_rows','aggregate','paired_comparisons',
                                              'excluded_tail','excluded_tail_scored','elapsed_seconds','execution_ready')},ensure_ascii=False))


if __name__=='__main__':
    main()
