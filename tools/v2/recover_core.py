"""Create an offline V2 backup or restore into a new isolated directory."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from trade_system.v2.recovery import backup,restore

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['backup','restore'])
    parser.add_argument('--source',required=True);parser.add_argument('--output',required=True)
    args=parser.parse_args()
    result={'backup':backup,'restore':restore}[args.action](args.source,args.output)
    print(json.dumps({k:v for k,v in result.items() if k not in ('members','source')}))
