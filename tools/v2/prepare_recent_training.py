"""Freeze the approved recent receipt configuration before running model fitting."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2 import research_campaign as campaign
from trade_system.v2.domain import identity
from trade_system.v2.gap_evidence import read_json, write_json
from trade_system.v2.research_recent import load_receipts


def prepare(root, base, receipts, output):
    root = Path(root).resolve()
    config = read_json(base)[0]
    reg, members, _, _ = campaign.replay(receipts)
    capture = reg['config']
    config.update(start=capture['start'], end=capture['end'],
        sources={'receipts':{'path':str(Path(receipts).resolve().relative_to(root)), 'manifest_id':identity(members)}},
        final_refit={'train_sessions':60,'valid_sessions':15,'variant':'price_baseline'})
    _, _, summary = load_receipts(config, root)
    if Path(output).exists():
        raise ValueError('new frozen training configuration required')
    write_json(output, config)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', default='config/research_delivery.json')
    parser.add_argument('--receipts', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    import json
    print(json.dumps(prepare(Path.cwd(), args.base, args.receipts, args.output), ensure_ascii=False))
