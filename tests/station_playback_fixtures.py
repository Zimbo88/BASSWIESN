"""Complete, explicit START for focused legacy station response fixtures.

The individual fake still controls source/readback failures and normalization.
Identity, standalone topology, empty presets and unchanged default volume are
fixture assumptions, not runtime fallbacks. Fault injection uses the independent
stateful radio in test_station_playback_safety instead of this compatibility aid.
"""
from xml.etree import ElementTree as ET

from basswiesn.app import db as app_db
from basswiesn.app.models import Device
from test_maintenance_reboot_safety import info, now, volume


def complete_station_client(inner, *, volume_from_inner=False, sources_from_inner=True,
                            playback_from_inner=True):
    class Client:
        def __init__(self, ip_address, **kwargs):
            self.inner = inner(ip_address)
            self.content = None
            self.standby = True
            with app_db.SessionLocal() as db:
                self.identity = db.query(Device).filter_by(ip_address=ip_address).one().device_id

        async def get_xml(self, path):
            fixed = {"/info": info(self.identity), "/getZone": "<zone/>", "/presets": "<presets/>"}
            if not volume_from_inner:
                fixed["/volume"] = volume(15)
            if not sources_from_inner:
                fixed["/sources"] = '<sources><sourceItem source="LOCAL_INTERNET_RADIO" status="READY"/></sources>'
            if path in fixed:
                return fixed[path]
            if path == "/standby":
                result = await self.inner.get_xml(path)
                self.standby = True
                return result
            if path == "/now_playing":
                if self.standby:
                    return now()
                xml = (await self.inner.get_xml(path)) if playback_from_inner else (
                    '<nowPlaying source="LOCAL_INTERNET_RADIO"><playStatus>STOP_STATE</playStatus></nowPlaying>')
                root = ET.fromstring(xml)
                if self.content is not None and root.get("source") == self.content.get("source") and root.find("ContentItem") is None:
                    root.append(self.content)
                return ET.tostring(root, encoding="unicode")
            return await self.inner.get_xml(path)

        async def post_xml(self, path, body):
            if path == "/select":
                self.content = ET.fromstring(body)
                self.standby = False
            return await self.inner.post_xml(path, body)
    return Client
