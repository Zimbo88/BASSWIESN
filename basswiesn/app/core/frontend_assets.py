"""Content-bound browser caching, including private builds of one version."""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path


def asset_digest(root: Path) -> str:
    digest = sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix in {".js", ".css"}:
            digest.update(path.relative_to(root).as_posix().encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:20]


@lru_cache(maxsize=1)
def static_revision() -> str:
    return asset_digest(Path(__file__).resolve().parents[1] / "static")


def versioned_html(html: str, version: str) -> str:
    # Keep the canonical project version visible; private fixes do not require
    # inventing a release. Their actual JS/CSS bytes determine cache identity.
    return html.replace(f"?v={version}", f"?v={version}&amp;asset={static_revision()}")
