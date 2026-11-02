"""Offline cleanup safety and real shell checks against disposable fixtures."""
import asyncio
import io
import json
from pathlib import Path
import subprocess
import tarfile

import pytest

from basswiesn.app.services.setup_rebuild import presentation_cleanup as c
from basswiesn.app.services.setup_rebuild import presentation_model as m
from basswiesn.app.services.setup_rebuild import presentation_transport as t
from basswiesn.app.services import radio_patch_policy as policy
from test_setup_presentation import rig, snapshot, TARGET  # noqa: F401

PLAN = {"base": m.BASE, "boot": "11111111-1111-1111-1111-111111111111",
        "app": policy.APP_STOCK, "battery": policy.BATTERY_STOCK,
        "rc": "absent"}
CURRENT = snapshot(boot="22222222-2222-2222-2222-222222222222")
DIRECTORY = m.BASE + ".restored-" + PLAN["boot"]
NAME = Path(DIRECTORY).name


def fixture_archive(name=NAME, extra=None):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as tar:
        for p in (name, name+"/backup"):
            entry=tarfile.TarInfo(p); entry.type=tarfile.DIRTYPE; tar.addfile(entry)
        for p,data in ((name+"/BoseApp.candidate",b"synthetic application"), (name+"/backup/splash.original", b"synthetic logo")):
            entry=tarfile.TarInfo(p); entry.size=len(data);tar.addfile(entry,io.BytesIO(data))
        if extra is not None:
            tar.addfile(extra, io.BytesIO(b"x"*extra.size))
    return output.getvalue()


@pytest.mark.parametrize("name", ["/mnt/nv", "/", "/mnt/nv/BoseApp-Persistence", "/mnt/nv/IoTCerts",
                                  m.BASE, m.BASE+"-upload", DIRECTORY+"/../other", "/mnt/nv/unknown-trial"])
def test_cleanup_never_accepts_broad_active_or_unknown_targets(name):
    with pytest.raises(m.PresentationError):c.allowed_directory(name,PLAN)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "device", "parent", "duplicate", "absolute"])
def test_tar_rejects_links_special_paths_and_duplicates(kind):
    entry=tarfile.TarInfo(NAME+"/extra")
    if kind=="symlink":entry.type=tarfile.SYMTYPE;entry.linkname="/etc/passwd"
    if kind=="hardlink":entry.type=tarfile.LNKTYPE;entry.linkname=NAME+"/BoseApp.candidate"
    if kind=="fifo":entry.type=tarfile.FIFOTYPE
    if kind=="device":entry.type=tarfile.CHRTYPE
    if kind=="parent":entry.name=NAME+"/../escape"
    if kind=="absolute":entry.name="/etc/passwd"
    if kind=="duplicate":entry.name=NAME+"/BoseApp.candidate"
    with pytest.raises(m.PresentationError):c.manifest(fixture_archive(extra=entry),NAME)


@pytest.mark.parametrize("change", [{"base":m.BASE},{"app":"a"*64},{"battery":"b"*64},
                                     {"boot":PLAN["boot"]},{"bind_ro":"yes"},{"pending":"yes"}])
def test_restore_readback_required_even_before_backup(change):
    with pytest.raises(m.PresentationError):c.cleanup_command("cleanup_backup",CURRENT|change,PLAN,DIRECTORY)


def deletion_tail(tmp_path, entries):
    command=c.cleanup_command("cleanup_remove",CURRENT,PLAN,DIRECTORY,entries)
    assert "rm -rf" not in command and "mount -o" not in command and "reboot" not in command
    assert subprocess.run(["sh","-n"],input=t.envelope(TARGET)+command,text=True,capture_output=True).returncode==0
    # Execute the actual generated file-validation/unlink phase, but only inside
    # a pytest-owned directory. Firmware/process guards are separately asserted.
    tail=command[command.index("cd /mnt/nv\n"):].replace("/mnt/nv",str(tmp_path))
    return "set -eu\nhash() { sha256sum \"$1\" | cut -d' ' -f1; }\nregular() { [ -f \"$1\" ] && [ ! -L \"$1\" ]; }\n"+tail


