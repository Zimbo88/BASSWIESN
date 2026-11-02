# Reproducing the Python environment

Runtime dependencies are declared in `requirements.in`. Development/test tools
are declared separately in `requirements-dev.in`. The generated `.lock` files
pin the complete dependency trees and permitted distribution SHA-256 hashes.
Development uses the runtime lock as a constraint, so tests do not silently run a
different FastAPI/SQLAlchemy stack from the shipped application.

The initial lock retains the installed baseline versions; it is not a blanket
dependency upgrade. Universal resolution includes platform markers for Python
3.11 and newer. CI tests selected supported Python versions; universal resolution
does not itself establish that every interpreter/platform works.

## Install

Use a fresh virtual environment. Runtime:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements.txt
```

Development (includes runtime and the pinned lock generator):

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --only-binary=:all: -r requirements-dev.lock
.venv/bin/python -m playwright install chromium webkit
```

`--only-binary=:all:` prevents implicit, unlocked source-build dependencies.
An unsupported wheel platform must fail; do not remove hashes to make it pass.
Playwright's Python version pins its browser revision; OS browser libraries are
separate system dependencies. The runtime container does not install test tools.

## Deliberate updates

```sh
PATH="$PWD/.venv/bin:$PATH" bash tools/lock_dependencies.sh
python3 tools/check_dependency_lock.py
# A reviewed, targeted upgrade:
PATH="$PWD/.venv/bin:$PATH" bash tools/lock_dependencies.sh --upgrade-package httpx
```

Commit direct declarations, both locks and `dependency-lock.json` together.
The manifest detects stale input/lock changes; pip verifies every installed
distribution. Regeneration prefers existing pins. Review the diff and run the
full software suite before accepting an upgrade. `pip-audit` must not have a
blanket vulnerability ignore list.

Hashes prove integrity against the reviewed lock, not that an upstream package
is trustworthy. Registry availability and platform wheel availability are still
external dependencies. The Python lock alone does not make OS packages
or the complete OCI image bit-reproducible. Release source archive determinism,
image provenance and base-image/OS maintenance are separate gates.

## Runtime base and architecture

The container uses the digest-pinned official Python 3.12.14 image on stable
Alpine 3.24. This removes unused Debian administration packages rather than
waiving their unresolved scanner findings. It changes the container's C library
from glibc to musl; it does **not** change the Raspberry Pi host operating system.
Both AMD64 (ordinary Intel/AMD Linux servers) and ARM64 (64-bit Raspberry Pi)
must install the existing hash-locked binary wheels, pass `pip check`, and run
the real authentication, migration and updater-validation probes. A clean scan
alone does not qualify a different runtime.

The runtime explicitly retains Bash, OpenSSL, sshpass and IANA timezone data.
Clock/seasonal-offset and legacy SSH option checks protect existing integration
contracts. Build tools stay in the separate SSH build stage. The image still
runs as UID/GID 10001 with read-only filesystems and no capabilities. OS packages
receive stable-branch security updates at build time; exact image digests and
SBOMs identify qualified artifacts, not a promise of bit-identical future builds.

## SSH client source lock

The container builds the OpenSSH client in a separate stage because the reviewed
stable-distribution client does not yet contain the required upstream security
fix. `tools/openssh-source.json` pins the official source archive, detached
signature, release-key file and signing fingerprint. The build verifies content
hashes and the signature before extracting or compiling anything. Redirects,
unexpected signers and unsafe archive members fail closed.

Only client executables and their required helpers enter the runtime, alongside
the upstream licence and a binary-hash manifest under
`/usr/local/share/doc/openssh/`. No SSH server, compiler or setuid executable is
installed by this stage. The application keeps its explicit per-device legacy
algorithm options; newer SSH defaults are not a reason to weaken device identity
or write verification. Offline configuration parsing is not hardware acceptance.

This source component must be reviewed separately from the distribution-package
scanner: absence of an `openssh-client` package entry is **not** a clean bill of
health. A source update requires reviewing the official release notes, checking
the signer independently, updating all pins together and retesting both runtime
architectures. System build dependencies remain separately maintained; source
pinning alone does not promise a bit-identical complete container image.

## Version source

`basswiesn.__version__` remains canonical. Docker's default version argument is
a checked mirror, not an independently chosen version. `tools/check_version.py`
rejects drift; the image build rejects mismatching overrides. UI/API tests check
the runtime value and cache-busting URLs. Old release-note filenames are history,
not active version sources. The 5.0.0 candidate also checks the distributed
environment template; a preserved installation environment cannot override
the running package version. Candidate/stable status is explicit package
metadata, separate from an observed public release date.
