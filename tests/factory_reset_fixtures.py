"""Private synthetic baseline files for offline reset tests."""
import pytest
from basswiesn.app.config import get_settings
from basswiesn.app.services import factory_reset
from basswiesn.app.services.setup_rebuild import backup
from basswiesn.app.services.setup_rebuild.backup import baseline_root, write_artifact, write_json_artifact


@pytest.fixture(autouse=True)
def reset_backup_directory(tmp_path, monkeypatch):
    settings = get_settings().model_copy(update={'data_dir': tmp_path})
    monkeypatch.setattr(backup, 'get_settings', lambda: settings)
    monkeypatch.setattr(factory_reset, 'get_settings', lambda: settings)


def make_reset_baseline(device):
    root = baseline_root('offline-test', device.device_id)
    raw = {'info.xml': device.info_xml, 'networkInfo.xml': '<networkInfo/>',
           'sources.xml': '<sources/>', 'presets.xml': '<presets/>',
           'now_playing.xml': '<nowPlaying source="STANDBY"/>', 'volume.xml': '<volume/>',
           'getZone.xml': '<zone/>'}
    hashes = {name: write_artifact(root, name, xml).sha256 for name, xml in raw.items()}
    manifest = write_json_artifact(root, 'backup-manifest.json', {'artifacts': hashes, 'rollback_scope': 'ROUTING_ONLY'})
    hashes['backup-manifest.json'] = manifest.sha256
    return {'verified': True, 'backup_path': str(root), 'sha256': hashes, 'rollback_scope': 'ROUTING_ONLY'}
