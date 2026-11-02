"""Synthetic original-restore tests: no proprietary images and no hardware IO."""
import asyncio
from dataclasses import replace
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from basswiesn.app.services.setup_rebuild import presentation_restore as r
from basswiesn.app.services.setup_rebuild import presentation_model as m
from basswiesn.app.services.setup_rebuild.backup import write_artifact
from basswiesn.app.services.setup_rebuild.presentation_transport import envelope
from test_setup_presentation import rig, snapshot, TARGET, archive  # noqa: F401
from test_setup_transaction_ownership import prepared, OwnedAdapter

BOOT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def restore_rig(rig, monkeypatch):
    service, row, transport, client = rig
    base = m.OLD_BASES[0]
    control = f'APP={r.policy.APP_STOCK}\nRC_BEFORE=absent\n'.encode()
    files = dict(zip(m.FILES, (b"patched application", b"battery", b"logo", b"logo")))
    files.update({"mnt/nv/rc.local": b"owned hook", base.lstrip('/')+'/control.sh': control,
                  base.lstrip('/')+'/backup/splash.original': b"original splash"})
    monkeypatch.setattr(r.policy, 'SPLASH_STOCK', m.digest(b"original splash"))
    before = snapshot(boot=BOOT, base=base, control=m.digest(control), rc=m.digest(b"owned hook"), bind_ro="yes")
    for path, field in zip(m.FILES, ('app','battery','splash_a','splash_b')):
        before[field] = m.digest(files[path])
    transport.snap = before.copy()
    monkeypatch.setattr(r, 'classify', lambda *args: 'existing')
    original = transport.exchange
    async def exchange(operation, **kw):
        if operation == 'backup':
            transport.calls.append(operation)
            return archive(files)
        if operation == 'restore_original':
            transport.calls.append(operation)
            if transport.fail == operation:
                raise m.PresentationError('LOST_REPLY')
            return b'RESTORE_CONFIGURED_REBOOT_REQUIRED\n'
        if operation == 'restore_archive':
            transport.calls.append(operation)
            if transport.fail == operation:
                raise m.PresentationError('LOST_REPLY')
            transport.snap['base'] = 'none'
            return b'ARCHIVED\n'
        return await original(operation, **kw)
    transport.exchange = exchange
    return r.PresentationRestore(service), row, transport, files


def boot_original(instance, row, transport):
    plan = instance.state(row)['plan']
    transport.snap.update(boot="22222222-2222-2222-2222-222222222222", app=plan['app'],
                          rc=plan['rc'], splash_a=r.policy.SPLASH_STOCK,
                          splash_b=r.policy.SPLASH_STOCK, bind_ro='no', pending='no')


def test_restore_full_transaction_preserves_battery_and_requires_new_boot(restore_rig):
    instance, row, transport, _ = restore_rig
    async def run():
        assert (await instance.prepare(row))['restore_prepared']
        assert (await instance.activate(row))['original_verified'] is False
        with pytest.raises(m.PresentationError, match='READBACK'):
            await instance.verify(row)
        boot_original(instance, row, transport)
        assert (await instance.verify(row))['original_verified'] is True
    asyncio.run(run())
    assert instance.state(row)['phase'] == 'VERIFIED'
    assert transport.calls.count('restore_original') == 1
    assert transport.calls.count('restore_archive') == 1


@pytest.mark.parametrize('failure', [False, True])
def test_verified_restore_reports_cleanup_separately(restore_rig, monkeypatch, failure):
    from basswiesn.app.services.setup_rebuild import presentation_cleanup
    instance,row,transport,_=restore_rig
    operation=AsyncMock(side_effect=m.PresentationError('UNSAFE') if failure else None,
                        return_value={'cleanup_verified':True,'free_kib':30000})
    monkeypatch.setattr(presentation_cleanup,'cleanup',operation)
    async def run():
        await instance.prepare(row);await instance.activate(row)
        boot_original(instance,row,transport)
        return await instance.verify(row)
    result=asyncio.run(run())
    assert result['original_verified'] is True
    assert result['presentation_cleanup']['cleanup_verified'] is (not failure)
    operation.assert_awaited_once()
    assert instance.state(row)['phase']=='VERIFIED'


@pytest.mark.parametrize('field,value', [('app','f'*64),('battery','f'*64),('rc','f'*64),
                                      ('splash_a','f'*64),('splash_b','f'*64),('bind_ro','yes'),('pending','yes')])
def test_restore_divergence_never_archives_or_claims_success(restore_rig, field, value):
    instance, row, transport, _ = restore_rig
    async def run():
        await instance.prepare(row); await instance.activate(row)
        boot_original(instance, row, transport)
        transport.snap[field] = value
        with pytest.raises(m.PresentationError, match='READBACK'):
            await instance.verify(row)
    asyncio.run(run())
    assert 'restore_archive' not in transport.calls


@pytest.mark.parametrize('operation', ['restore_original','restore_archive'])
def test_lost_reply_is_durable_and_never_replayed(restore_rig, operation):
    instance, row, transport, _ = restore_rig
    async def run():
        await instance.prepare(row)
        transport.fail = operation
        if operation == 'restore_archive':
            await instance.activate(row); boot_original(instance,row,transport)
        action = instance.activate if operation == 'restore_original' else instance.verify
        with pytest.raises(m.PresentationError, match='LOST_REPLY'): await action(row)
        with pytest.raises(m.PresentationError): await action(row)
    asyncio.run(run())
    assert transport.calls.count(operation) == 1
    assert instance.state(row)['phase'] in ('WRITE_INTENT','ARCHIVE_INTENT')


