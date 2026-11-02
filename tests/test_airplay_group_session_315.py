"""Synthetic native snapshots, not an Apple or radio hardware acceptance."""
import json

import pytest

from basswiesn.app.services.airplay_bridge.group_session import (
    GroupSession, GroupSessionRejected, ReceiverBinding, ReceiverSample,
)

MASTER, MEMBER, OTHER = "AP2-MASTER", "AP2-MEMBER", "AP2-OTHER"
GROUP, SEPARATE = "a" * 64, "b" * 64


class Inputs:
    def __init__(self, ids=(MASTER, MEMBER, OTHER)):
        self.now, self.seq = 100.0, 0
        self.bindings = tuple(ReceiverBinding(key, f"{index:032x}", index + 20, 99.0)
                              for index, key in enumerate(ids, 1))
        self.records = {item.device_id: dict(version=1, nonce=item.nonce, pid=item.pid,
            revision=1, connection=0, state="IDLE", group_hash="", event_monotonic=99.0)
            for item in self.bindings}
        self.session = GroupSession(self.bindings, master=MASTER, started=self.now)

    def frame(self, states=None, *, advance=0.1):
        self.now += advance
        self.seq += 1
        for key, group in (states or {}).items():
            item = self.records[key]
            item.update(revision=item['revision'] + 1, connection=1 if group else 0,
                        state="GROUPED" if group else "IDLE", group_hash=group,
                        event_monotonic=self.now)
        self.session.renew_owner(sequence=self.seq, observed_monotonic=self.now, now=self.now)
        return {key: ReceiverSample(json.dumps(item).encode(), self.seq, self.now, True)
                for key, item in self.records.items()}


def test_exact_matching_native_membership_and_no_title_or_ip_heuristic():
    r = Inputs()
    plan = r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP, OTHER: SEPARATE}), now=r.now)
    assert plan.members == (MEMBER,) and plan.status == "ACTIVE"
    plan = r.session.observe(r.frame({MEMBER: ""}), now=r.now)
    assert plan.members == () and plan.status == "ACTIVE"


def test_master_departure_requires_handoff_not_reuse_of_old_relay():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP}), now=r.now)
    plan = r.session.observe(r.frame({MASTER: ""}), now=r.now)
    assert plan.status == "HANDOFF_REQUIRED" and not plan.members
    with pytest.raises(GroupSessionRejected):
        r.session.observe(r.frame({MASTER: GROUP}), now=r.now)


def test_normal_end_is_terminal():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP}), now=r.now)
    assert r.session.observe(r.frame({MASTER: ""}), now=r.now).status == "STOPPED"
    with pytest.raises(GroupSessionRejected):
        r.session.observe(r.frame(), now=r.now)


def test_initial_idle_is_waiting_not_permission_to_build_a_zone():
    r = Inputs()
    assert r.session.observe(r.frame(), now=r.now).status == "WAITING"


def test_missing_member_observation_is_not_an_implicit_leave():
    r = Inputs()
    samples = r.frame({MASTER: GROUP, MEMBER: GROUP})
    del samples[MEMBER]
    with pytest.raises(GroupSessionRejected):
        r.session.observe(samples, now=r.now)
    with pytest.raises(GroupSessionRejected):
        r.session.observe(r.frame(), now=r.now)


@pytest.mark.parametrize('failure', ['nonce', 'pid', 'dead', 'old_heartbeat', 'extra_receiver',
                                    'malformed', 'clock', 'bool_sequence'])
def test_bad_native_or_supervisor_state_fails_closed(failure):
    r = Inputs()
    samples = r.frame({MASTER: GROUP, MEMBER: GROUP})
    sample = samples[MEMBER]
    raw = json.loads(sample.native)
    if failure in {'nonce', 'pid'}:
        raw[failure] = "f" * 32 if failure == 'nonce' else 909
    samples[MEMBER] = ReceiverSample(b'not json' if failure == 'malformed' else json.dumps(raw).encode(),
        True if failure == 'bool_sequence' else sample.heartbeat_sequence,
        r.now - 6 if failure == 'old_heartbeat' else sample.observed_monotonic,
        failure != 'dead')
    if failure == 'extra_receiver':
        samples['UNAUTHORIZED'] = sample
    with pytest.raises(ValueError):
        r.session.observe(samples, now=float('nan') if failure == 'clock' else r.now)


