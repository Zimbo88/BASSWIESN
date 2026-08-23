from __future__ import annotations

from basswiesn.app.config import get_settings


def dlna_status() -> dict:
    return {
        "enabled": get_settings().experimental_dlna,
        "experimental": True,
        "background_services_started": False,
        "renderer_discovery": "disabled" if not get_settings().experimental_dlna else "manual_only",
        "transcoding": "not_required",
        "limitations": [
            "Keine Garantie für jedes SoundTouch-Modell.",
            "Keine automatische Transkodierung.",
            "Keine Hintergrundscans, solange das Feature-Flag deaktiviert ist.",
        ],
    }


async def discover_renderers() -> dict:
    if not get_settings().experimental_dlna:
        return {**dlna_status(), "renderers": [], "skipped": True, "reason": "BASSWIESN_EXPERIMENTAL_DLNA=false"}
    return {**dlna_status(), "renderers": [], "skipped": False, "reason": "Renderer discovery is prepared but requires hardware validation."}
