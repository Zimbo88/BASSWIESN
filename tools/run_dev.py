import multiprocessing
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from basswiesn.app.config import get_settings
from basswiesn.app.services.tls import ensure_tls_files


def run(app_path: str, port: int, ssl_certfile: str | None = None, ssl_keyfile: str | None = None) -> None:
    uvicorn.run(app_path, host="0.0.0.0", port=port, reload=False, ssl_certfile=ssl_certfile, ssl_keyfile=ssl_keyfile)


if __name__ == "__main__":
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
    for process in processes:
        process.start()
    for process in processes:
        process.join()
