# Host flight recorder

The optional BASSWIESN observer records evidence for intermittent failures on a
Linux Docker host. It does not repair faults or reboot anything. It uses only
host files, the local Docker Unix socket and the system journal. It never scans
the LAN or sends a radio request.

## Captured information

- Available/free memory, swap counters, CPU/load, uptime, pressure and temperature.
- Process names, PIDs, state, thread count and resident/virtual memory, not command
  arguments or environments.
- BASSWIESN container state, start/finish time, restart count, exit code and OOM
  indication; bounded Docker logs and one-shot CPU/memory statistics.
- Incremental application `.log` files directly in the data folder or its
  `logs` subdirectory; kernel and warning-level system journal entries.
- Free disk space and a UTC timeline, continuing independently of a laptop.

Samples are taken approximately once per minute. Busy commands have time and
output limits. Files, processes or log lines beyond those limits can be omitted:
this is a bounded recorder, not a promise to capture every event. It does not read
databases, credential files, packet contents, audio or crash/core files.

## Installation

Run on the Docker host, from the unpacked release directory. The data directory
must be the one actually mounted by the running BASSWIESN container. Preview
first; only `--install` enables the service:

```bash
python3 tools/install_observer.py --data-dir "$PWD/data"
sudo python3 tools/install_observer.py --data-dir "$PWD/data" --install
sudo systemctl status basswiesn-observer.service
```

The installer requires Python3, Docker and systemd. It installs the recorder in
`/opt/basswiesn-observer`, private recordings in `/var/lib/basswiesn-observer`
and one systemd service. Existing recorder files/configuration are backed up
under the installation directory before replacement. After moving the release
to a new directory, repeat the install command with its new data path.

The service is enabled at boot and restarted on failure. Resource limits are
128 MiB memory, 15% of one CPU and low scheduling priority. Network address
families are restricted to Unix sockets; IP traffic is denied by the unit.
The observer does not need a radio password, SSH enablement or cloud token.

## Retention and privacy

Recordings are private to root. Recognized secrets, authentication headers and
query parameters are omitted. Arbitrary external logs can still contain private
information: **do not publish the recordings without a separate review**.

Logs rotate at approximately one hour or 8 MiB. Closed segments are gzip files
with individual SHA256 sidecars. The rolling retention is fourteen days with
a 1 GiB archive budget and a 512 MiB free-space reserve. Only the observer's
strictly named closed segments are automatically removed. When storage is
scarce, recording pauses rather than writing elsewhere. Oldest retained data
may therefore cover less than fourteen days. The current open segment has no
final checksum until rotation.

This host recorder cannot recover logs that vanished from a radio's memory
before collection. A separately installed USB recorder on a radio remains an
independent, private diagnostic tool; this installer never touches it.

## Stop and inspect

```bash
sudo systemctl stop basswiesn-observer.service
sudo systemctl disable basswiesn-observer.service
sudo journalctl -u basswiesn-observer.service --no-pager -n 30
```

Stopping the recorder preserves its files. Before copying evidence, stop it or
copy only completed `.jsonl.gz` segments and their `.sha256` files. Validate
each sidecar from the directory containing its segment using `sha256sum -c`.
No automatic upload, service repair or radio action is performed.
