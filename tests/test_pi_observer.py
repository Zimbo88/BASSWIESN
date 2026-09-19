"""Host recorder tests use synthetic files and subprocess mocks only."""
from datetime import UTC, datetime, timedelta
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from tools import pi_observer as observer
from tools.install_observer import UNIT

pytestmark = pytest.mark.unit


def test_redaction_covers_generic_secret_json_and_headers():
    for raw in ("secret: hidden", "Authorization: Bearer hidden", "password=hidden", '"secret": "hidden"', 'token: "hidden"', "-----BEGIN PRIVATE KEY-----"):
        assert "hidden" not in observer.redact(raw)
        assert "omitted" in observer.redact(raw)
    assert observer.redact("https://example.invalid/audio?k=hidden") == "https://example.invalid/audio?[query omitted]"
    assert "hidden" not in observer.redact("https://user:hidden@example.invalid/audio")


def test_rotation_integrity_private_permissions_and_unrelated_files(tmp_path, monkeypatch):
    root=tmp_path/"recordings"
    archive=observer.Archive(root, max_total=1024, min_free=0)
    now=datetime.now(UTC)
    assert archive.append({"value":"safe"*40}, now)
    monkeypatch.setattr(observer, "MAX_SEGMENT", 128)
    unrelated=root/"user.txt"; unrelated.write_text("Keep")
    archive.rotate(now+timedelta(seconds=1))
    files=list(root.glob("sample-*.gz"))
    assert len(files)==1
    payload=gzip.decompress(files[0].read_bytes())
    assert json.loads(payload)["value"]=="safe"*40
    digest=files[0].with_name(files[0].name+".sha256").read_text().split()[0]
    assert hashlib.sha256(files[0].read_bytes()).hexdigest()==digest
    assert files[0].stat().st_mode & 0o077 == 0
    archive.rotate(now+timedelta(days=15))
    assert not list(root.glob("sample-*.gz")) and unrelated.read_text()=="Keep"


def test_no_space_or_symlink_does_not_fall_back(tmp_path, monkeypatch):
    archive=observer.Archive(tmp_path, min_free=512)
    monkeypatch.setattr(observer.shutil, "disk_usage", lambda _:SimpleNamespace(free=100))
    assert not archive.append({"sample":1}, datetime.now(UTC))
    assert not (tmp_path/"current.jsonl").exists()
    target=tmp_path/"not-a-log"; target.write_text("Keep")
    (tmp_path/"current.jsonl").symlink_to(target)
    with pytest.raises(PermissionError): archive.rotate(datetime.now(UTC))
    assert target.read_text()=="Keep"


def test_sampling_is_host_only_bounded_and_excludes_env_and_db(tmp_path, monkeypatch):
    data=tmp_path/"data"; data.mkdir()
    (data/"master.log").write_text("healthy\nsecret: hidden\n")
    (data/"private.env").write_text("DO NOT READ")
    calls=[]
    def run(args):
        calls.append(args)
        assert args[0] in {"docker", "journalctl"}
        if args[0]=="docker": assert args[1:3]==["--host", "unix:///var/run/docker.sock"]
        assert not any(word in args for word in ("exec", "start", "stop", "restart", "rm"))
        return {"output": "a"*12 if "ps" in args else "fixture", "returncode":0}
    monkeypatch.setattr(observer, "host_sample", lambda:{"memory_kib":{"MemAvailable":123}})
    archive=observer.Archive(tmp_path/"out", min_free=0)
    recorder=observer.Observer(data,archive,runner=run)
    assert recorder.sample()
    record=json.loads((archive.root/"current.jsonl").read_text())
    assert record["application_logs"][0]["lines"]==["healthy", "[sensitive log line omitted]"]
    assert "DO NOT READ" not in json.dumps(record)
    assert any("inspect" in call for call in calls)
    assert "RestrictAddressFamilies=AF_UNIX" in UNIT and "IPAddressDeny=any" in UNIT
    assert not recorder.tail_files()


def test_host_process_sample_has_no_command_lines_or_environment(tmp_path):
    proc=tmp_path/"proc"; (proc/"123").mkdir(parents=True)
    (proc/"meminfo").write_text("MemAvailable: 1234 kB\n")
    (proc/"123"/"status").write_text("Name:\tpython3\nPid:\t123\nVmRSS:\t500 kB\n")
    (proc/"123"/"environ").write_text("private")
    sample=observer.host_sample(proc_root=proc,sys_root=tmp_path/"sys")
    assert sample["memory_kib"]["MemAvailable"]==1234
    assert sample["processes"]==[{"Name":"python3", "Pid":"123", "VmRSS":"500 kB"}]
