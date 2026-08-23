"""Profiles for SoundTouch Portable family."""

from .base import ActivationMethod, SshProfile


PORTABLE_PROFILE = SshProfile(
    key="soundtouch-portable-scm-27.0.6.46330.5043500",
    model_family="portable",
    supported_model_patterns=("soundtouch portable", "portable"),
    firmware_patterns=("27.0.6.46330.5043500",),
    product_id="0x0925",
    variants=("taigan",),
    platforms=("scm",),
    ssh_daemon="dropbear-or-openssh",
    port=22,
    activation_method=ActivationMethod.CLI17000,
    persistent_operation="portable.persist_ssh",
    temporary_operation="portable.start_ssh",
    reboot_operation="common.reboot",
    status_operation="common.read_ssh_state",
    recovery_operation="common.rollback_ssh",
    risk="medium-high",
    limitations=(
        "Only the exact researched 27.0.6.46330.5043500 SCM/taigan build is write-enabled.",
        "Never apply a stationary operation to a portable device.",
    ),
)
