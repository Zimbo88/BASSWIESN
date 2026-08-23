"""Allowlisted SSH recovery operation metadata."""

from .activation import public_operation


def recovery_operations() -> list[dict[str, object]]:
    return [public_operation("common.rollback_ssh")]
