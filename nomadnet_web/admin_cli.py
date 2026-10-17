"""Console commands for the admin account, run inside the container:

    docker exec -it <container> python -m nomadnet_web.admin_cli reset-password
    docker exec -it <container> python -m nomadnet_web.admin_cli status

``reset-password`` asks for the new password twice (nothing is echoed or
printed back). ``--username`` renames the admin, and is required on an
install that has no account yet. ``--password-stdin`` reads one line from
standard input instead, for scripts. A reset signs the admin out everywhere.

This is the only way to change the stored admin password: the web interface
has no reset by design (there is no email to prove who you are).
"""

import argparse
import getpass
import os
import sys

from .admin_account import AdminAccount


def default_config_dir() -> str:
    """The app's config directory is the parent of the Reticulum one."""
    return os.environ.get("CONFIG_DIR") or os.path.dirname(
        os.environ.get("RNS_CONFIG_DIR", "/config/reticulum").rstrip("/")) or "/config"


def main(argv=None, prompt=getpass.getpass, stdin=None, out=None, err=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    parser = argparse.ArgumentParser(prog="python -m nomadnet_web.admin_cli", description=__doc__.split("\n\n")[0])
    parser.add_argument("--config-dir", default=default_config_dir(),
                        help="where the app keeps its files (default: the folder above RNS_CONFIG_DIR, i.e. /config)")
    sub = parser.add_subparsers(dest="command", required=True)
    reset = sub.add_parser("reset-password", help="set a new admin password")
    reset.add_argument("--username", help="rename the admin (required if no account exists yet)")
    reset.add_argument("--password-stdin", action="store_true", help="read the password from standard input")
    sub.add_parser("status", help="show whether an admin account exists")
    args = parser.parse_args(argv)

    account = AdminAccount(args.config_dir)

    if args.command == "status":
        name = account.username()
        if name:
            print(f"Admin account: {name}", file=out)
        else:
            print("No admin account yet. Open the web interface to set one up.", file=out)
        return 0

    if args.password_stdin:
        password = stdin.readline().rstrip("\r\n")
    else:
        password = prompt("New admin password: ")
        if password != prompt("Again: "):
            print("The two passwords do not match. Nothing was changed.", file=err)
            return 1

    problem = account.set_password(password, username=args.username)
    if problem:
        print(problem + " Nothing was changed.", file=err)
        return 1
    print("Admin password changed. Everyone signed in as the admin is signed out.", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
