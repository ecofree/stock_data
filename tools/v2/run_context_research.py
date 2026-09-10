"""Frozen real-data snapshot -> context actor -> immutable self-contained review."""
import argparse
import csv
from io import StringIO
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.v2.backup_verify import sha256
from trade_system.v2.legacy_context import load_snapshot
from trade_system.v2.market_context import ContextPolicy
from trade_system.v2.publisher import publish
from trade_system.v2.reporting import render_review
from trade_system.v2.service import Service


def run(source, output, day):
    output.mkdir(parents=True,exist_ok=False)
    source_hash = sha256(source)
    bundle = load_snapshot(source,day,source_sha256=source_hash)
    # Frozen, explicitly experimental parameters: not tuned to this day's
    # candidates, not production strategy thresholds or financial advice.
    policy = ContextPolicy('context-observation-fixture-v1',.95,3,.5,.2,20)
    with Service(output/'research.duckdb') as service:
        receipt = service.submit('context_import',bundle=bundle,policy=policy.__dict__,budget_seconds=120).result(120)
        page = service.submit('review',account_id='real-account-not-provided',context_id=receipt['context_id'],budget_seconds=120).result(120)
    context = page['context']
    page['fact_count'] = len(context['evidence_ids'])
    page['products'] = [dict(dataset=k,unit=p['unit'],semantics=p['metric_definition'],origin=p['origin_family'],
                             consumer='context_observations_only') for k,p in bundle['products'].items()]
    buf = StringIO()
    writer = csv.writer(buf)
    writer.writerow(['instrument','state','theme_ids','execution_ready'])
    for candidate in context['conditional_candidates']:
        writer.writerow([candidate['instrument'],candidate['state'],'|'.join(candidate['theme_ids']),False])
    artifacts = {'review.html':render_review(page).encode(),day+'-conditional-candidates.csv':buf.getvalue().encode('utf-8-sig'),
                 'context.json':json.dumps(context,ensure_ascii=False,indent=2).encode(),
                 'adapter_diagnostics.json':json.dumps(bundle['adapter_diagnostics'],indent=2).encode()}
    publish(output/'reports','snapshot',artifacts,generation=1)
    summary = {'scope':'historical_research_not_live','source_sha256':source_hash,**receipt,
               'market_dimensions':context['market_dimensions'],'diagnostics':context['diagnostics'],
               'adapter_diagnostics':bundle['adapter_diagnostics'],'blockers':context['blockers'],
               'review':str(output/'reports/runs/snapshot/review.html')}
    (output/'verification.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--date',required=True)
    args = parser.parse_args()
    run(args.source.resolve(strict=True),args.output.resolve(),args.date)
