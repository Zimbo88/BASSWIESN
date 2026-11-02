"""Opt-in ICY display metadata. Never a playback proxy or radio controller.

Only incoming live-radio reports trigger bounded public-stream probes. Audio
bytes are discarded, results are shared per station, and provider responses
never wait for the Internet. Missing metadata cannot stop playback.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import UTC, datetime
import hashlib
import json
import math
import re
import time
from urllib.parse import urljoin, urlsplit

import httpx
from sqlalchemy.orm import Session

from basswiesn.app.models import MetadataState, RuntimeState, Setting
from basswiesn.app.services.display_composition import (
    DISPLAY_FIELDS, DisplayPreference, _text, display_fields, validate_display_preference,
)
from basswiesn.app.services.network_security import pinned_http_target, validate_outbound_http_url
from basswiesn.app.services.protected_devices import is_device_access_protected
from basswiesn.app.services.airplay_bridge.display_session import is_bridge_station

MAX_BYTES = 262144
MAX_INTERVAL = 65536
MAX_AGE = 120
PREF_PREFIX = "radio_display:"
CACHE_PREFIX = "station_icy:"
def _decode(value: str) -> dict:
    try:
        decoded = json.loads(value)
        return decoded if isinstance(decoded, dict) else {}
    except (TypeError, ValueError):
        return {}


def load_display_preference(db: Session, device_id: str) -> DisplayPreference:
    row = db.query(Setting).filter(Setting.key == PREF_PREFIX + device_id.upper()).one_or_none()
    value = _decode(row.value) if row else {}
    if value.get("mode") == "CUSTOM":
        try:
            # Invalid persisted choices fail closed: no metadata probes or
            # invented fallback settings. An explicit empty title is valid.
            preference = DisplayPreference("CUSTOM", "other" in value["fields"],
                                           tuple(value["fields"]), tuple(value["field_order"]))
            validate_display_preference(preference)
            return preference
        except (KeyError, TypeError, ValueError):
            return DisplayPreference("CUSTOM", fields=())
    return DisplayPreference(
        mode="TRACK_ARTIST" if value.get("mode") == "TRACK_ARTIST" else "STATION",
        show_other_info=value.get("show_other_info") is True,
    )


def has_display_preference(db: Session, device_id: str) -> bool:
    """Distinguish an explicit 'station only' choice from legacy behaviour."""
    return db.query(Setting.key).filter(Setting.key == PREF_PREFIX + device_id.upper()).first() is not None


def save_display_preference(db: Session, device_id: str, preference: DisplayPreference):
    validate_display_preference(preference)
    key = PREF_PREFIX + device_id.upper()
    row = db.query(Setting).filter(Setting.key == key).one_or_none()
    if row is None:
        row = Setting(key=key)
        db.add(row)
    row.value = json.dumps(asdict(preference))
    db.commit()


def cache_key(url: str) -> str:
    return CACHE_PREFIX + hashlib.sha256(url.encode()).hexdigest()


def cached_metadata(db: Session, url: str, *, now: float | None = None) -> dict:
    row = db.query(RuntimeState).filter(RuntimeState.key == cache_key(url)).one_or_none()
    value = _decode(row.value) if row else {}
    stamp = value.get("observed_timestamp")
    age = (time.time() if now is None else now) - stamp if isinstance(stamp, (int, float)) else -1
    if not 0 <= age <= MAX_AGE:
        return {}
    return value


def display_observation(db: Session, device_id: str, *, now: float | None = None) -> dict:
    """Explain stored upstream evidence, never infer physical display success.

    Pure DB read. Opening display preferences must not probe a stream or radio.
    A recent shared cache can outlive the radio session, so label it as cached
    sender data, not as current playback or a live OLED measurement.
    """
    from basswiesn.app.services.orion import station_by_contract_key
    result = {"status": "NOT_OBSERVED", "scope": "LAST_STATION_CACHE", "radio_contacted": False,
              "probe_started": False, "display_verified": False, "cache_age_seconds": None,
              "artist_available": False, "title_available": False, "station_name": ""}
    if not load_display_preference(db, device_id).needs_metadata:
        return {**result, "status": "NOT_REQUESTED"}
    meta = db.query(MetadataState).filter(MetadataState.device_id == device_id).one_or_none()
    if meta is None or not meta.station_id:
        return result
    station = station_by_contract_key(db, meta.station_id)
    if station is None:
        return result
    result["station_name"] = _text(station.name)
    row = db.query(RuntimeState).filter(RuntimeState.key == cache_key(station.stream_url)).one_or_none()
    value = _decode(row.value) if row else {}
    stamp = value.get("observed_timestamp")
    current = time.time() if now is None else now
    if type(stamp) not in (int, float) or not math.isfinite(stamp) or current < stamp:
        return result
    age = current - stamp
    result["cache_age_seconds"] = round(age)
    if age > MAX_AGE:
        return {**result, "status": "STALE"}
    if value.get("status") not in {"AVAILABLE", "EMPTY_METADATA"}:
        return {**result, "status": "UNAVAILABLE"}
    result.update(artist_available=bool(value.get("artist")), title_available=bool(value.get("track")))
    if result["artist_available"] and result["title_available"]:
        result["status"] = "SONG_AVAILABLE"
    elif value.get("other_info"):
        result["status"] = "INFORMATION_ONLY"
    else:
        result["status"] = "NO_SONG"
    return result


def classify_title(raw: str, station_name: str, host: str) -> dict:
    """Observed provider conventions are inference, not an ICY field guarantee.

    ICY defines one StreamTitle string, not a universal artist/title schema.
    Unknown formats and station slogans stay in other_info, never fake artists.
    """
    raw = _text(raw)
    result = {"raw_title": raw, "track": "", "artist": "", "other_info": raw,
              "classification": "UNSTRUCTURED" if raw else "EMPTY"}
    host = host.lower()
    br_provider = host == "rndfnk.com" or host.endswith(".rndfnk.com")
    separators = (": ", " - ") if br_provider else (" - ",) if (
        host == "radiopaloma.de" or host.endswith(".radiopaloma.de")
        or host == "stream24.net" or host.endswith(".stream24.net")
        or host == "krone.at" or host.endswith(".krone.at")
        or host == "antenne.de" or host.endswith(".antenne.de")
    ) else ()
    candidates = [separator for separator in separators if separator in raw]
    if candidates:
        separator = min(candidates, key=raw.index)
        artist, track = raw.split(separator, 1)
        normalize = lambda s: re.sub(r"[^\w]", "", s.casefold())
        station = normalize(station_name)
        branded = bool(normalize(artist) and normalize(artist) in station)
        information_label = normalize(artist) in {
            "verkehr", "verkehrsmeldungen", "wetter", "nachrichten", "news",
            "information", "info", "hinweis", "jetzt", "sendung", "programm",
        }
        if artist.strip() and track.strip() and not branded and not information_label:
            result.update(artist=artist.strip(), track=track.strip(), other_info="",
                          classification="ARTIST_TITLE_INFERRED")
    return result


async def probe_icy(url: str, station_name: str) -> dict:
    result = {"status": "UNAVAILABLE", "track": "", "artist": "", "other_info": ""}
    latest = None

    def finish(reason: str) -> dict:
        # A startup slogan/empty title is a genuine observation, but may be
        # followed by current song information within the SAME bounded read.
        # Never enlarge the byte/time limits or retain a title from a prior
        # collection. An explicit empty update replaces the current fallback.
        return {**latest, "probe_stop_reason": reason} if latest is not None else {**result, "status": reason}
    try:
        async with asyncio.timeout(12):
            async with httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False) as client:
                current = url
                for _ in range(5):
                    validation = await asyncio.to_thread(validate_outbound_http_url, current, public_only=True)
                    if not validation.ok:
                        return {**result, "status": "TARGET_REJECTED"}
                    pinned, headers, extensions = pinned_http_target(current, validation)
                    async with client.stream("GET", pinned, headers={**headers, "Icy-MetaData": "1",
                            "Accept-Encoding": "identity", "User-Agent": "BASSWIESN-Metadata/1"},
                            extensions=extensions) as response:
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location", "")
                            if not location:
                                return {**result, "status": "INVALID_REDIRECT"}
                            current = urljoin(current, location)
                            continue
                        if response.status_code != 200:
                            return {**result, "status": "HTTP_ERROR", "http_status": response.status_code}
                        result.update(genre=_text(response.headers.get("icy-genre")),
                                      description=_text(response.headers.get("icy-description")),
                                      bitrate=_text(response.headers.get("icy-br"), 12),
                                      mime=_text(response.headers.get("content-type"), 80))
                        if response.headers.get("content-encoding", "identity") != "identity":
                            return {**result, "status": "UNSUPPORTED_ENCODING"}
                        try:
                            interval = int(response.headers.get("icy-metaint", "0"))
                        except ValueError:
                            interval = 0
                        if not 1 <= interval <= MAX_INTERVAL:
                            return {**result, "status": "NO_METADATA_INTERVAL"}
                        buffer = bytearray()
                        total = 0
                        async for chunk in response.aiter_raw():
                            total += len(chunk)
                            if total > MAX_BYTES:
                                return finish("BYTE_LIMIT")
                            buffer.extend(chunk)
                            while len(buffer) > interval:
                                size = buffer[interval] * 16
                                end = interval + 1 + size
                                if len(buffer) < end:
                                    break
                                block = bytes(buffer[interval + 1:end]).rstrip(b"\0")
                                del buffer[:end]
                                try:
                                    text = block.decode("utf-8")
                                except UnicodeDecodeError:
                                    text = block.decode("latin-1")
                                match = re.search(r"StreamTitle='(.*?)';", text, re.DOTALL)
                                if match:
                                    if match.group(1).strip():
                                        latest = {**result, **classify_title(match.group(1), station_name, urlsplit(current).hostname or ""),
                                                  "status": "AVAILABLE", "source": "ICY"}
                                        if latest.get("track") and latest.get("artist"):
                                            return latest
                                    else:
                                        latest = {**result, "status": "EMPTY_METADATA", "source": "ICY"}
                                # A zero-length ICY frame means "no update", not
                                # "this station has no song metadata". Startup
                                # bursts can contain several such frames; keep
                                # looking within the existing byte/time budgets.
                        return finish("STREAM_ENDED")
                return {**result, "status": "REDIRECT_LIMIT"}
    except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
        if latest is not None:
            return {**latest, "probe_stop_reason": "TIME_LIMIT" if isinstance(exc, TimeoutError) else type(exc).__name__}
        return {**result, "status": "PROBE_FAILED", "error_type": type(exc).__name__}


async def probe_station_metadata(url: str, station_name: str) -> dict:
    from basswiesn.app.services import antenne_metadata
    if antenne_metadata.supports(url):
        programme = await antenne_metadata.probe()
        if programme is not None:
            return programme
    return await probe_icy(url, station_name)


class StationMetadataCollector:
    def __init__(self, session_factory, probe=probe_station_metadata):
        self.session_factory = session_factory
        self.probe = probe
        self.tasks = {}
        self.next_due = {}

    def schedule(self, db: Session, device, station, *, peer: str, event_type: str):
        if event_type not in {"START", "TIMED"} or station is None or peer != device.ip_address:
            return
        # A relay is one-use and private; probing it for ICY would consume the
        # radio's audio grant. Apple text arrives via the owned session instead.
        if is_bridge_station(station):
            return
        if is_device_access_protected(device.ip_address, device_id=device.device_id):
            return
        if not load_display_preference(db, device.device_id).needs_metadata:
            return
        key = cache_key(station.stream_url)
        if key in self.tasks or len(self.tasks) >= 4 or self.next_due.get(key, 0) > time.monotonic():
            return
        # Bound the cache book-keeping even when many distinct stations occur.
        if len(self.next_due) >= 128:
            self.next_due = {k: v for k, v in self.next_due.items() if v > time.monotonic()}
            if len(self.next_due) >= 128:
                return
        self.next_due[key] = time.monotonic() + 60
        self.tasks[key] = asyncio.create_task(self._collect(key, station.stream_url, station.name))

    async def _collect(self, key, url, name):
        try:
            result = await self.probe(url, name)
            result.update(observed_timestamp=time.time(), observed_at=datetime.now(UTC).isoformat())
            # An explicit empty title is a valid live update, not a network
            # failure: the next song can appear on the normal next-minute poll.
            self.next_due[key] = time.monotonic() + (60 if result.get("status") in {"AVAILABLE", "EMPTY_METADATA"} else 300)
            with self.session_factory() as db:
                row = db.query(RuntimeState).filter(RuntimeState.key == key).one_or_none()
                if row is None:
                    row = RuntimeState(key=key)
                    db.add(row)
                row.value = json.dumps(result, ensure_ascii=False)
                db.commit()
        except Exception:
            # Metadata is optional. It must neither fail a reporting response
            # nor alter playback; failed probes get a bounded retry delay.
            self.next_due[key] = time.monotonic() + 300
        finally:
            self.tasks.pop(key, None)

    async def shutdown(self):
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
