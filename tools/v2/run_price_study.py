"""Run the approved price-only comparison with a sealed factor donor batch."""
from pathlib import Path
import argparse
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.price_study import run

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='reports/research-delivery')
    parser.add_argument('--donor',required=True)
    args=parser.parse_args()
    print(json.dumps(run(Path.cwd(),args.output,args.donor),ensure_ascii=False))
