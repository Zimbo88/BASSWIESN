"""Central private-test override for setup confirmation gates only."""

from basswiesn.app.config import get_settings
from basswiesn.app.core.masterlog import write_masterlog


def setup_confirmations_disabled() -> bool:
    settings = get_settings()
    return settings.test_mode and settings.disable_setup_confirmations


def is_yes_confirmation(provided: object) -> bool:
    return str(provided or "").strip().lower() == "yes"


def setup_confirmation_allowed(
    provided: object,
    expected: str,
    *,
    endpoint: str,
    action: str,
    alternatives: tuple[str, ...] = (),
) -> bool:
    value = str(provided or "").strip()
    if is_yes_confirmation(value):
        return True
    if not setup_confirmations_disabled():
        return False
    write_masterlog(
        "setup_confirmation_skipped",
        endpoint=endpoint,
        skipped_confirmation=expected,
        action=action,
    )
    return True
