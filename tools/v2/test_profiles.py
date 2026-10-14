"""Test workload classification. Release remains the complete data safety gate.

Quick is only a feedback subset. History/stress are explicit targeted workloads,
not permission to omit them from the release gate. Unknown new tests default to
release. Frozen research remains mandatory in its own dependency profile/CI job.
"""
from pathlib import PurePosixPath

PROFILES = ('all', 'quick', 'release', 'history', 'research', 'stress')
RESEARCH_NODE = 'tests/test_research_delivery.py::test_prediction_and_contributions_use_frozen_feature_order'
STRESS_NODES = frozenset({
    'tests/test_journal_index.py::test_long_history_reads_do_not_enumerate_event_directories',
    'tests/test_tushare_history.py::test_large_repeated_valuation_batch_preserves_keys_clocks_and_rollback',
})
QUICK_FILES = frozenset({
    'test_config.py', 'test_os_handle_lock.py', 'test_task_set_retirement.py',
    'test_native_process.py', 'test_verification_hygiene.py',
    'test_valuation_intake_admission.py', 'test_valuation_assessment_scope.py',
    'test_artifact_retention.py', 'test_test_profiles.py', 'test_gate_contract.py',
    'test_backup_retention_policy.py', 'test_pre_push_evidence.py',
    'test_architecture_research_contract.py', 'test_source_authority.py',
})


def group(nodeid, fixtures=()):
    node = nodeid.replace('\\', '/').split('[', 1)[0]
    filename = PurePosixPath(node.split('::')[0]).name
    if filename == 'test_research_runtime_contract.py' or node == RESEARCH_NODE:
        return 'research'
    if node in STRESS_NODES:
        return 'stress'
    if 'frozen_recovery' in fixtures:
        return 'history'
    return 'release'


def selected(profile, nodeid, fixtures=()):
    if profile not in PROFILES:
        raise ValueError('explicit known test profile required')
    category = group(nodeid, fixtures)
    if profile == 'all':
        return True
    if profile == 'release':
        return category != 'research'  # Includes every history/stress safety case.
    if profile == 'quick':
        return category != 'research' and PurePosixPath(nodeid.replace('\\', '/').split('::')[0]).name in QUICK_FILES
    return category == profile
