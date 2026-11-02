import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools.build_ssh_client import BINARIES, HELPERS, source_lock
from tools.check_ssh_provenance import component, supplement

pytestmark = pytest.mark.unit
LOCK = Path(__file__).resolve().parents[1] / "tools/openssh-source.json"


@pytest.fixture
def installed(tmp_path):
    hashes = {}
    for directory, names in (("bin", BINARIES), ("libexec", HELPERS)):
        (tmp_path / directory).mkdir()
        for name in names:
            file = tmp_path / directory / name
            file.write_bytes(b"synthetic executable " + name.encode())
            file.chmod(0o755)
            hashes[directory + "/" + name] = hashlib.sha256(file.read_bytes()).hexdigest()
    docs = tmp_path / "share/doc/openssh"
    docs.mkdir(parents=True)
    (docs / "LICENCE").write_text("synthetic test licence")
    (docs / "LICENCE").chmod(0o644)
    manifest = {"format": 1, "version": "10.6p1", "source": source_lock(LOCK),
                "signature_verified": True, "files": hashes}
    (docs / "build-manifest.json").write_text(json.dumps(manifest))
    (docs / "build-manifest.json").chmod(0o644)
    return tmp_path


def test_source_component_is_explicit_and_does_not_claim_a_clean_scan(installed):
    item = component(installed, LOCK)
    assert item["purl"] == "pkg:generic/openssh-portable@10.6p1"
    properties = {p["name"]: p["value"] for p in item["properties"]}
    assert len(json.loads(properties["basswiesn:installed-file-sha256"])) == 9
    assert "not a clean scan" in properties["basswiesn:security-assessment"]


@pytest.mark.parametrize("change", ["bytes", "mode", "symlink", "missing", "extra", "version", "signature"])
def test_component_rejects_changed_binary_or_unbound_manifest(installed, change):
    path = installed / "bin/ssh"
    manifest_path = installed / "share/doc/openssh/build-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "bytes":
        path.write_text("changed executable")
    elif change == "mode":
        path.chmod(0o4755)
    elif change == "symlink":
        path.unlink()
        path.symlink_to("scp")
    elif change == "missing":
        del manifest["files"]["bin/ssh"]
    elif change == "extra":
        manifest["files"]["../../outside"] = "0" * 64
    elif change == "version":
        manifest["version"] = "old"
    else:
        manifest["signature_verified"] = False
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        component(installed, LOCK)


def test_supplement_preserves_scan_inventory_and_attaches_source_dependency(installed):
    original = {"bomFormat": "CycloneDX", "specVersion": "1.6",
                "metadata": {"component": {"bom-ref": "image"}},
                "components": [{"name": "python", "bom-ref": "python"}],
                "dependencies": [{"ref": "image", "dependsOn": ["python"]}]}
    before = copy.deepcopy(original)
    item = component(installed, LOCK)
    result = supplement(original, item)
    assert original == before
    assert result["components"] == [*before["components"], item]
    assert result["dependencies"][0]["dependsOn"] == sorted(["python", item["bom-ref"]])
    with pytest.raises(ValueError):
        supplement(result, item)
