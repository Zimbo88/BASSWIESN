from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import shutil
import subprocess

from basswiesn.app.config import Settings


@dataclass(frozen=True)
class TLSStatus:
    enabled: bool
    mode: str
    port: int
    cert_file: str
    key_file: str
    valid_until: str
    renewal_needed: bool
    ok: bool
    message: str


def _cert_not_after(cert_file: Path) -> str:
    if not cert_file.exists() or not shutil.which("openssl"):
        return ""
    try:
        result = subprocess.run(
            ["openssl", "x509", "-enddate", "-noout", "-in", str(cert_file)],
            text=True,
            capture_output=True,
            timeout=5,
            check=True,
        )
    except Exception:
        return ""
    value = result.stdout.strip().removeprefix("notAfter=").strip()
    for fmt in ("%b %d %H:%M:%S %Y %Z", "%b  %d %H:%M:%S %Y %Z"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=UTC).isoformat()
        except ValueError:
            continue
    return value


def _needs_renewal(cert_file: Path) -> bool:
    if not cert_file.exists():
        return True
    if not shutil.which("openssl"):
        return False
    result = subprocess.run(
        ["openssl", "x509", "-checkend", str(30 * 24 * 3600), "-noout", "-in", str(cert_file)],
        text=True,
        capture_output=True,
        timeout=5,
    )
    return result.returncode != 0


def _generate_selfsigned(cert_file: Path, key_file: Path, settings: Settings) -> None:
    openssl = shutil.which("openssl")
    if not openssl:
        raise RuntimeError("openssl is not installed")
    cert_file.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            openssl,
            "req",
            "-x509",
            "-nodes",
            "-newkey",
            "rsa:2048",
            "-days",
            str(settings.cert_days),
            "-keyout",
            str(key_file),
            "-out",
            str(cert_file),
            "-subj",
            "/CN=basswiesn.local",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=True,
    )


def ensure_tls_files(settings: Settings) -> TLSStatus:
    if not settings.enable_https:
        return TLSStatus(False, settings.cert_mode, settings.https_port, "", "", "", False, True, "HTTPS disabled")
    mode = settings.cert_mode if settings.cert_mode in {"selfsigned", "local-ca", "external"} else "selfsigned"
    if mode == "external":
        cert_file = Path(settings.tls_cert_file)
        key_file = Path(settings.tls_key_file)
        ok = cert_file.exists() and key_file.exists()
        renewal_needed = _needs_renewal(cert_file) if cert_file.exists() else True
        return TLSStatus(True, mode, settings.https_port, str(cert_file), str(key_file), _cert_not_after(cert_file), renewal_needed, ok, "external certificate loaded" if ok else "external cert/key missing")
    cert_file = settings.data_dir / "tls" / "basswiesn.crt"
    key_file = settings.data_dir / "tls" / "basswiesn.key"
    renewal_needed = _needs_renewal(cert_file) or not key_file.exists()
    if renewal_needed:
        _generate_selfsigned(cert_file, key_file, settings)
        renewal_needed = False
    return TLSStatus(True, mode, settings.https_port, str(cert_file), str(key_file), _cert_not_after(cert_file), renewal_needed, True, f"{mode} certificate ready")
