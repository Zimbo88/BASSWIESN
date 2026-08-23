from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy import desc
from sqlalchemy.orm import Session

from basswiesn.app.db import get_db
from basswiesn.app.config import get_settings
from basswiesn.app.models import RequestLog
from basswiesn.app.services.diagnostics import diagnostics_snapshot

router = APIRouter(tags=["debug"])


@router.get("/", response_class=HTMLResponse)
async def debug_home() -> str:
    version = get_settings().version
    display_version = version if str(version).startswith("v") else f"v{version}"
    return f"""<!doctype html><html lang="de"><head><meta name="viewport" content="width=device-width"><title>BASSWIESN Diagnose · 1860</title><style>body{{font-family:system-ui;background:#11151d;color:#eef3f8;max-width:850px;margin:40px auto;padding:20px}}a{{color:#ff5bbd}}section{{background:#1b2230;border:1px solid #37445a;border-radius:16px;padding:20px;margin:16px 0}}</style></head><body><h1>BASSWIESN Diagnose · Port 1860</h1><p>Dieser Dienst sammelt technische Zustände, ohne die normale Weboberfläche oder den Radio-Cloudverkehr zu vermischen.</p><section><h2>Direkte Ansichten</h2><p><a href="/health">Dienststatus</a> · <a href="/requests">Letzte Radio-/Cloud-Anfragen</a> · <a href="/diagnostics.json">Zusammengefasste Diagnose</a> · <a href="/docs">API-Dokumentation</a></p></section><section><h2>Wofür?</h2><p>Fehler zeitlich zuordnen, angefragte Dienste erkennen und Setup-/Preset-/Multiroom-Probleme nachvollziehen. Rohdaten bleiben im Diagnosebereich; die Endnutzeroberfläche fasst sie verständlich zusammen.</p></section><p>Version {display_version}</p></body></html>"""


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "service": "basswiesn-debug"}


@router.get("/requests")
async def requests(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(RequestLog).order_by(desc(RequestLog.ts)).limit(200).all()
    return [
        {
            "ts": row.ts.isoformat() + "Z",
            "service": row.service,
            "method": row.method,
            "path": row.path,
            "host": row.host,
            "status_code": row.status_code,
        }
        for row in rows
    ]


@router.get("/diagnostics.json")
async def diagnostics(db: Session = Depends(get_db)) -> dict:
    return diagnostics_snapshot(db)