@pytest.mark.parametrize("change", ["none","modified","extra","symlink","hardlink"])
def test_real_shell_removes_only_exact_backed_up_tree(tmp_path,change):
    raw=fixture_archive();entries=c.manifest(raw,NAME)
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:tar.extractall(tmp_path,filter="data")
    target=tmp_path/NAME/"BoseApp.candidate"
    if change=="modified":target.write_bytes(b"changed")
    if change=="extra":(tmp_path/NAME/"extra").write_bytes(b"unknown")
    if change=="symlink":target.unlink();target.symlink_to(tmp_path/"untouched")
    if change=="hardlink":(tmp_path/"external").hardlink_to(target)
    (tmp_path/"untouched").write_bytes(b"keep")
    result=subprocess.run(["sh"],input=deletion_tail(tmp_path,entries),text=True,capture_output=True)
    assert (result.returncode==0)==(change=="none"),result.stderr
    assert (tmp_path/"untouched").read_bytes()==b"keep"
    if change=="none":assert not (tmp_path/NAME).exists()
    else:assert (tmp_path/NAME/"backup/splash.original").exists()


@pytest.mark.parametrize("failure", ["none","corrupt_backup","changed_state","lost_reply","bad_readback"])
def test_transaction_backup_before_write_and_uncertainty_never_replayed(rig,monkeypatch,failure):
    service,row,transport,_=rig
    calls=[];inspections=0
    async def exchange(operation,**kw):
        nonlocal inspections
        calls.append(operation)
        if operation=="inspect":
            inspections+=1
            snap=CURRENT.copy()
            if failure=="changed_state" and inspections==2:snap["battery"]="e"*64
            return "".join(f"{k}={v}\n" for k,v in snap.items()).encode()
        if operation=="cleanup_backup":return fixture_archive()
        if operation=="cleanup_remove":
            root=service.root(row)/"cleanup"/NAME
            assert c.manifest((root/"package.private.tar").read_bytes(),NAME)
            assert json.loads((root/"receipt.json").read_text())["phase"]=="DELETE_INTENT"
            if failure=="lost_reply":raise m.PresentationError("LOST_REPLY")
            return b"REMOVED\n"
        if operation=="cleanup_absent":return b"present" if failure=="bad_readback" else b"ABSENT\n"
        raise AssertionError(operation)
    transport.exchange=exchange
    if failure=="corrupt_backup":monkeypatch.setattr(c,"read_private",lambda *a:b"corrupt")
    if failure=="none":
        result=asyncio.run(c.cleanup(service,row,PLAN,DIRECTORY))
        assert result["cleanup_verified"] is True and result["removed_files"]==2
    else:
        with pytest.raises(m.PresentationError):asyncio.run(c.cleanup(service,row,PLAN,DIRECTORY))
        if failure in ("corrupt_backup","changed_state"):assert "cleanup_remove" not in calls
    if failure in ("none","lost_reply","bad_readback"):
        with pytest.raises(m.PresentationError,match="EXISTING_INTENT"):
            asyncio.run(c.cleanup(service,row,PLAN,DIRECTORY))
        assert calls.count("cleanup_remove")==1


def test_upload_duplicate_removed_only_after_verified_package_move():
    cmd=t.mutation_command("stage",CURRENT,{p:"a"*64 for p in ("control.sh","rc.installed","BoseApp.candidate","splash.candidate")},"b"*64)
    assert cmd.index('mv package "$base"') < cmd.index('rm '+m.BASE+'-upload/incoming.tar')
    assert '[ "$(hash incoming.tar)" = '+"b"*64+' ]' in cmd
    assert subprocess.run(["sh","-n"],input=cmd,text=True,capture_output=True).returncode==0
