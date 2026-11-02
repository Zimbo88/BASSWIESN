#!/usr/bin/env python3
"""Bounded, host-only flight recorder. No radio requests or corrective actions.

Private evidence only: redaction reduces accidental disclosure, not a promise
that arbitrary third-party logs are safe to publish. No environment, packet
capture, audio, database contents, process arguments or core dumps are read.
"""
from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import time

MAX_OUTPUT = 512 * 1024
MAX_SEGMENT = 8 * 1024 * 1024
MAX_TOTAL = 1024 * 1024 * 1024
MIN_FREE = 512 * 1024 * 1024
RETENTION_DAYS = 14
SENSITIVE = re.compile(r"(?i)(?:\b(?:secrets?|password|passwd|authorization|cookies?|credentials?|token|private.?key|pairing.?key|api.?key|client.?secret|wpa_psk)\b|-----BEGIN .*PRIVATE KEY|\bBearer\s+\S+)")


def redact(line):
    if SENSITIVE.search(line) or re.fullmatch(r"[A-Za-z0-9+/=]{64,}", line.strip()):
        return "[sensitive log line omitted]"
    line = re.sub(r"(https?://)[^/\s@]+@", r"\1[credentials]@", line)
    line = re.sub(r"(https?://[^\s?<>\"]+)\?[^\s<>\"]+", r"\1?[query omitted]", line)
    return line[:8192]


def command(args, timeout=8):
    """Bound stdout in memory and time; never use a shell or inherited Docker target."""
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    except OSError as exc:
        return {"error": type(exc).__name__, "output": ""}
    output = bytearray(); truncated = False
    deadline = time.monotonic() + timeout
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while time.monotonic() < deadline:
                if not selector.select(min(0.2, max(0, deadline-time.monotonic()))):
                    continue
                block = os.read(proc.stdout.fileno(), min(65536, MAX_OUTPUT-len(output)))
                if not block: break
                output.extend(block)
                if len(output) >= MAX_OUTPUT:
                    truncated = True; break
            else:
                truncated = True
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=2)
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=2)
        proc.stdout.close()
    return {"returncode": proc.returncode, "truncated": truncated,
            "output": "\n".join(redact(line) for line in output.decode("utf-8", "replace").splitlines())}


def read_small(path, limit=65536):
    try:
        with Path(path).open("r", errors="replace") as stream:
            return stream.read(limit)
    except OSError:
        return ""


def host_sample(proc_root=Path("/proc"), sys_root=Path("/sys")):
    memory = {}
    for line in read_small(proc_root / "meminfo").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1].isdigit(): memory[parts[0].rstrip(":")] = int(parts[1])
    processes=[]
    for path in sorted(proc_root.glob("[0-9]*/status"))[:2048]:
        fields={}
        for line in read_small(path, 8192).splitlines():
            key, _, value = line.partition(":")
            if key in {"Name", "State", "Pid", "PPid", "VmRSS", "VmSize", "Threads"}:
                fields[key] = value.strip()
        if fields: processes.append(fields)
    return {"memory_kib": memory, "load": read_small(proc_root / "loadavg", 256).strip(),
            "uptime": read_small(proc_root / "uptime", 256).strip(),
            "cpu_counters": read_small(proc_root / "stat", 32768),
            "pressure": {kind:read_small(proc_root / "pressure" / kind, 1024) for kind in ("cpu", "memory", "io")},
            "temperature_millidegrees": read_small(sys_root / "class/thermal/thermal_zone0/temp", 64).strip(),
            "processes": processes}


class Archive:
    def __init__(self, root, *, max_total=MAX_TOTAL, min_free=MIN_FREE):
        self.root = root
        self.max_total, self.min_free = max_total, min_free
        self.segment_started = time.time()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)

    def append(self, record, instant):
        # Only this recorder's exact file patterns are eligible for retention.
        raw = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
        if len(raw) > MAX_SEGMENT: raise ValueError("Oversized recorder sample")
        if shutil.disk_usage(self.root).free < self.min_free + len(raw) + MAX_SEGMENT:
            return False  # no flash/root filesystem fallback
        self.rotate(instant)
        current = self.root / "current.jsonl"
        if current.is_symlink(): raise PermissionError("Refusing symlink log")
        with current.open("ab") as stream:
            os.chmod(current, 0o600)
            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
        return True

    def rotate(self, instant):
        current = self.root / "current.jsonl"
        if current.is_symlink(): raise PermissionError("Refusing symlink log")
        if current.exists() and (current.stat().st_size >= MAX_SEGMENT or instant.timestamp()-self.segment_started >= 3600):
            name = instant.strftime("sample-%Y%m%dT%H%M%S%fZ.jsonl.gz")
            target = self.root / name
            with target.open("xb") as output:
                os.chmod(target, 0o600)
                with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=0) as zipped:
                    with current.open("rb") as source: shutil.copyfileobj(source, zipped, 65536)
                output.flush(); os.fsync(output.fileno())
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            checksum = target.with_name(target.name + ".sha256")
            checksum.write_text(f"{digest}  {target.name}\n"); os.chmod(checksum, 0o600)
            current.unlink()
            self.segment_started = instant.timestamp()
        archives = sorted(path for path in self.root.glob("sample-*.jsonl.gz") if re.fullmatch(r"sample-\d{8}T\d{12}Z\.jsonl\.gz", path.name) and not path.is_symlink())
        total = sum(p.stat().st_size for p in archives)
        for path in archives:
            if total <= self.max_total - MAX_SEGMENT and instant.timestamp()-path.stat().st_mtime < RETENTION_DAYS*86400:
                continue
            total -= path.stat().st_size
            path.unlink()
            path.with_name(path.name + ".sha256").unlink(missing_ok=True)