def test_corrupt_backup_stops_before_restore_write(restore_rig):
    instance,row,transport,_ = restore_rig
    asyncio.run(instance.prepare(row))
    write_artifact(instance.service.root(row),'restore.private.tar',b'corrupt')
    with pytest.raises(m.PresentationError,match='BACKUP_CORRUPT'):
        asyncio.run(instance.activate(row))
    assert 'restore_original' not in transport.calls


def test_changed_current_state_stops_before_write(restore_rig):
    instance,row,transport,_ = restore_rig
    asyncio.run(instance.prepare(row))
    transport.snap['battery'] = 'f'*64
    with pytest.raises(m.PresentationError,match='PRECONDITION'):
        asyncio.run(instance.activate(row))
    assert 'restore_original' not in transport.calls


@pytest.mark.parametrize('change', ['controller','splash','startup','app'])
def test_original_backup_validation(restore_rig, change):
    instance,row,transport,files = restore_rig
    prefix = transport.snap['base'].lstrip('/')+'/'
    if change=='controller': files[prefix+'control.sh'] += b'#changed'
    if change=='splash': files[prefix+'backup/splash.original']=b'bad'
    if change=='startup': files[prefix+'backup/rc.original']=b'unexpected'
    if change=='app':
        files[prefix+'control.sh']=b'APP=unknown\n'
        transport.snap['control']=m.digest(files[prefix+'control.sh'])
    with pytest.raises(m.PresentationError):
        r.restoration_plan(transport.snap,files)


def test_legacy_non_display_app_changes_are_preserved(restore_rig):
    _,_,transport,files=restore_rig
    p=transport.snap['base'].lstrip('/')+'/control.sh'
    files[p]=f'APP={r.policy.APP_LEGACY}\nRC_BEFORE=absent\n'.encode()
    transport.snap['control']=m.digest(files[p])
    assert r.restoration_plan(transport.snap,files)['app']==r.policy.APP_LEGACY


def test_shell_has_no_warm_app_replacement_or_battery_write(restore_rig):
    _,_,transport,files=restore_rig
    plan=r.restoration_plan(transport.snap,files)
    for op in ('restore_original','restore_archive'):
        command=envelope(TARGET)+r.restore_command(op,transport.snap,plan)
        checked=subprocess.run(['/bin/sh','-n'],input=command,text=True,capture_output=True)
        assert checked.returncode==0,checked.stderr
        assert 'umount' not in command and 'kill' not in command
        assert 'cp /opt/Bose/BoseApp' not in command
        assert 'mv /opt/Bose/BoseApp' not in command
        assert '> /opt/Bose/BatteryMonitor' not in command
        assert 'rm -rf' not in command
    with pytest.raises(m.PresentationError):
        r.restore_command('restore_original',transport.snap,plan|{'base':'/etc'})
    with pytest.raises(m.PresentationError):
        r.restore_command('restore_original',transport.snap,plan|{'boot':'x;reboot'})


class RestoreAdapter(OwnedAdapter):
    async def presentation_preflight(self,row): return await self._result('presentation_preflight',{})
    async def restore_prepare(self,row): return await self._result('restore_prepare',{'restore_prepared':True})
    async def restore_activate(self,row): return await self._result('restore_activate',{'restore_configured':True})
    async def restore_verify(self,row): return await self._result('restore_verify',{'original_verified':True})


def test_coordinator_restore_never_routes_plays_or_changes_preferences():
    adapter=RestoreAdapter();coordinator,job=prepared(adapter,presentation_mode='original')
    result=asyncio.run(coordinator.execute(job))
    assert result['status']=='completed',result
    assert adapter.calls==['identify','presentation_preflight','backup','restore_prepare','restore_activate','reboot','reconnect','restore_verify']
    assert result['devices'][0]['evidence']['original_verified'] is True
    with pytest.raises(ValueError,match='Original-Rückbau'):
        asyncio.run(coordinator.rollback(job))


@pytest.mark.parametrize('step', ['restore_prepare','restore_activate','restore_verify'])
def test_coordinator_false_result_cannot_be_success(step):
    adapter=RestoreAdapter()
    setattr(adapter,step,AsyncMock(return_value={}))
    coordinator,job=prepared(adapter,presentation_mode='original')
    assert asyncio.run(coordinator.execute(job))['status']=='failed'
    assert 'route' not in adapter.calls and 'playback_test' not in adapter.calls


@pytest.mark.parametrize('ack', [None,False,'yes',1])
def test_api_requires_separate_restore_ack(monkeypatch,ack):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from basswiesn.app.routers import setup_rebuild as router
    from basswiesn.app.services.setup_rebuild.server_target import ServerTarget
    candidate=SimpleNamespace(simulated=False,audio_safety_locked=False,public_dict=lambda:{'presentation_supported':True})
    monkeypatch.setattr(router,'selected_setup_devices',lambda *a,**k:[(SimpleNamespace(device_id='AA11BB22CC33'),candidate)])
    monkeypatch.setattr(router,'resolve_server_target',lambda p:ServerTarget('192.0.2.10',1328,1516,1860))
    monkeypatch.setattr(router,'_ensure_test_mode_simulation_device',lambda db:None)
    def forbidden(): raise AssertionError('No job without original-restore acknowledgement')
    monkeypatch.setattr(router,'get_coordinator',forbidden)
    app=FastAPI();app.include_router(router.router)
    with TestClient(app) as client:
        response=client.post('/api/setup/rebuild/start',json={'device_ids':['AA11BB22CC33'],'presentation_mode':'original','restore_acknowledged':ack})
    assert response.status_code==400
    assert 'Originaldarstellung' in response.json()['detail']
