#!/usr/bin/env python3
"""Local-only SoundTouch display behavior emulator (not original firmware).

Run with Python 3.11+: python tools/display_emulator.py
Open the printed loopback URL on this computer. Ctrl-C stops the tool.
No application startup, database, credentials, device transport or Internet.
"""
from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
import socketserver
import sys
from typing import cast

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from basswiesn.app.services.display_composition import (  # noqa: E402
    DISPLAY_FIELDS, DisplayPreference, display_fields, validate_display_preference,
)

ASSETS = Path(__file__).with_name("display_emulator_assets")
MAX_BODY = 16_384
CONTRACT = {
    "kind": "BEHAVIORAL_DISPLAY_EMULATOR", "firmware_execution": False,
    "hardware_validated": False, "radio_contact": False, "database_access": False,
    "text_composition": "SHARED_WITH_BASSWIESN_RUNTIME",
    "layout": "UNCALIBRATED_MODEL", "fields": DISPLAY_FIELDS,
    "observed_raster": {"width": 128, "height": 100, "bpp": 8, "scope": "One SoundTouch 30 SCM 27.0.6"},
    "limits": ["Exact font rasterization, pagination and overlay priority remain partial; the free model is uncalibrated.",
               "AirPlay is synthetic metadata here, not an implemented radio metadata bridge.",
               "Separate fixed screen regions are a design experiment, not an available device setting.",
               "The 256-character budget belongs to BASSWIESN, not to the OLED panel."],
}


def compose(payload: object) -> dict[str, object]:
    """Bound the input before calling the single runtime text implementation."""
    if not isinstance(payload, dict) or set(payload) - {"station", "metadata", "mode", "fields", "order", "clock"}:
        raise ValueError("invalid composition object")
    values = cast(dict[str, object], payload)
    station, clock, mode = values.get("station", ""), values.get("clock", ""), values.get("mode", "CUSTOM")
    if not isinstance(station, str) or len(station) > 1024:
        raise ValueError("station must be bounded text")
    if not isinstance(clock, str) or (clock and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock)):
        raise ValueError("clock must be HH:MM or empty")
    if not isinstance(mode, str):
        raise ValueError("invalid mode")
    metadata = values.get("metadata", {})
    if not isinstance(metadata, dict) or set(metadata) - {"track", "artist", "other_info", "description", "genre"}:
        raise ValueError("invalid metadata fields")
    if any(not isinstance(k, str) or not isinstance(v, str) or len(v) > 1024 for k, v in metadata.items()):
        raise ValueError("metadata must contain bounded text")
    fields, order = values.get("fields", list(DISPLAY_FIELDS[:-1])), values.get("order", list(DISPLAY_FIELDS))
    for item in (fields, order):
        if not isinstance(item, list) or len(item) > 5 or any(not isinstance(v, str) for v in item):
            raise ValueError("invalid fields or order")
    selected = tuple(cast(list[str], fields))
    ordered = tuple(cast(list[str], order))
    preference = DisplayPreference(mode, fields=selected, field_order=ordered)
    # Validate choices even in legacy modes so switching mode cannot hide bad input.
    validate_display_preference(DisplayPreference("CUSTOM", fields=selected, field_order=ordered))
    validate_display_preference(preference)
    rendered = display_fields(preference, station, metadata, clock_text=clock)
    return {**rendered, "characters": len(rendered["track"]), "contract": CONTRACT}


def network_guard(event: str, args: tuple[object, ...]) -> None:
    """Defense in depth for this process only; no outbound/DNS/child processes."""
    if event in {"socket.connect", "socket.sendto", "socket.getaddrinfo", "socket.gethostbyname",
                 "socket.gethostbyaddr", "subprocess.Popen", "os.system"}:
        raise PermissionError("DISPLAY_EMULATOR_OUTBOUND_FORBIDDEN")
    if event == "socket.bind":
        address = args[1]
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise PermissionError("DISPLAY_EMULATOR_LOOPBACK_ONLY")


class DisplayServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self) -> None:
        # HTTPServer normally resolves its own hostname; this tool needs no DNS.
        socketserver.TCPServer.server_bind(self)
        self.server_name = "127.0.0.1"
        self.server_port = self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    server_version = "DisplayModel"
    sys_version = ""

    def setup(self) -> None:
        self.request.settimeout(3)
        super().setup()

    def log_message(self, format: str, *args: object) -> None:
        # User-entered metadata and request URLs must not become access logs.
        pass

    @property
    def origin(self) -> str:
        address = cast(tuple[str, int], self.server.server_address)
        return f"http://127.0.0.1:{address[1]}"

    def authorized(self, *, write: bool = False) -> bool:
        if self.headers.get_all("Host") != [self.origin.removeprefix("http://")]:
            return False
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            return False
        return not write or self.headers.get_all("Origin") == [self.origin]

    def reply(self, code: int, body: bytes, content_type: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; "
                         "connect-src 'self'; img-src 'self' blob:; font-src 'self' blob:; frame-ancestors 'none'; base-uri 'none'; "
                         "form-action 'none'; object-src 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self.authorized():
            self.reply(403, b'{"error":"origin denied"}')
            return
        routes = {"/": ("index.html", "text/html"), "/style.css": ("style.css", "text/css"),
                  "/emulator.mjs": ("emulator.mjs", "text/javascript"), "/model.mjs": ("model.mjs", "text/javascript"),
                  "/review": ("review.html", "text/html"), "/review.css": ("review.css", "text/css"),
                  "/fixed-layout.mjs": ("fixed-layout.mjs", "text/javascript"),
                  "/review.mjs": ("review.mjs", "text/javascript"), "/review-model.mjs": ("review-model.mjs", "text/javascript"),
                  "/boot": ("boot.html", "text/html"), "/boot.mjs": ("boot.mjs", "text/javascript"),
                  "/boot-layout.mjs": ("boot-layout.mjs", "text/javascript"), "/boot-mark.json": ("boot-mark.json", "application/json")}
        if self.path == "/api/contract":
            self.reply(200, json.dumps(CONTRACT).encode())
        elif self.path in routes:
            name, kind = routes[self.path]
            self.reply(200, (ASSETS / name).read_bytes(), kind)
        else:
            self.reply(404, b'{"error":"unknown local route"}')

    def do_POST(self) -> None:
        if not self.authorized(write=True):
            self.reply(403, b'{"error":"origin denied"}')
            return
        if self.path != "/api/compose":
            self.reply(404, b'{"error":"no device actions exist"}')
            return
        try:
            lengths = self.headers.get_all("Content-Length") or []
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding"):
                raise ValueError("length required")
            size = int(lengths[0])
            if not 0 < size <= MAX_BODY:
                self.reply(413, b'{"error":"body too large or empty"}')
                return
            if self.headers.get("Content-Type") != "application/json":
                self.reply(415, b'{"error":"JSON required"}')
                return
            raw = self.rfile.read(size)
            if len(raw) != size:
                raise ValueError("incomplete request")
            result = compose(json.loads(raw))
        except (ValueError, TypeError, RecursionError):
            self.reply(400, b'{"error":"invalid bounded display input"}')
            return
        self.reply(200, json.dumps(result, ensure_ascii=False).encode())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8840, help="Loopback port only; 0 selects an unused port")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("port out of range")
    sys.addaudithook(network_guard)
    try:
        server = DisplayServer(("127.0.0.1", args.port), Handler)
    except OSError:
        parser.exit(1, "Cannot bind local port. Retry with --port 0; no existing service was changed.\n")
    print(f"Display emulator: http://127.0.0.1:{server.server_port}/", flush=True)
    print("Behavior model only. No firmware execution, radio access or database. Ctrl-C stops it.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
