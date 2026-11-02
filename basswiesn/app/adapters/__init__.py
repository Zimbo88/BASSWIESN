"""Adapters for communication with external SoundTouch services and devices."""

from basswiesn.app.adapters.discovery import probe_device, scan_subnet
from basswiesn.app.adapters.soundtouch_client import SoundTouchClient

__all__ = ["SoundTouchClient", "probe_device", "scan_subnet"]
