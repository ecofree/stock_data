"""Read-only owned-test retention plan; no deletion or lease reclamation command."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from trade_system.artifact_retention import inventory, retention_plan, owned_test_root, DEFAULT_LIMITS, DEFAULT_DAYS  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--archive-index', type=Path, required=True)
    parser.add_argument('--deployment-state', type=Path, required=True)
    parser.add_argument('--restore-proofs', type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('new evidence file required')
    index = json.loads(args.archive_index.read_text('utf-8'))
    state = json.loads(args.deployment_state.read_text('utf-8'))
    references = []
    path_keys = {'path', 'archive', 'objects', 'manifest', 'container_catalog', 'extra_objects'}

    def walk(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in path_keys and isinstance(child, str):
                    path = Path(child)
                    references.append(path if path.is_absolute() else args.archive_index.parent / path)
                elif isinstance(child, (dict, list)):
                    walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(index)
    for value in (state['runtime_root'], state['source']['directory'],
                  state['collector']['database'], state['publication']['workspace'],
                  state['compatibility_rollback']['directory']):
        references.append(Path(value))
    proofs = json.loads(args.restore_proofs.read_text('utf-8')) if args.restore_proofs else {}
    root = owned_test_root()
    result = retention_plan(root, referenced_paths=references, restore_proofs=proofs)
    result.update(inventory=inventory(root), hard_limits=DEFAULT_LIMITS,
                  prior_checkout_temp_inventory=inventory(ROOT / 'tmp/test-runs'),
                  prior_checkout_temp_policy='retain_until_explicit_verified_archive_retirement',
                  successful_run_retention_days=DEFAULT_DAYS, failed_run_retention_days=30,
                  automatic_deletion=False, raw_human_and_original_receipts_not_eligible=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps({'output': str(args.output), 'runs': result['inventory']['runs'],
                      'deletions': 0, 'automatic_deletion': False}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
