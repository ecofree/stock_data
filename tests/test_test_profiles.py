import pytest

from tools.v2.test_profiles import PROFILES, RESEARCH_NODE, STRESS_NODES, group, selected


@pytest.mark.parametrize('node,fixtures,expected', [
    ('tests/test_new_unknown.py::test_new_safety_case', (), 'release'),
    ('tests/test_retained_recovery_probe.py::test_original_hash', ('frozen_recovery',), 'history'),
    (next(iter(STRESS_NODES)), (), 'stress'),
    (RESEARCH_NODE, (), 'research'),
    ('tests/test_research_runtime_contract.py::test_fit[fixture]', (), 'research'),
])
def test_each_node_has_one_group_without_dropping_safety(node, fixtures, expected):
    assert group(node, fixtures) == expected
    assert selected('all', node, fixtures)
    assert selected('release', node, fixtures) == (expected != 'research')
    assert sum(selected(p, node, fixtures) for p in ('history', 'stress', 'research')) == (expected != 'release')


def test_quick_is_explicit_subset_and_never_full_release_authority():
    assert selected('quick', 'tests/test_os_handle_lock.py::test_lock')
    assert not selected('quick', 'tests/test_new_unknown.py::test_new_safety_case')
    assert selected('release', 'tests/test_new_unknown.py::test_new_safety_case')
    for node in STRESS_NODES:
        assert selected('release', node) and selected('stress', node)
    with pytest.raises(ValueError):
        selected('silent-skip', 'tests/test_p0_observation.py::test_required_day')
    assert len(PROFILES) == 6