def test_receiver_heartbeat_cannot_renew_dead_controller():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP}), now=r.now)
    r.now += 6
    fresh = {key: ReceiverSample(json.dumps(value).encode(), 40, r.now, True)
             for key, value in r.records.items()}
    with pytest.raises(GroupSessionRejected):
        r.session.observe(fresh, now=r.now)
    with pytest.raises(GroupSessionRejected):
        r.session.renew_owner(sequence=50, observed_monotonic=r.now, now=r.now)


def test_new_owner_heartbeat_cannot_revive_expired_lease():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP}), now=r.now)
    with pytest.raises(GroupSessionRejected):
        r.session.renew_owner(sequence=2, observed_monotonic=r.now+6, now=r.now+6)


def test_unchanged_native_event_survives_with_fresh_independent_heartbeats():
    r = Inputs()
    first = r.session.observe(r.frame({MASTER: GROUP}), now=r.now)
    for _ in range(30):
        assert r.session.observe(r.frame(advance=1), now=r.now) is first
    assert r.records[MASTER]['revision'] == 2


def test_terminal_teardown_refresh_is_live_but_cannot_become_a_new_session():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP}), now=r.now)
    terminal = r.session.observe(r.frame({MASTER: '', MEMBER: ''}), now=r.now)
    for _ in range(10):
        assert r.session.refresh(r.frame(advance=1), now=r.now) is terminal
        r.session.require_live(now=r.now)
    with pytest.raises(GroupSessionRejected):
        r.session.refresh(r.frame({MEMBER: GROUP}), now=r.now)


@pytest.mark.parametrize('first', [MASTER, MEMBER])
def test_staggered_disconnects_preserve_terminal_plan_without_regranting(first):
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP}), now=r.now)
    observed = r.session.observe(r.frame({first: ''}), now=r.now)
    if first == MEMBER:
        assert observed.status == 'ACTIVE' and observed.members == ()
        terminal = r.session.observe(r.frame({MASTER: ''}), now=r.now)
        assert terminal.status == 'STOPPED'
    else:
        terminal = observed
        assert terminal.status == 'HANDOFF_REQUIRED'
        assert r.session.refresh(r.frame({MEMBER: ''}), now=r.now) is terminal
        # Draining is cleanup, not a new STOPPED plan or an audio grant.
        assert terminal.status == 'HANDOFF_REQUIRED'
    assert terminal.members == ()
    for _ in range(3):
        assert r.session.refresh(r.frame(), now=r.now) is terminal
        r.session.require_live(now=r.now)


@pytest.mark.parametrize('change', ['member_reconnect', 'member_group', 'master_resurrection'])
def test_terminal_drain_rejects_new_activity(change):
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP}), now=r.now)
    terminal = r.session.observe(r.frame({MASTER: ''}), now=r.now)
    if change == 'member_reconnect':
        assert r.session.refresh(r.frame({MEMBER: ''}), now=r.now) is terminal
    identity = MASTER if change == 'master_resurrection' else MEMBER
    previous = r.records[identity]
    previous.update(state='GROUPED', group_hash=SEPARATE if change == 'member_group' else GROUP,
                    connection=1 if change == 'member_group' else 2,
                    revision=previous['revision'] + 1, event_monotonic=r.now)
    with pytest.raises(GroupSessionRejected):
        r.session.refresh(r.frame(), now=r.now)
    assert r.session.plan is terminal


