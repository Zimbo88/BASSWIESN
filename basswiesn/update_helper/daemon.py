"""Socket-activated host service. No TCP listener, shell, or automatic install."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import signal
import socket
import sys
import threading

from .deployment import SOCKET_PATH, verify_deployment
from .journal import Journal
from .onboarding import HOST_ROOT
from .service import UpdateService, serve_connection
from .transaction import TransactionRunner
from .host_executor import ProductionExecutor


def inherited_listener():
    """Accept exactly the socket supplied by this process's service manager."""
    if (os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1"
            or os.environ.get("LISTEN_FDNAMES") != "basswiesn-update"):
        raise ValueError("SOCKET_ACTIVATION_INVALID")
    listener = socket.socket(fileno=os.dup(3))
    try:
        if (listener.family != socket.AF_UNIX or listener.type != socket.SOCK_STREAM
                or not listener.getsockopt(socket.SOL_SOCKET, socket.SO_ACCEPTCONN)
                or listener.getsockname() != SOCKET_PATH):
            raise ValueError("SOCKET_ACTIVATION_INVALID")
        os.close(3)
        listener.set_inheritable(False)
        for name in ("LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES"):
            os.environ.pop(name, None)
        return listener
    except BaseException:
        listener.close()
        raise


def run_listener(listener, service, stop, *, workers=8):
    """Bounded concurrency. In-flight actions finish even after disconnection.

    Status stays responsive while one serialized transaction is in progress.
    Stop accepting on shutdown; a forced kill leaves durable recovery intent.
    """
    if type(workers) is not int or not 1 <= workers <= 8:
        raise ValueError("WORKER_LIMIT")
    slots = threading.BoundedSemaphore(workers)
    listener.settimeout(0.25)
    def handle(connection):
        try:
            serve_connection(connection, service)
        finally:
            slots.release()
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="update-client") as pool:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            if not slots.acquire(blocking=False):
                connection.close()
                continue
            try:
                pool.submit(handle, connection)
            except BaseException:
                connection.close()
                slots.release()
                raise


def main():
    journal = listener = None
    try:
        if os.geteuid() != 0 or not sys.flags.isolated or not sys.flags.no_site or sys.version_info < (3, 11):
            return 2
        authorization = verify_deployment()
        listener = inherited_listener()
        journal = Journal(Path(HOST_ROOT) / "journal")
        journal.recover_interrupted()  # no Docker action or job replay
        service = UpdateService(authorization, TransactionRunner(journal, ProductionExecutor()))
        stop = threading.Event()
        for number in (signal.SIGINT, signal.SIGTERM):
            signal.signal(number, lambda *_: stop.set())
        run_listener(listener, service, stop)
        return 0
    except Exception:
        return 2  # No secret/path/exception traceback in system logs.
    finally:
        if listener is not None:
            listener.close()
        if journal is not None:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