class Observer:
    def __init__(self, data_dir, archive, runner=command):
        self.data_dir, self.archive, self.runner = data_dir, archive, runner
        self.offsets={}
        self.previous = datetime.now(UTC) - timedelta(seconds=60)

    def docker(self, *args):
        return self.runner(["docker", "--host", "unix:///var/run/docker.sock", *args])

    def tail_files(self):
        results=[]
        if not self.data_dir: return results
        # Flat, explicit log locations only. No DB/backups/env/private key traversal.
        files=list(self.data_dir.glob("*.log")) + list((self.data_dir / "logs").glob("*.log"))
        for path in sorted(files)[:16]:
            if path.is_symlink() or not path.is_file(): continue
            state = path.stat()
            old_inode, old_offset = self.offsets.get(str(path), (state.st_ino, max(0, state.st_size-131072)))
            offset = old_offset if old_inode == state.st_ino and old_offset <= state.st_size else 0
            with path.open("rb") as source:
                source.seek(offset)
                raw=source.read(131072)
                self.offsets[str(path)] = (state.st_ino, source.tell())
            if raw:
                results.append({"name":path.name, "offset":offset, "lines":[redact(line) for line in raw.decode("utf-8", "replace").splitlines()]})
        return results

    def sample(self):
        now = datetime.now(UTC)
        since, until = self.previous.isoformat(), now.isoformat()
        listing = self.docker("ps", "-a", "--filter", "label=com.docker.compose.service=basswiesn", "--format", "{{.ID}}")
        ids = [value for value in listing.get("output", "").splitlines() if re.fullmatch(r"[a-f0-9]{12,64}", value)][:4]
        containers=[]
        for identifier in ids:
            # No full inspect: deliberately exclude Env, mounts, auth and arguments.
            state = self.docker("inspect", "--format", '{"id":{{json .Id}},"state":{{json .State.Status}},"running":{{json .State.Running}},"pid":{{.State.Pid}},"oom":{{.State.OOMKilled}},"exit":{{.State.ExitCode}},"started":{{json .State.StartedAt}},"finished":{{json .State.FinishedAt}},"restarts":{{.RestartCount}}}', identifier)
            logs = self.docker("logs", "--timestamps", "--tail", "1000", "--since", since, "--until", until, identifier)
            stats = self.docker("stats", "--no-stream", "--format", "{{json .}}", identifier)
            containers.append({"state":state, "logs":logs, "stats":stats})
        record = {"utc":until, "version":1, "host":host_sample(), "container_listing":listing,
                  "containers":containers, "application_logs":self.tail_files(),
                  "kernel":self.runner(["journalctl", "--kernel", "--no-pager", "-o", "short-iso", "-n", "1000", "--since", since, "--until", until]),
                  "warnings":self.runner(["journalctl", "--no-pager", "-p", "warning", "-o", "short-iso", "-n", "1000", "--since", since, "--until", until]),
                  "disk":dict(zip(("total", "used", "free"), shutil.disk_usage(self.archive.root)))}
        self.previous = now
        return self.archive.append(record, now)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--once", action="store_true")
    args=parser.parse_args()
    os.umask(0o077)
    config=json.loads(args.config.read_text())
    archive=Archive(Path(config["output_dir"]))
    lock=archive.root / "observer.lock"
    if lock.is_symlink(): raise PermissionError("Refusing symlink lock")
    with lock.open("a") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        observer=Observer(Path(config["data_dir"]) if config.get("data_dir") else None, archive)
        stopping=False
        def stop(*_):
            nonlocal stopping
            stopping=True
        signal.signal(signal.SIGTERM, stop); signal.signal(signal.SIGINT, stop)
        while not stopping:
            started=time.monotonic()
            try:
                if not observer.sample(): print("Recorder paused: reserved free space", flush=True)
            except Exception as exc:
                print(f"Recorder sample failed: {type(exc).__name__}", flush=True)
            if args.once: break
            while not stopping and time.monotonic()-started < 60: time.sleep(1)


if __name__ == "__main__":
    main()
