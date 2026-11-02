"""Explicit known-profile fixture support for older focused remote tests.

Only missing stable protocol fixtures are supplied; playback and actual volume
continue to come from each test's fake. This is not a runtime/profile exception.
"""
from xml.etree import ElementTree as ET

from basswiesn.app import db as app_db
from basswiesn.app.models import Device
from test_maintenance_reboot_safety import BUILD, info

ITEM = '<ContentItem source="LOCAL_INTERNET_RADIO" sourceAccount="" type="stationurl" location="http://example.invalid/live.mp3"/>'


def establish_key_identity(identity):
    with app_db.SessionLocal() as db:
        device = db.query(Device).filter_by(device_id=identity).one()
        device.model, device.firmware, device.info_xml = "SoundTouch 30", BUILD, info(identity)
        db.commit()


def complete_key_client(inner, *, volume_from_inner=False, selected_preset=False, simulate_mute=False):
    class Client:
        def __init__(self, ip_address, **kwargs):
            self.inner = inner(ip_address)
            self.muted = False
            with app_db.SessionLocal() as db:
                self.identity = db.query(Device).filter_by(ip_address=ip_address).one().device_id

        async def get_xml(self, path):
            fixed = {"/info": info(self.identity), "/getZone": "<zone/>",
                     "/presets": '<presets>' + ''.join(f'<preset id="{slot}">{ITEM}</preset>' for slot in range(1, 7)) + '</presets>',
                     "/sources": '<sources><sourceItem source="LOCAL_INTERNET_RADIO" sourceAccount="" status="READY"/></sources>'}
            if not volume_from_inner:
                fixed["/volume"] = f'<volume><actualvolume>1</actualvolume><muteenabled>{str(self.muted).lower()}</muteenabled></volume>'
            if path in fixed:
                return fixed[path]
            xml = await self.inner.get_xml(path)
            if path == "/now_playing" and selected_preset:
                root = ET.fromstring(xml)
                if root.get("source") == "LOCAL_INTERNET_RADIO" and root.find("ContentItem") is None:
                    root.append(ET.fromstring(ITEM))
                return ET.tostring(root, encoding="unicode")
            if path == "/volume":
                root = ET.fromstring(xml)
                if root.find("muteenabled") is None:
                    ET.SubElement(root, "muteenabled").text = str(self.muted).lower()
                return ET.tostring(root, encoding="unicode")
            return xml

        async def post_xml(self, path, body):
            if simulate_mute and path == "/key":
                node = ET.fromstring(body)
                if node.text == "MUTE":
                    if node.get("state") == "press":
                        self.muted = not self.muted
                    return '<status>OK</status>'
            return await self.inner.post_xml(path, body)
    return Client
