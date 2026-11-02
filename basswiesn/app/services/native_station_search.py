"""One identity-bound native provider query, never a playback or preset write.

The firmware uses POST for searching. Serialize with device transactions so a
setup/reboot cannot retarget this request, but do not pretend it changes stored
configuration or needs a restore. A timeout is not permission to replay POST.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import cast
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeXML  # type: ignore[import-untyped]
from defusedxml.common import DefusedXmlException  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from basswiesn.app.models import Device
from basswiesn.app.services.device_settings_writer import ClientFactory, IdentityMismatch, verify_settings_identity
from basswiesn.app.services.device_transactions import device_fingerprint, device_transaction
from basswiesn.app.services.protected_devices import require_unprotected_device


class NativeSearchError(Exception):
    def __init__(self, code: str, status: int) -> None:
        self.code, self.status = code, status
        super().__init__(code)


def _xml(text: str) -> Element:
    try:
        if not text or len(text.encode()) > 1024 * 1024:
            raise ValueError('missing or excessive XML')
        return cast(Element, SafeXML.fromstring(text, forbid_dtd=True))
    except (ValueError, ParseError, DefusedXmlException):
        raise NativeSearchError('NATIVE_SEARCH_INVALID_XML', 502) from None


async def search_native(
    db: Session, device: Device, body: str, *, client_factory: ClientFactory,
    guard: Callable[[Session, Device], None],
) -> str:
    try:
        _xml(body)
    except NativeSearchError:
        raise NativeSearchError('NATIVE_SEARCH_INVALID_REQUEST', 400) from None
    frozen = device_fingerprint(device)
    identity, address = frozen[:2]
    async with device_transaction([identity]):
        def unchanged() -> None:
            db.refresh(device)
            if device_fingerprint(device) != frozen:
                raise NativeSearchError('NATIVE_SEARCH_TARGET_CHANGED', 409)
            require_unprotected_device(device, action='native_station_search', requester='manual')
            guard(db, device)

        unchanged()
        client = client_factory(address, device_id=identity,
                                request_purpose='native_station_search', trigger='manual')

        async def identify() -> None:
            unchanged()
            try:
                xml = await client.get_xml('/info')
            except Exception:
                raise NativeSearchError('NATIVE_SEARCH_TRANSPORT_UNAVAILABLE', 502) from None
            _xml(xml)
            _xml(device.info_xml)
            try:
                verify_settings_identity(device, xml)
            except (ValueError, IdentityMismatch, ParseError):
                raise NativeSearchError('NATIVE_SEARCH_IDENTITY_UNCONFIRMED', 409) from None
            unchanged()

        await identify()
        try:
            response = await client.post_xml('/searchStation', body)
        except Exception:
            raise NativeSearchError('NATIVE_SEARCH_RESPONSE_UNAVAILABLE', 502) from None
        root = _xml(response)
        if root.get('deviceID', identity).strip().upper() != identity.strip().upper():
            raise NativeSearchError('NATIVE_SEARCH_IDENTITY_UNCONFIRMED', 409)
        await identify()
        return response
