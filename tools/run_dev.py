import multiprocessing
import importlib
import json
import signal
import sys
import time
import urllib.request
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.app.config import get_settings
from basswiesn.app.services.tls import ensure_tls_files


def run(app_path: str, port: int, ssl_certfile: str | None = None, ssl_keyfile: str | None = None) -> None:
    module, name = app_path.split(":", 1)
    app = getattr(importlib.import_module(module), name)
    # The parent finished migrations before *any* service began writing.
    # Direct uvicorn launches retain the normal lifespan initialization.
    app.state.database_schema_prepared = True
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False, proxy_headers=False,
                ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)


def healthcheck() -> bool:
    """Check every required local service, never a device or LAN endpoint."""
    settings = get_settings()
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for port, path in ((settings.web_port, "/api/readiness"),
                       (settings.cloud_port, "/bmx/registry/v1/services"),
                       (settings.debug_port, "/health")):
        try:
            with opener.open(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                payload = json.load(response)
                if response.status != 200 or not isinstance(payload, dict):
                    return False
                if path == "/api/readiness" and payload.get("ready") is not True:
                    return False
                if path == "/health" and payload.get("status") != "ok":
                    return False
                if path == "/bmx/registry/v1/services" and not isinstance(payload.get("bmx_services"), list):
                    return False
        except (OSError, ValueError):
            return False
    return True


def supervise(processes) -> None:
    """A lost cloud process must fail the container, not leave a green WebUI."""
    try:
        for process in processes:
            process.start()
        while True:
            for process in processes:
                if process.exitcode is not None:
                    raise SystemExit(process.exitcode or 1)
            time.sleep(0.2)
    finally:
        for process in processes:
            if process.pid is not None and process.is_alive():
                process.terminate()
        deadline = time.monotonic() + 12
        for process in processes:
            if process.pid is not None:
                process.join(timeout=max(0, deadline - time.monotonic()))
                if process.is_alive():
                    process.kill()
                    process.join(timeout=2)


if __name__ == "__main__":
    if "--healthcheck" in sys.argv:
        raise SystemExit(0 if healthcheck() else 1)
    # Only the parent runs migrations; this prevents a started WebGUI's
    # background writers contending with another child's schema seeding.
    from basswiesn.app.db import init_db, engine
    from basswiesn.app.services.filesystem_contract import ensure_runtime_directories
    ensure_runtime_directories(get_settings().data_dir)
    init_db()
    engine.dispose()
    settings = get_settings()
    specs = [
        ("basswiesn.app.main:web_app", settings.web_port, None, None),
        ("basswiesn.app.main:cloud_app", settings.cloud_port, None, None),
        ("basswiesn.app.main:debug_app", settings.debug_port, None, None),
    ]
    if settings.enable_https:
        tls = ensure_tls_files(settings)
        if not tls.ok:
            raise SystemExit(f"HTTPS requested but unavailable: {tls.message}")
        specs.append(("basswiesn.app.main:https_app", settings.https_port, tls.cert_file, tls.key_file))
    processes = [multiprocessing.Process(target=run, args=spec) for spec in specs]
    def terminate(_signal, _frame):
        raise SystemExit(0)
    signal.signal(signal.SIGTERM, terminate)
    supervise(processes)
