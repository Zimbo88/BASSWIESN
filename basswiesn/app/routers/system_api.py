"""System and feature read models; no radio write orchestration."""
from pathlib import Path
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from sqlalchemy.orm import Session
from basswiesn import __release_stage__
from basswiesn.app.api_models import HealthCheckResponse, ReadinessResponse, StatusResponse, VersionResponse
from basswiesn.app.config import get_settings
from basswiesn.app.db import get_db
from basswiesn.app.services import runtime_health
from basswiesn.app.services.feature_status import build_feature_status
from basswiesn.app.services.listener_health import http_service_status as _http_service_status
from basswiesn.app.services.release_publication import publication_info, refresh_publication

router = APIRouter(tags=["system"])

@router.get("/health", response_model=StatusResponse)
async def health() -> dict[str, str | bool]:
    return {"ok": True, "status": "ok", "version": get_settings().version}


@router.get("/version", response_model=VersionResponse)
async def api_version(db: Session = Depends(get_db)) -> dict[str, Any]:
    settings = get_settings()
    build_type = "Stable Release" if __release_stage__ == "stable" and "-rc" not in settings.version else "Release Candidate"
    return {"version": settings.version, "build_type": build_type,
            **publication_info(db, settings.version)}


@router.post("/version/publication-refresh")
async def version_publication_refresh(db: Session = Depends(get_db)) -> dict[str, Any]:
    """Explicit official-publication lookup; no installation or radio action."""
    version = get_settings().version
    return {"version": version, **await refresh_publication(db, version)}


@router.get("/features/status")
async def feature_status(db: Session = Depends(get_db)) -> dict[str, Any]:
    features = build_feature_status(db)
    counts = {"all": len(features)}
    counts.update({
        "active": sum(bool(item["enabled"] and item["available"]) for item in features),
        "action_required": sum(bool(item["blockers"]) or not item["configured"] for item in features),
        "disabled": sum(item["status"] == "Deaktiviert" for item in features),
        "experimental": sum(bool(item["experimental"] or item["lab_only"]) for item in features),
        "hardware_open": sum(item["hardware_status"] == "offen" for item in features),
    })
    return {"version": get_settings().version, "source": "runtime-config-and-local-state", "features": features, "counts": counts}


FEATURE_DOCUMENTS = {
    "project-status": "FEATURES.md",
    "activation-matrix": "FEATURES.md",
    "activation-gaps": "FEATURES.md",
    "release-pipeline": "RELEASE_CHECKLIST.md",
    "testing": "RELEASE_CHECKLIST.md",
    "offline": "FEATURES.md",
}


@router.get("/features/docs/{document_id}")
async def feature_documentation(document_id: str) -> Response:
    filename = FEATURE_DOCUMENTS.get(document_id)
    if not filename:
        raise HTTPException(status_code=404, detail="feature documentation not found")
    root = Path(__file__).resolve().parents[3]
    path = root / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="feature documentation not found")
    return Response(path.read_text(encoding="utf-8"), media_type="text/markdown")


@router.get("/system/service-health")
async def system_service_health() -> dict[str, Any]:
    settings = get_settings()
    cloud = await _http_service_status("cloud", f"http://127.0.0.1:{settings.cloud_port}/live")
    debug = await _http_service_status("debug", f"http://127.0.0.1:{settings.debug_port}/live")
    cloud["internal_url"] = f"http://127.0.0.1:{settings.cloud_port}/live"
    cloud["browser_url"] = settings.local_base_url.rstrip("/") + "/about"
    debug["internal_url"] = f"http://127.0.0.1:{settings.debug_port}/live"
    debug["browser_url"] = settings.debug_base_url.rstrip("/") + "/"
    return {
        "cloud": cloud,
        "debug": debug,
        "status": "green" if cloud["online"] and debug["online"] else "yellow",
    }


@router.get("/readiness", response_model=ReadinessResponse)
async def readiness(db: Session = Depends(get_db)) -> dict[str, Any]:
    return runtime_health.readiness(db)


@router.get("/system/healthcheck", response_model=HealthCheckResponse)
def system_healthcheck(db: Session = Depends(get_db)) -> dict[str, Any]:
    # Historical log counts can take seconds on a Pi. FastAPI runs this
    # synchronous handler in its worker pool, not the radio-readback loop.
    return runtime_health.release_healthcheck(db)
