"""Exercise selection safety without collecting or contacting any real hardware."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import pytest_collection_modifyitems

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def selection(monkeypatch):
    for key in ('CI', 'GITHUB_ACTIONS'):
        monkeypatch.delenv(key, raising=False)
    for key, value in {
        'BASSWIESN_HARDWARE_TARGET_ID': 'EXAMPLE',
        'BASSWIESN_HARDWARE_TARGET_IP': '192.0.2.42',
        'BASSWIESN_HARDWARE_MODEL': 'Example Model',
        'BASSWIESN_HARDWARE_FIRMWARE': 'exact.example',
        'BASSWIESN_HARDWARE_VARIANT': 'example',
        'BASSWIESN_HARDWARE_PLATFORM': 'example',
        'BASSWIESN_HARDWARE_CONFIRM': 'BASSWIESN HARDWARE TEST',
    }.items():
        monkeypatch.setenv(key, value)
    return SimpleNamespace(option=SimpleNamespace(markexpr='hardware')), [
        SimpleNamespace(get_closest_marker=lambda name: name == 'hardware')]


@pytest.mark.parametrize('key,value', [
    ('CI', 'true'), ('GITHUB_ACTIONS', 'true'), ('BASSWIESN_HARDWARE_CONFIRM', ''),
    ('BASSWIESN_HARDWARE_TARGET_ID', ''), ('BASSWIESN_HARDWARE_FIRMWARE', '27.*'),
    ('PROTECTED_DEVICE_IDS', ''), ('PROTECTED_DEVICE_IPS', ''),
])
def test_hardware_cannot_bypass_collection_gate(selection, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(pytest.UsageError, match='Hardware tests blocked'):
        pytest_collection_modifyitems(*selection)


@pytest.mark.parametrize('expression', ['', 'unit', 'hardware or unit', 'not browser'])
def test_inherited_approval_cannot_authorize_generic_test_run(selection, expression):
    config, items = selection
    config.option.markexpr = expression
    with pytest.raises(pytest.UsageError):
        pytest_collection_modifyitems(config, items)


def test_explicit_selection_is_only_selection_not_hardware_approval(selection):
    pytest_collection_modifyitems(*selection)


def test_software_collection_does_not_need_hardware_approval():
    pytest_collection_modifyitems(SimpleNamespace(), [])


@pytest.mark.parametrize('script', ['test_fast.sh', 'test_integration.sh', 'test_ui.sh', 'test_release.sh'])
def test_every_software_entrypoint_excludes_hardware_even_with_multiple_markers(script):
    assert 'not hardware' in (ROOT / 'tools' / script).read_text()
