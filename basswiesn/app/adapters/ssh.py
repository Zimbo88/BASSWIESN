"""Fixed OpenSSH compatibility arguments for legacy SoundTouch firmware."""


LEGACY_SSH_ALGORITHM_OPTIONS = (
    "HostKeyAlgorithms=+ssh-rsa",
    "PubkeyAcceptedAlgorithms=+ssh-rsa",
    "KexAlgorithms=+diffie-hellman-group14-sha1,diffie-hellman-group1-sha1",
    "Ciphers=+aes256-cbc,aes128-cbc",
)


def legacy_ssh_options(
    *, connect_timeout: int = 5, batch_mode: bool = True
) -> tuple[str, ...]:
    """Return the single canonical option set used by every SSH caller."""
    return (
        f"BatchMode={'yes' if batch_mode else 'no'}",
        *LEGACY_SSH_ALGORITHM_OPTIONS,
        "StrictHostKeyChecking=no",
        "UserKnownHostsFile=/dev/null",
        f"ConnectTimeout={connect_timeout}",
        "LogLevel=ERROR",
    )


def build_legacy_ssh_command(
    ip_address: str,
    username: str,
    remote_command: str,
    *,
    connect_timeout: int = 5,
    batch_mode: bool = True,
) -> list[str]:
    """Build a non-interactive SSH invocation compatible with Bose radios."""
    options = legacy_ssh_options(
        connect_timeout=connect_timeout, batch_mode=batch_mode
    )
    command = ["ssh"]
    for option in options:
        command.extend(("-o", option))
    command.extend((f"{username}@{ip_address}", remote_command))
    return command
