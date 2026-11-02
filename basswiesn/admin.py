"""Local-only account bootstrap/recovery: python -m basswiesn.admin --help."""

import argparse
import getpass
import sys

from basswiesn.app.config import get_settings
from basswiesn.app.core.admin_store import AdminStore, AuthUnavailable, BootstrapConflict, LoginRejected


def main() -> int:
    parser = argparse.ArgumentParser(description="Local administrator setup; never connects to a radio")
    parser.add_argument("operation", choices=("bootstrap", "reset-password", "enable-api-tokens"))
    parser.add_argument("--username")
    args = parser.parse_args()
    if args.operation == "enable-api-tokens":
        from basswiesn.app.core.auth_schema import enable_api_tokens
        try:
            backup = enable_api_tokens(AdminStore(get_settings().data_dir / "secrets" / "admin" / "auth.sqlite3"))
        except (AuthUnavailable, OSError):
            print("Authentication schema upgrade refused; retained backups must be kept private.", file=sys.stderr)
            return 1
        print("API-token schema ready. Private backup retained in the administrator store." if backup else "API-token schema already current.")
        return 0
    if not args.username:
        parser.error("--username is required for account operations")
    if not sys.stdin.isatty():
        parser.error("Use an interactive terminal; passwords must not be passed as arguments or environment values")
    password = getpass.getpass("New password (15+ characters): ")
    if password != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        return 1
    store = AdminStore(get_settings().data_dir / "secrets" / "admin" / "auth.sqlite3")
    try:
        if args.operation == "bootstrap":
            store.bootstrap(args.username, password)
        else:
            store.reset_password(args.username, password)
    except (AuthUnavailable, BootstrapConflict, LoginRejected, ValueError, OSError):
        print("Account change refused. Check the account, password policy and private storage permissions locally.", file=sys.stderr)
        return 1
    print("Local account saved. Password reset revokes existing sessions. No device was contacted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
