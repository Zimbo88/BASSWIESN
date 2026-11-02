"""Payload-free playback control for one explicitly owned receiver pilot.

These events describe playback, NOT Apple group identity or authenticated
session membership. A terminal pilot cannot be rearmed. The process owner must
create a new lifetime/stream grant for another session; metadata cannot do it.
"""
from dataclasses import dataclass


STATES = frozenset(('WAITING', 'PLAYING', 'PAUSED', 'ENDED'))
CONTROL_CODES = frozenset(('pbeg', 'pend', 'pfls', 'prsm', 'paus', 'pres'))
MAX_COUNTER = 2**53 - 1


def ends_stream(kind: str, code: str) -> bool:
    """Activity-monitor expiry (aend) is not player/session destruction.

    A real paused AP2 session emitted aend without pend. Shairport's activity
    monitor also documents that separate timer. Treating it as teardown closes
    the one-use relay and makes resume impossible. Keep the independent bounded
    silence/process/lease deadlines; activity events cannot renew them.
    """
    return kind == 'ssnc' and code == 'pend'


@dataclass
class PilotPlayback:
    state: str = 'WAITING'
    sequence: int = 0
    buffer_epoch: int = 0

    def apply(self, kind: str, code: str) -> bool:
        """Consume an allowlisted event name, never its untrusted payload.

        AP2 rate changes use paus/pres; classic playback uses pfls/prsm.
        pbeg means player creation, so it must not override an earlier pause.
        abeg/aend are activity, not proof of playback or session teardown.
        """
        if kind != 'ssnc' or code not in CONTROL_CODES or self.state == 'ENDED':
            return False
        if self.sequence >= MAX_COUNTER - 1 or self.buffer_epoch >= MAX_COUNTER - 1:
            self.state = 'ENDED'
        elif ends_stream(kind, code):
            self.state = 'ENDED'
            self.buffer_epoch += 1
        elif code in ('paus', 'pfls'):
            self.state = 'PAUSED'
            self.buffer_epoch += 1
        elif code in ('pres', 'prsm') or self.state == 'WAITING':
            self.state = 'PLAYING'
        self.sequence += 1
        return True

    def snapshot(self) -> dict:
        return {'state': self.state, 'sequence': self.sequence,
                'buffer_epoch': self.buffer_epoch}


class PlaybackCursor:
    """Validate private atomic IPC snapshots from the owned pilot process.

    A pause/resume faster than a reader's poll still advances buffer_epoch,
    forcing pending PCM to be discarded. No timestamp, address or sender ID is
    used as a session identifier. This is not a network-facing trust boundary.
    """

    def __init__(self) -> None:
        self._previous: dict | None = None

    def accept(self, value: dict) -> bool:
        if (type(value) is not dict or set(value) != {'state', 'sequence', 'buffer_epoch'}
                or type(value['state']) is not str or value['state'] not in STATES
                or any(type(value[key]) is not int or not 0 <= value[key] <= MAX_COUNTER
                       for key in ('sequence', 'buffer_epoch'))
                or value['buffer_epoch'] > value['sequence']):
            raise ValueError('invalid playback observation')
        if self._previous is not None:
            if value == self._previous:
                return False
            if (value['sequence'] <= self._previous['sequence']
                    or value['buffer_epoch'] < self._previous['buffer_epoch']
                    or self._previous['state'] == 'ENDED'):
                raise ValueError('stale playback observation')
        self._previous = dict(value)
        return True
