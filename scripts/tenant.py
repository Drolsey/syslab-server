"""Create and manage tenants.

    py scripts\\tenant.py new "Acme Ltd"
    py scripts\\tenant.py list
    py scripts\\tenant.py show <id>
    py scripts\\tenant.py disable <id>
    py scripts\\tenant.py enable <id>
    py scripts\\tenant.py token new <id> --label "amro laptop"
    py scripts\\tenant.py token list [<id>]
    py scripts\\tenant.py token revoke <fingerprint>
    py scripts\\tenant.py alias link website 42 <our id>
    py scripts\\tenant.py alias list [<our id>]
    py scripts\\tenant.py alias unlink website 42

A token is shown once, at the moment it is created, and never again. The store
holds only its SHA-256, so there is no command that could print it back to you.
If it is lost, revoke it and issue another.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import tenancy  # noqa: E402
from app.config import (  # noqa: E402
    BOOTSTRAP_TENANT, CONTROL_PATH, DATA_ROOT, DERIVED_ROOT, INDEX_ROOT,
)

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


# --------------------------------------------------------------------------
# deleting, which is the one thing here that cannot be undone
# --------------------------------------------------------------------------

REMOVED = "_removed"


def _app_is_running() -> bool:
    from app.config import APP_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", APP_PORT)) == 0


def _count_files(folder: Path) -> tuple[int, int]:
    if not folder.exists():
        return 0, 0
    files = [p for p in folder.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def cmd_delete(args) -> int:
    tenant = tenancy.get_tenant(args.id)
    if tenant is None:
        print(f"\n  No tenant with id {args.id!r}.\n")
        return 1

    folder = DATA_ROOT / tenant["id"]
    index = INDEX_ROOT / f"{tenant['id']}.sqlite3"
    # Step 2.1 gave a tenant a third thing on disk. It holds text extracted
    # from their documents, so leaving it behind after a delete leaves a copy
    # of the customer's content in a folder nothing points at any more.
    derived = DERIVED_ROOT / tenant["id"]
    count, size = _count_files(folder)
    tokens = tenancy.list_tokens(tenant["id"])

    print(f"\n  Tenant   {tenant['id']}  ({tenant['name']})")
    print(f"  State    {'ACTIVE' if tenant['active'] else 'disabled ' + _short(tenant['disabled_at'])}")
    print(f"  Files    {count} file(s), {size / 1048576:.1f} MB in {folder}")
    print(f"  Index    {'present' if index.exists() else 'absent'}  {index}")
    print(f"  Derived  {'present' if derived.exists() else 'absent'}  {derived}")
    print(f"  Tokens   {len(tokens)}")

    if not args.apply:
        print("\n  This is a dry run. Nothing has changed.")
        print("  To do it:")
        print(f"    py scripts\\tenant.py delete {tenant['id']} --apply --confirm {tenant['id']}")
        print("  The files are MOVED aside by default, not destroyed. Add --purge-files")
        print("  to remove them for good.\n")
        return 0

    # ---- the refusals ----------------------------------------------------
    if args.confirm != tenant["id"]:
        print(f"\n  REFUSING: --confirm must repeat the tenant id exactly. Naming it "
              f"twice is the point; deleting the wrong customer is not a mistake "
              f"worth being efficient about.\n")
        return 1

    if tenant["id"] == BOOTSTRAP_TENANT:
        print(f"\n  REFUSING: {tenant['id']!r} is the bootstrap tenant, which holds this")
        print("  install's own documents and is what your .env APP_TOKEN signs in as.")
        print("  Deleting it would empty your own assistant. If you genuinely mean to,")
        print("  point BOOTSTRAP_TENANT in .env at something else first.\n")
        return 1

    if tenant["active"]:
        print("\n  REFUSING: this tenant is still active. Disable it first, check that")
        print("  nothing broke, then delete it:")
        print(f"    py scripts\\tenant.py disable {tenant['id']}")
        print("  Two steps on purpose: the first can be undone and this one cannot.\n")
        return 1

    if _app_is_running():
        print("\n  REFUSING: something is listening on the app's port, so the service is")
        print("  probably running and may hold a file open. Stop it first:")
        print("    powershell -ExecutionPolicy Bypass -File "
              "scripts\\service\\control_windows.ps1 -Action stop\n")
        return 1

    # ---- do it -----------------------------------------------------------
    print("\n  Removing")
    print("  " + LINE)

    removed = tenancy.delete_tenant(tenant["id"])
    print(f"  control plane: {removed['tokens']} token(s), {removed['users']} user(s), "
          f"{removed['tenant_database']} database row(s), and the tenant itself")

    if index.exists():
        index.unlink()
        print(f"  index:         deleted {index.name} (derived, rebuildable)")

    # Deleted rather than moved aside, even when the documents are only moved.
    # Everything in it can be made again from the files, so keeping it would
    # preserve a second copy of the customer's text for no benefit.
    if derived.exists():
        shutil.rmtree(derived)
        print("  derived:       deleted (artifacts and manifest, rebuildable)")

    if not folder.exists():
        print("  files:         none on disk")
    elif args.purge_files:
        shutil.rmtree(folder)
        print(f"  files:         DELETED {count} file(s) permanently")
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = DATA_ROOT / REMOVED / f"{tenant['id']}-{stamp}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(folder), str(destination))
        print(f"  files:         moved {count} file(s) to {destination}")
        print("                 access is gone; the documents are not. Delete that")
        print("                 folder by hand when you are sure.")

    print("\n  Done. The tenant cannot sign in, and nothing of theirs is reachable")
    print("  through the app.\n")
    return 0


def cmd_alias_link(args) -> int:
    """Point another system's id at one of ours. Step 4.0's operator surface.

    This is the ONLY way an alias is created. Nothing in the app writes to
    tenant_alias: a link decides which customer's documents a foreign id can
    reach, and that is a deliberate act by a person, not something a request
    can do to itself.
    """
    link = tenancy.link_alias(args.system, args.external_id, args.id, label=args.label)
    print(f"\n  {link['external_system']}:{link['external_id']}  ->  {link['tenant_id']}")
    print("\n  The retrieval plane will now resolve that id to that tenant.")
    print("  Nothing else changed: no token was issued and no file was touched.\n")
    return 0


def cmd_alias_unlink(args) -> int:
    removed = tenancy.unlink_alias(args.system, args.external_id)
    if not removed:
        print(f"\n  Nothing linked for {args.system}:{args.external_id}.\n")
        return 1
    print(f"\n  {args.system}:{args.external_id} unlinked.")
    print("  That id now answers 404 on the retrieval plane. The tenant, its")
    print("  documents and its tokens are untouched.\n")
    return 0


def cmd_alias_list(args) -> int:
    aliases = tenancy.list_aliases(args.id)
    if not aliases:
        where = f" for {args.id}" if args.id else ""
        print(f"\n  No aliases{where}. Link one with:")
        print("    py scripts\\tenant.py alias link website 42 <our id>\n")
        return 0
    print(f"\n  {'system':<14}{'their id':<26}{'our tenant':<14}{'linked (UTC)':<22}label")
    print("  " + LINE)
    for alias in aliases:
        print(f"  {alias['external_system']:<14}{alias['external_id'][:24]:<26}"
              f"{alias['tenant_id']:<14}{_short(alias['created_at']):<22}"
              f"{alias['label'] or '-'}")
    print()
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

    p = subs.add_parser("delete", help="remove a disabled tenant for good")
    p.add_argument("id")
    p.add_argument("--apply", action="store_true", help="actually do it")
    p.add_argument("--confirm", default="", help="repeat the tenant id to confirm")
    p.add_argument("--purge-files", action="store_true",
                   help="delete the documents too, instead of moving them aside")
    p.set_defaults(func=cmd_delete)

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

    alias = subs.add_parser(
        "alias", help="link another system's tenant id to one of ours")
    alias_subs = alias.add_subparsers(dest="alias_command", required=True)

    p = alias_subs.add_parser("link", help="point a foreign id at one of our tenants")
    p.add_argument("system", help="which system the id comes from, e.g. 'website'")
    p.add_argument("external_id", help="their id for the customer, exactly as they send it")
    p.add_argument("id", help="our tenant id")
    p.add_argument("--label", help="a note, e.g. 'acme, migrated 11 Sep'")
    p.set_defaults(func=cmd_alias_link)

    p = alias_subs.add_parser("unlink", help="remove a link; the tenant is untouched")
    p.add_argument("system")
    p.add_argument("external_id")
    p.set_defaults(func=cmd_alias_unlink)

    p = alias_subs.add_parser("list", help="every link, or one tenant's")
    p.add_argument("id", nargs="?", default=None)
    p.set_defaults(func=cmd_alias_list)

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
