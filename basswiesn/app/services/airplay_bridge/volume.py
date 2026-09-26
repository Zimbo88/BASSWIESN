"""Pure AirPlay slider projection. No network, radio access or implicit writes.

The documented pvol source value is slider-linear -30..0; -144 is mute.
This projection is a control position, not acoustic dB calibration of Bose.
The owner supplies an explicit maximum and must verify hardware readback.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class VolumeIntent:
    requested: int
    applied: int
    muted: bool
    limited: bool


def project_volume(airplay_db: float, *, maximum: int) -> VolumeIntent:
    if type(maximum) is not int or not 1 <= maximum <= 100:
        raise ValueError('explicit radio volume maximum 1..100 required')
    if (type(airplay_db) not in (int, float) or not math.isfinite(airplay_db)
            or not (-30 <= airplay_db <= 0 or airplay_db == -144)):
        raise ValueError('invalid AirPlay volume')
    muted = airplay_db == -144
    requested = 0 if muted else int(math.floor((airplay_db + 30) * 100 / 30 + .5))
    applied = min(requested, maximum)
    return VolumeIntent(requested, applied, muted, requested != applied)


class SessionVolumeCursor:
    """Reject other/old sessions and duplicate events before a hardware write.

    Latest-event coalescing belongs to the receiver status channel. The first
    accepted event must be chosen by the owner after startup safety readback;
    this class never guesses whether a high initial slider value is safe.
    """

    def __init__(self, generation: int, *, maximum: int):
        if type(generation) is not int or generation < 1:
            raise ValueError('positive session generation required')
        project_volume(-30, maximum=maximum)
        self.generation = generation
        self.maximum = maximum
        self.sequence = -1
        self.closed = False

    def consume(self, generation: int, sequence: int, airplay_db: float):
        if (self.closed or generation != self.generation or type(sequence) is not int
                or sequence < 0 or sequence <= self.sequence):
            return None
        result = project_volume(airplay_db, maximum=self.maximum)
        self.sequence = sequence
        return result

    def close(self):
        self.closed = True
