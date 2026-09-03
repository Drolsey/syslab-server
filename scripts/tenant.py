"""Create and manage tenants.

    py scripts\\tenant.py new "Acme Ltd"
    py scripts\\tenant.py list
    py scripts\\tenant.py show <id>
    py scripts\\tenant.py disable <id>
    py scripts\\tenant.py enable <id>
    py scripts\\tenant.py token new <id> --label "amro laptop"
    py scripts\\tenant.py token list [<id>]
    py scripts\\tenant.py token revoke <fingerprint>

A token is shown once, at the moment it is created, and never again. The store
holds only its SHA-256, so there is no command that could print it back to you.
If it is lost, revoke it and issue another.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tenancy  # noqa: E402
from app.config import CONTROL_PATH  # noqa: E402

LINE = "-" * 62


def _short(stamp: str | None) -> str:
    """An ISO timestamp trimmed to the minute, for tables with fixed columns."""
    if not stamp:
        return "-"
    return stamp[:16].replace("T", " ")


def _print_token(token: str, tenant_id: str) -> None:
    print()
    print("  " + "=" * 60)
    print("  COPY THIS NOW. It is not stored and cannot be shown again.")
    print("  " + "=" * 60)
    print(f"\n    {token}\n")
    print(f"  Tenant: {tenant_id}")
    print(f"  Fingerprint: {tenancy.token_hash(token)[:12]}   (use this to revoke it)")
    print()


def cmd_new(args) -> int:
    tenant = tenancy.create_tenant(args.name, tenant_id=args.id)
    print(f"\nCreated tenant {tenant['id']}  ({tenant['name']})")
    token = tenancy.issue_token(tenant["id"], label=args.label)
    _print_token(token, tenant["id"])
    return 0


def cmd_list(args) -> int:
    tenants = tenancy.list_tenants(include_disabled=args.all)
    print(f"\nControl plane: {CONTROL_PATH}")
    if not tenants:
        print("\n  No tenants yet. Create one with:  py scripts\\tenant.py new \"Name\"\n")
        return 0
    print(f"\n  {'id':<14}{'name':<28}{'created (UTC)':<22}state")
    print("  " + LINE)
    for tenant in tenants:
        # Trim the timezone offset rather than let it run into the next column.
        # Every timestamp in the control plane is UTC, and the header says so.
        created = _short(tenant["created_at"])
        state = "active" if tenant["active"] else f"disabled {_short(tenant['disabled_at'])}"
        print(f"  {tenant['id']:<14}{tenant['name'][:26]:<28}{created:<22}{state}")
    print()
    return 0


def cmd_show(args) -> int:
    tenant = tenancy.get_tenant(args.id)
    if tenant is None:
        print(f"\n  No tenant with id {args.id!r}.\n")
        return 1
    print(f"\n  id        {tenant['id']}")
    print(f"  name      {tenant['name']}")
    print(f"  created   {tenant['created_at']}  (UTC)")
    print(f"  state     {'active' if tenant['active'] else 'disabled ' + tenant['disabled_at']}")
    tokens = tenancy.list_tokens(tenant["id"])
    print(f"\n  {len(tokens)} token(s)")
    for token in tokens:
        state = "active" if token["active"] else f"revoked {_short(token['revoked_at'])}"
        seen = _short(token["last_seen_at"]) if token["last_seen_at"] else "never used"
        print(f"    {token['fingerprint']}  {(token['label'] or '-'):<20}{state:<26}{seen}")
    print()
    return 0


def cmd_disable(args) -> int:
    tenant = tenancy.set_disabled(args.id, True)
    print(f"\n  {tenant['id']} disabled. Its tokens stop working immediately.")
    print("  Nothing on disk was touched; enable it again to restore access.\n")
    return 0


def cmd_enable(args) -> int:
    tenant = tenancy.set_disabled(args.id, False)
    print(f"\n  {tenant['id']} enabled.\n")
    return 0


def cmd_token_new(args) -> int:
    token = tenancy.issue_token(args.id, label=args.label)
    _print_token(token, args.id)
    return 0


def cmd_token_list(args) -> int:
    tokens = tenancy.list_tokens(args.id)
    if not tokens:
        print("\n  No tokens.\n")
        return 0
    print(f"\n  {'fingerprint':<14}{'tenant':<14}{'label':<20}{'state':<28}last seen")
    print("  " + LINE)
    for token in tokens:
        state = "active" if token["active"] else f"revoked {_short(token['revoked_at'])}"
        print(f"  {token['fingerprint']:<14}{token['tenant_id']:<14}"
              f"{(token['label'] or '-'):<20}{state:<26}"
              f"{_short(token['last_seen_at']) if token['last_seen_at'] else 'never'}")
    print()
    return 0


def cmd_token_revoke(args) -> int:
    result = tenancy.revoke_token(args.fingerprint)
    if result["already_revoked"]:
        print(f"\n  {result['fingerprint']} was already revoked. Nothing changed.\n")
    else:
        print(f"\n  {result['fingerprint']} revoked. Any device using it is signed out.\n")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage syslab tenants and their tokens.")
    subs = parser.add_subparsers(dest="command", required=True)

    p = subs.add_parser("new", help="create a tenant and issue its first token")
    p.add_argument("name")
    p.add_argument("--id", help="explicit id; normally leave this to be generated")
    p.add_argument("--label", help="what this first token is for, e.g. 'amro laptop'")
    p.set_defaults(func=cmd_new)

    p = subs.add_parser("list", help="list tenants")
    p.add_argument("--all", action="store_true", default=True,
                   help="include disabled tenants (default)")
    p.add_argument("--active", dest="all", action="store_false",
                   help="active tenants only")
    p.set_defaults(func=cmd_list)

    p = subs.add_parser("show", help="one tenant and its tokens")
    p.add_argument("id")
    p.set_defaults(func=cmd_show)

    p = subs.add_parser("disable", help="suspend a tenant; nothing is deleted")
    p.add_argument("id")
    p.set_defaults(func=cmd_disable)

    p = subs.add_parser("enable", help="undo disable")
    p.add_argument("id")
    p.set_defaults(func=cmd_enable)

    token = subs.add_parser("token", help="issue, list and revoke tokens")
    token_subs = token.add_subparsers(dest="token_command", required=True)

    p = token_subs.add_parser("new", help="issue another token for a tenant")
    p.add_argument("id")
    p.add_argument("--label")
    p.set_defaults(func=cmd_token_new)

    p = token_subs.add_parser("list", help="tokens, as metadata only")
    p.add_argument("id", nargs="?", default=None)
    p.set_defaults(func=cmd_token_list)

    p = token_subs.add_parser("revoke", help="revoke one token by fingerprint")
    p.add_argument("fingerprint")
    p.set_defaults(func=cmd_token_revoke)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except tenancy.TenancyError as exc:
        # An expected refusal, not a crash. One line, no traceback.
        print(f"\n  {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
