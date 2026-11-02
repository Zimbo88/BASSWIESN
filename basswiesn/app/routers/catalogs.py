from fastapi import APIRouter

from basswiesn.app.services.catalogs import (
    DISPLAY_METADATA_MODES,
    KEY_COMMANDS,
    MEDIA_LIBRARY_CAPABILITIES,
    SERVICE_CATALOG,
    SETTINGS_CATALOG,
    STEREO_PAIRING_RESEARCH,
    STOCKHOLM_LANGUAGES,
    SUPPORTED_MEDIA_TYPES,
    TELNET_COMMANDS,
)

router = APIRouter(prefix="/api", tags=["catalogs"])


@router.get("/languages")
async def languages() -> list[dict]:
    return STOCKHOLM_LANGUAGES


@router.get("/settings/catalog")
async def settings_catalog() -> list[dict]:
    return SETTINGS_CATALOG


@router.get("/keys")
async def key_commands() -> list[dict]:
    return KEY_COMMANDS


@router.get("/display/metadata-modes")
async def display_metadata_modes() -> list[dict]:
    return DISPLAY_METADATA_MODES


@router.get("/telnet/commands")
async def telnet_commands() -> list[dict]:
    return TELNET_COMMANDS


@router.get("/media-library/capabilities")
async def media_library_capabilities() -> dict:
    return MEDIA_LIBRARY_CAPABILITIES


@router.get("/services/catalog")
async def services_catalog() -> list[dict]:
    return SERVICE_CATALOG


@router.get("/stereo-pairing/research")
async def stereo_pairing_research() -> dict:
    return STEREO_PAIRING_RESEARCH


@router.get("/media-types")
async def media_types() -> list[dict]:
    return SUPPORTED_MEDIA_TYPES
