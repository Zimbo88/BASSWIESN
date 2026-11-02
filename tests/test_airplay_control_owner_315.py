"""Owned IPC acknowledgement tests; no receiver advertisement or radio I/O."""
import asyncio
import json
import os
import time

import pytest

from basswiesn.app.services.airplay_bridge.control_owner import AudioControl, AudioControlRejected
from basswiesn.app.services.airplay_bridge.private_ipc import FileAudioControlPort, publish_control
from basswiesn.app.services.airplay_bridge.native_group import read_native_group
from test_airplay_control_lease_315 import NONCE


class Worker:
    def __init__(self):
        self.now, self.sequence, self.published = 100.0, 0, []
        self.rewrite = lambda ack: ack
        self.missing, self.released, self.paused = 0, False, False

    async def publish(self, raw):
        self.command = json.loads(raw)
        self.published.append(self.command)

    async def acknowledgement(self):
        if self.missing:
            self.missing -= 1
            raise FileNotFoundError
        self.now += .001
        self.sequence += 1
        self.released = self.command['allow_audio'] and not self.paused
        return json.dumps(self.rewrite(dict(version=1, nonce=NONCE, sequence=self.sequence,
            observed_monotonic=self.now, command_sequence=self.command['sequence'],
            hold_epoch=self.command['hold_epoch'], allow_audio=self.command['allow_audio'],
            audio_released=self.released))).encode()

    def owner(self, **kw):
        return AudioControl(NONCE, self, clock=lambda: self.now, **kw)


def test_exact_worker_ack_precedes_hold_success_and_pause_is_not_false_release():
    worker = Worker()
    worker.missing = 2
    async def run():
        owner = worker.owner()
        assert not (await owner.hold()).audio_released
        assert (await owner.release()).audio_released
        assert not (await owner.hold()).audio_released
        assert [c['hold_epoch'] for c in worker.published] == [1, 1, 2]
        worker.paused = True
        assert not (await owner.release()).audio_released
        assert (await owner.pulse()).allow_audio
    asyncio.run(run())


@pytest.mark.parametrize('bad', [
    {'nonce': 'a'*32}, {'version': True}, {'extra': 1}, {'audio_released': 0},
    {'audio_released': True}, {'allow_audio': True}, {'hold_epoch': True},
    {'command_sequence': True}, {'command_sequence': 20}, {'hold_epoch': 20},
    {'observed_monotonic': 1}, {'observed_monotonic': float('nan')},
])
def test_bad_receipt_is_terminal_not_write_permission(bad):
    worker = Worker()
    worker.rewrite = lambda ack: {**ack, **bad}
    async def run():
        owner = worker.owner()
        with pytest.raises(AudioControlRejected):
            await owner.hold()
        with pytest.raises(AudioControlRejected):
            await owner.release()
        assert len(worker.published) == 1
    asyncio.run(run())


def test_an_old_ack_never_acknowledges_a_new_command():
    worker = Worker()
    worker.rewrite = lambda ack: {**ack, 'command_sequence': 0}
    async def run():
        with pytest.raises(AudioControlRejected):
            await worker.owner(timeout=.05).hold()
        assert not worker.released
    asyncio.run(run())


def test_expired_owner_cannot_be_revived_by_living_worker():
    worker = Worker()
    async def run():
        owner = worker.owner()
        await owner.hold()
        worker.now += 6
        with pytest.raises(AudioControlRejected):
            await owner.pulse()
        assert len(worker.published) == 1
    asyncio.run(run())


def test_source_preparation_requires_current_held_acknowledgement():
    worker = Worker()
    async def run():
        owner = worker.owner()
        with pytest.raises(AudioControlRejected):
            owner.require_held()
        await owner.hold()
        owner.require_held()
        await owner.release()
        with pytest.raises(AudioControlRejected):
            owner.require_held()
        await owner.hold()
        owner.require_held()
        worker.now += 6
        with pytest.raises(AudioControlRejected):
            owner.require_held()
        with pytest.raises(AudioControlRejected):
            await owner.release()
    asyncio.run(run())


def test_revocation_during_ack_blocks_following_release():
    worker = Worker()
    owner = worker.owner()
    def revoke(ack):
        owner.revoke()
        return ack
    worker.rewrite = revoke
    async def run():
        with pytest.raises(AudioControlRejected):
            await owner.hold()
        with pytest.raises(AudioControlRejected):
            await owner.release()
    asyncio.run(run())


def test_hung_and_cancelled_ipc_are_bounded_and_terminal():
    async def run():
        for cancel in (False, True):
            worker = Worker()
            reached = asyncio.Event()
            async def hung(_):
                reached.set()
                await asyncio.Event().wait()
            worker.publish = hung
            owner = worker.owner(timeout=.05)
            task = asyncio.create_task(owner.hold())
            await reached.wait()
            if cancel:
                task.cancel()
            with pytest.raises(asyncio.CancelledError if cancel else AudioControlRejected):
                await task
            with pytest.raises(AudioControlRejected):
                await owner.release()
    asyncio.run(run())


def test_private_control_atomic_file_never_follows_target_symlink(tmp_path):
    root = tmp_path / 'private'
    root.mkdir(mode=0o700)
    other = tmp_path / 'unrelated'
    other.write_bytes(b'preserve')
    (root/'controller.json').symlink_to(other)
    publish_control(root, b'new control')
    assert other.read_bytes() == b'preserve'
    assert read_native_group(root/'controller.json') == b'new control'
    assert (root/'controller.json').stat().st_mode & 0o777 == 0o600
    assert sorted(p.name for p in root.iterdir()) == ['controller.json']
    alias = tmp_path / 'alias'
    alias.symlink_to(root)
    with pytest.raises(OSError):
        publish_control(alias, b'not allowed')
    root.chmod(0o755)
    with pytest.raises(ValueError):
        publish_control(root, b'not allowed')


def test_real_local_ipc_exchange(tmp_path):
    tmp_path.chmod(0o700)
    async def run():
        port = FileAudioControlPort(tmp_path)
        owner = AudioControl(NONCE, port)
        async def worker():
            seq = 0
            while True:
                try:
                    value = json.loads(read_native_group(tmp_path/'controller.json'))
                except FileNotFoundError:
                    await asyncio.sleep(.001)
                    continue
                seq += 1
                ack = dict(version=1, nonce=value['nonce'], sequence=seq,
                    observed_monotonic=time.monotonic(), command_sequence=value['sequence'],
                    hold_epoch=value['hold_epoch'], allow_audio=value['allow_audio'],
                    audio_released=value['allow_audio'])
                file = tmp_path/'ack.tmp'
                fd = os.open(file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, 'w') as stream:
                    json.dump(ack, stream)
                file.replace(tmp_path/'controller-ack.json')
                await asyncio.sleep(.005)
        task = asyncio.create_task(worker())
        try:
            assert not (await owner.hold()).audio_released
            assert (await owner.release()).audio_released
            assert not (await owner.hold()).audio_released
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