def test_terminal_drain_still_requires_fresh_member_heartbeat():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP}), now=r.now)
    r.session.observe(r.frame({MASTER: ''}), now=r.now)
    samples = r.frame({MEMBER: ''})
    sample = samples[MEMBER]
    samples[MEMBER] = ReceiverSample(sample.native, sample.heartbeat_sequence, r.now - 6, True)
    with pytest.raises(GroupSessionRejected):
        r.session.refresh(samples, now=r.now)


@pytest.mark.parametrize('replacement', ['connection', 'group_hash'])
def test_new_connection_or_changed_master_group_needs_new_grant(replacement):
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP}), now=r.now)
    r.records[MASTER].update(revision=3, event_monotonic=r.now)
    r.records[MASTER][replacement] = 2 if replacement == 'connection' else SEPARATE
    with pytest.raises(GroupSessionRejected, match='NEW_RELAY_GRANT'):
        r.session.observe(r.frame(), now=r.now)


def test_initial_unassigned_is_waiting_not_permission_to_build_a_zone():
    r = Inputs()
    r.records[MASTER].update(state='UNASSIGNED', connection=1)
    plan = r.session.observe(r.frame({MEMBER: GROUP}), now=r.now)
    assert plan.status == 'WAITING' and plan.members == ()
    assert r.session.refresh(r.frame(), now=r.now) is plan


def test_idle_unassigned_grouped_startup_preserves_the_original_connection():
    r = Inputs()
    waiting = r.session.observe(r.frame(), now=r.now)
    r.records[MASTER].update(state='UNASSIGNED', connection=1, revision=2,
                             event_monotonic=r.now)
    assert r.session.observe(r.frame(), now=r.now) is waiting
    plan = r.session.observe(r.frame({MASTER: GROUP, MEMBER: GROUP}), now=r.now)
    assert plan.status == 'ACTIVE' and plan.members == (MEMBER,)


def test_initial_unassigned_cannot_be_replaced_by_a_new_connection():
    r = Inputs()
    r.records[MASTER].update(state='UNASSIGNED', connection=1)
    assert r.session.observe(r.frame(), now=r.now).status == 'WAITING'
    r.records[MASTER].update(state='GROUPED', group_hash=GROUP, connection=2, revision=2,
                             event_monotonic=r.now)
    with pytest.raises(GroupSessionRejected, match='NEW_RELAY_GRANT_REQUIRED'):
        r.session.observe(r.frame(), now=r.now)


def test_unassigned_after_established_group_never_reuses_the_grant():
    r = Inputs()
    r.session.observe(r.frame({MASTER: GROUP}), now=r.now)
    r.records[MASTER].update(state='UNASSIGNED', group_hash='', revision=3,
                             event_monotonic=r.now)
    with pytest.raises(GroupSessionRejected, match='MASTER_GROUP_UNKNOWN'):
        r.session.observe(r.frame(), now=r.now)
    with pytest.raises(GroupSessionRejected):
        r.session.observe(r.frame({MASTER: GROUP}), now=r.now)


@pytest.mark.parametrize('failure', ['native', 'heartbeat'])
def test_initial_unassigned_still_requires_verified_fresh_input(failure):
    r = Inputs()
    r.records[MASTER].update(state='UNASSIGNED', connection=1)
    samples = r.frame()
    sample = samples[MASTER]
    samples[MASTER] = ReceiverSample(b'not json' if failure == 'native' else sample.native,
        sample.heartbeat_sequence, r.now - 6 if failure == 'heartbeat' else r.now, True)
    with pytest.raises(GroupSessionRejected):
        r.session.observe(samples, now=r.now)


def test_binding_rejects_duplicate_ids_nonces_or_unknown_master():
    binding = ReceiverBinding(MASTER, 'a' * 32, 30, 99)
    for bindings, master in [((binding, binding), MASTER), ((binding,), MEMBER),
                            ((binding, ReceiverBinding(MEMBER, 'a' * 32, 31, 99)), MASTER)]:
        with pytest.raises(GroupSessionRejected):
            GroupSession(bindings, master=master, started=100)
