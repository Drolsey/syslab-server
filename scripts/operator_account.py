"""Operator accounts for the dashboard at /admin (Step 11.3).

    py scripts\\operator_account.py new amro --name "Amro Taha"
    py scripts\\operator_account.py new amro --generate      print a strong password once
    py scripts\\operator_account.py list
    py scripts\\operator_account.py disable amro             also signs them out everywhere
    py scripts\\operator_account.py enable amro
    py scripts\\operator_account.py reset-password amro      also signs them out everywhere

This is how the first account is made: the dashboard has nobody to sign in as
until one exists. Passwords are typed without echo, twice, or generated and
printed once. Only an scrypt hash is stored.
"""

from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import operators  # noqa: E402


def _password(generate: bool) -> str:
    if generate:
        password = secrets.token_urlsafe(18)
        print(f"\n  Password (shown once, store it now): {password}\n")
        return password
    first = getpass.getpass("  Password: ")
    if getpass.getpass("  Again:    ") != first:
        raise operators.OperatorError("The two passwords did not match.")
    return first


def cmd_new(args) -> int:
    operator = operators.create(args.username, args.name or args.username, _password(args.generate))
    operators.audit(None, "operator_created", operator["id"], {"via": "cli"})
    print(f"  Created operator {operator['id']!r}. Sign in at http://<this box>:<port>/admin")
    return 0


def cmd_list(_args) -> int:
    rows = operators.list_all()
    if not rows:
        print("  No operators yet. Create one with: py scripts/operator_account.py new <username>")
    for row in rows:
        state = "active" if row["active"] else "DISABLED"
        print(f"  {row['id']:<20} {row['display_name']:<28} {state:<9} last login {row['last_login_at'] or '-'}")
    return 0


def cmd_disable(args) -> int:
    operators.set_disabled(args.username, True)
    operators.audit(None, "operator_disabled", args.username, {"via": "cli"})
    print(f"  Disabled {args.username!r} and ended their sessions.")
    return 0


def cmd_enable(args) -> int:
    operators.set_disabled(args.username, False)
    operators.audit(None, "operator_enabled", args.username, {"via": "cli"})
    print(f"  Enabled {args.username!r}.")
    return 0


def cmd_reset(args) -> int:
    operators.set_password(args.username, _password(args.generate))
    operators.audit(None, "password_reset", args.username, {"via": "cli"})
    print(f"  New password set for {args.username!r}; their sessions were ended.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("new", help="create an operator")
    p.add_argument("username")
    p.add_argument("--name", help="display name shown in the dashboard")
    p.add_argument("--generate", action="store_true", help="generate and print a password")
    p.set_defaults(run=cmd_new)

    p = subs.add_parser("list", help="list operators")
    p.set_defaults(run=cmd_list)

    for name, run, text in (("disable", cmd_disable, "disable and sign out"),
                            ("enable", cmd_enable, "undo disable")):
        p = subs.add_parser(name, help=text)
        p.add_argument("username")
        p.set_defaults(run=run)

    p = subs.add_parser("reset-password", help="set a new password and sign out")
    p.add_argument("username")
    p.add_argument("--generate", action="store_true", help="generate and print a password")
    p.set_defaults(run=cmd_reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.run(args)
    except operators.OperatorError as exc:
        print(f"  {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
