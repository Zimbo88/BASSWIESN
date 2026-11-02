"""A crashed test runner is not a killed behavioral mutation."""
import pytest
from tools.check_safety_mutations import ROOT, TARGETS, expected_outcome

pytestmark = pytest.mark.unit


def test_every_fault_seed_has_one_current_valid_source_anchor():
    # Fail quickly on refactoring drift, instead of calling a missing seed a
    # detected behavioral fault or discovering it midway through the long gate.
    for target_name, target in TARGETS.items():
        path = ROOT / (target.module.replace('.', '/') + '.py')
        source = path.read_text()
        for name, (old, new) in target.mutations.items():
            assert source.count(old) == 1, (target_name, name)
            compile(source.replace(old, new, 1), str(path), 'exec')


@pytest.mark.parametrize("name,code,tests,errors,failures,expected", [
    ("control", 0, 10, 0, 0, True),
    ("control", 1, 10, 0, 1, False),
    ("fault", 1, 10, 0, 1, True),
    ("fault", 0, 10, 0, 0, False),
    ("fault", 1, 10, 1, 1, False),
    ("fault", 2, 10, 0, 1, False),
    ("fault", 1, 0, 0, 0, False),
    ("control", 0, 0, 0, 0, False),
    ("fault", 5, 0, 0, 0, False),
])
def test_only_real_assertion_failures_count_as_detected_faults(name, code, tests, errors, failures, expected):
    assert expected_outcome(name, code, tests, errors, failures) is expected
