"""Profiles for SoundTouch 20 and SoundTouch 30 families."""

from .base import ActivationMethod, SshProfile


ST20_LEGACY_PROFILE = SshProfile(
    key="soundtouch-20-scm-27.0.6.46330.5043500",
    model_family="stationary",
    supported_model_patterns=("soundtouch 20",),
    firmware_patterns=("27.0.6.46330.5043500",),
    product_id="0x0923",
    variants=("spotty",),
    platforms=("scm",),
    ssh_daemon="dropbear-or-openssh",
    port=22,
    activation_method=ActivationMethod.CLI17000,
    persistent_operation="stationary.persist_ssh",
    temporary_operation="stationary.start_ssh",
    reboot_operation="common.reboot",
    status_operation="common.read_ssh_state",
    recovery_operation="common.rollback_ssh",
    risk="medium",
    limitations=(
        "CLI-17000 operation is firmware-dependent and requires readback.",
        "Unknown firmware must remain preview-only.",
    ),
)

ST30_LEGACY_PROFILE = SshProfile(
    key="soundtouch-30-scm-27.0.6.46330.5043500",
    model_family="stationary",
    supported_model_patterns=("soundtouch 30",),
    firmware_patterns=("27.0.6.46330.5043500",),
    product_id="0x0924",
    variants=("mojo",),
    platforms=("scm",),
    ssh_daemon="dropbear-or-openssh",
    port=22,
    activation_method=ActivationMethod.CLI17000,
    persistent_operation="stationary.persist_ssh",
    temporary_operation="stationary.start_ssh",
    reboot_operation="common.reboot",
    status_operation="common.read_ssh_state",
    recovery_operation="common.rollback_ssh",
    risk="medium",
    limitations=(
        "Only the exact researched 27.0.6.46330.5043500 SCM/mojo build is write-enabled.",
        "Unknown firmware, variant or platform remains preview-only.",
    ),
)

ST20_SERIES_III_PROFILE = SshProfile(
    key="soundtouch-20-sm2-27.0.6.46330.5043500",
    model_family="stationary",
    supported_model_patterns=("soundtouch 20",),
    firmware_patterns=("27.0.6.46330.5043500",),
    product_id="0x093B",
    variants=("spotty",),
    platforms=("sm2",),
    ssh_daemon="dropbear-or-openssh",
    port=22,
    activation_method=ActivationMethod.CLI17000,
    persistent_operation="stationary.persist_ssh",
    temporary_operation="stationary.start_ssh",
    reboot_operation="common.reboot",
    status_operation="common.read_ssh_state",
    recovery_operation="common.rollback_ssh",
    risk="medium",
    limitations=(
        "Only the exact researched 27.0.6.46330.5043500 SM2/spotty build is write-enabled.",
        "Unknown firmware, variant or platform remains preview-only.",
    ),
)

ST30_SERIES_III_PROFILE = SshProfile(
    key="soundtouch-30-sm2-27.0.6.46330.5043500",
    model_family="stationary",
    supported_model_patterns=("soundtouch 30",),
    firmware_patterns=("27.0.6.46330.5043500",),
    product_id="0x093C",
    variants=("mojo",),
    platforms=("sm2",),
    ssh_daemon="dropbear-or-openssh",
    port=22,
    activation_method=ActivationMethod.CLI17000,
    persistent_operation="stationary.persist_ssh",
    temporary_operation="stationary.start_ssh",
    reboot_operation="common.reboot",
    status_operation="common.read_ssh_state",
    recovery_operation="common.rollback_ssh",
    risk="medium",
    limitations=(
        "Only the exact researched 27.0.6.46330.5043500 SM2/mojo build is write-enabled.",
        "Unknown firmware, variant or platform remains preview-only.",
    ),
)

# Backwards-compatible symbol for callers which only need one representative
# stationary profile. Detection uses the complete profile tuple below.
STATIONARY_PROFILE = ST30_LEGACY_PROFILE
STATIONARY_PROFILES = (
    ST20_LEGACY_PROFILE,
    ST30_LEGACY_PROFILE,
    ST20_SERIES_III_PROFILE,
    ST30_SERIES_III_PROFILE,
)
