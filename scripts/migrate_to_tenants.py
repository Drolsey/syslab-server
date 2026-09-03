"""Move this install's existing data into a tenant folder. Sub-step 1.3.

    py scripts\\migrate_to_tenants.py              # dry run, changes nothing
    py scripts\\migrate_to_tenants.py --apply      # do it
    py scripts\\migrate_to_tenants.py --undo       # put it back

Before Step 1 every document lived directly in data\\ and there was one search
index. Now each tenant has a folder and an index of its own, so the files that
are already there have to become somebody's. They become BOOTSTRAP_TENANT,
which is "default" unless .env says otherwise.

STOP THE APP FIRST. Windows will not let you move a file the running service
has open, and a half-moved data folder is the worst state this can be in. The
script checks the port and refuses if something is listening.

What it does, in order:
  1. refuses if the app is running, or if the target folder already has files
  2. creates the tenant in the control plane if it is not there
  3. moves every entry in data\\ into data\\<tenant>\\
  4. writes an undo manifest next to the control plane
  5. deletes the old single index and rebuilds the tenant's from the files
  6. counts before and after, and says so

The index is rebuildable from the documents at every point, so it is never the
thing that has to survive. The files are, which is why every move is recorded.
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
# Its own folder too, so this file can be imported by the tests as well as run
# as a script. Run as __main__ python adds it; imported, nothing does.
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _deps  # noqa: E402

# Before anything else, and before a single file moves. Run under an
# interpreter without these and the move still works while the index rebuild
# silently produces nothing, which is how a successful-looking migration left
# nineteen readable documents unsearchable.
_deps.require("pymupdf", "openpyxl")

from app import context, search, tenancy  # noqa: E402
from app.config import (  # noqa: E402
    APP_PORT,
    BOOTSTRAP_TENANT,
    CONTROL_DIR,
    DATA_ROOT,
    INDEX_ROOT,
    ensure_control_dir,
)

LINE = "-" * 62
MANIFEST = "migration-1.3-undo.json"
OLD_INDEX = "documents.sqlite3"


def section(title: str) -> None:
    print(f"\n{title}\n{LINE}")


def app_is_running() -> bool:
    """Is something listening on the app's port? Then it may hold a file open."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1.0)
        return probe.connect_ex(("127.0.0.1", APP_PORT)) == 0


def movable(root: Path, target: Path) -> list[Path]:
    """Everything in data\\ that is not the target folder or a keep-file."""
    if not root.exists():
        return []
    return sorted(
        entry for entry in root.iterdir()
        if entry != target and entry.name not in {".gitkeep", MANIFEST}
    )


def count_files(folder: Path) -> int:
    if not folder.exists():
        return 0
    return sum(1 for p in folder.rglob("*") if p.is_file() and p.name != ".gitkeep")


# --------------------------------------------------------------------------

def plan(tenant: str) -> tuple[Path, list[Path], int]:
    target = DATA_ROOT / tenant
    entries = movable(DATA_ROOT, target)
    return target, entries, count_files(DATA_ROOT)


def show(tenant: str) -> int:
    target, entries, before = plan(tenant)
    section("What would move")
    print(f"  From:   {DATA_ROOT}")
    print(f"  Into:   {target}")
    print(f"  Tenant: {tenant}\n")
    if not entries:
        print("  Nothing to move. Either this has already run, or the folder is empty.")
    for entry in entries[:40]:
        kind = "dir " if entry.is_dir() else "file"
        print(f"    {kind}  {entry.name}")
    if len(entries) > 40:
        print(f"    ... and {len(entries) - 40} more")
    print(f"\n  {before} file(s) under {DATA_ROOT.name} now, and the same number "
          f"must be there afterwards.")
    print(f"\n  The old index {INDEX_ROOT / OLD_INDEX} would be deleted and")
    print(f"  {INDEX_ROOT / f'{tenant}.sqlite3'} rebuilt from the files.")
    print("\n  Nothing has changed. Run again with --apply to do it.\n")
    return 0


def apply(tenant: str) -> int:
    target, entries, before = plan(tenant)

    section("Checks before touching anything")
    if app_is_running():
        print(f"  REFUSING: something is listening on port {APP_PORT}, so the app is")
        print("  probably running and may hold a file open. Windows will not move a")
        print("  file in that state, and a half-moved data folder is the worst")
        print("  outcome here. Stop it first:")
        print("    powershell -ExecutionPolicy Bypass -File "
              "scripts\\service\\control_windows.ps1 -Action stop\n")
        return 1
    print("  PASS  nothing is listening on the app's port")

    existing = count_files(target)
    if existing:
        print(f"  REFUSING: {target} already holds {existing} file(s). This has")
        print("  probably run already. Look at what is in there before doing anything")
        print("  else; --undo will put back whatever this script moved.\n")
        return 1
    print(f"  PASS  {target.name}/ is empty or absent")

    if not entries:
        print("  Nothing to move. Leaving everything alone.\n")
        return 0

    # The tenant has to exist in the control plane, or nothing can sign in as it.
    ensure_control_dir()
    connection = tenancy.connect()
    try:
        if tenancy.get_tenant(tenant, connection=connection) is None:
            tenancy.create_tenant("Default", tenant_id=tenant, connection=connection)
            print(f"  PASS  created tenant {tenant} in the control plane")
        else:
            print(f"  PASS  tenant {tenant} already exists in the control plane")
    finally:
        connection.close()

    # ---- move ------------------------------------------------------------
    section("Moving")
    target.mkdir(parents=True, exist_ok=True)
    moved: list[dict] = []
    for entry in entries:
        destination = target / entry.name
        if destination.exists():
            print(f"  STOPPING: {destination} already exists. {len(moved)} entr(ies)")
            print("  have been moved. Run --undo to put them back.")
            write_manifest(tenant, moved)
            return 1
        shutil.move(str(entry), str(destination))
        moved.append({"from": str(entry), "to": str(destination)})
        print(f"  moved  {entry.name}")

    write_manifest(tenant, moved)
    print(f"\n  Undo manifest: {CONTROL_DIR / MANIFEST}")

    # ---- count -----------------------------------------------------------
    section("Counting")
    after = count_files(DATA_ROOT)
    print(f"  before: {before} file(s)")
    print(f"  after:  {after} file(s)")
    if before != after:
        print("\n  THE COUNTS DO NOT MATCH. Something was lost or gained. Run --undo")
        print("  and do not restart the app until this is understood.\n")
        return 1
    print("  PASS  every file is accounted for")

    # ---- index -----------------------------------------------------------
    section("Rebuilding the index")
    old = INDEX_ROOT / OLD_INDEX
    if old.exists():
        old.unlink()
        print(f"  deleted the old single index {old.name}")
    with context.use_tenant(tenant):
        result = search.rebuild()
    print(f"  rebuilt {INDEX_ROOT / f'{tenant}.sqlite3'}")
    print(f"  {result['files_seen']} searchable file(s) seen, "
          f"{result['indexed']} indexed, {len(result['skipped'])} skipped")

    if result["files_seen"] and not result["indexed"]:
        print("\n  EVERY SEARCHABLE FILE FAILED TO INDEX. The files moved and are")
        print("  safe, but the index is empty, so nothing can be found by content.")
        print("  This is an environment fault rather than a data one. Fix it and run:")
        print(f"    py scripts\\check_search.py\n")
        return 1
    if result["skipped"]:
        print(f"  skipped: {', '.join(result['skipped'][:8])}"
              f"{' ...' if len(result['skipped']) > 8 else ''}")
        print("  A skipped file has no extractable text. It is still in the folder")
        print("  and still readable by name; it just cannot be found by content.")

    section("Done")
    print(f"  {after} file(s) now belong to tenant {tenant}.")
    print("  Start the app and confirm it can still see them:")
    print("    powershell -ExecutionPolicy Bypass -File "
          "scripts\\service\\control_windows.ps1 -Action start")
    print("    py scripts\\check_tools.py")
    print("    py scripts\\check_search.py\n")
    return 0


def write_manifest(tenant: str, moved: list[dict]) -> None:
    ensure_control_dir()
    (CONTROL_DIR / MANIFEST).write_text(
        json.dumps(
            {
                "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "tenant": tenant,
                "data_root": str(DATA_ROOT),
                "moved": moved,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def undo() -> int:
    path = CONTROL_DIR / MANIFEST
    section("Undo")
    if not path.exists():
        print(f"  No manifest at {path}, so there is nothing recorded to undo.\n")
        return 1
    if app_is_running():
        print(f"  REFUSING: something is listening on port {APP_PORT}. Stop the app "
              "first.\n")
        return 1

    record = json.loads(path.read_text(encoding="utf-8"))
    moved = record.get("moved", [])
    print(f"  Manifest written {record.get('written_at')}, {len(moved)} entr(ies).")

    put_back = 0
    for entry in reversed(moved):
        source, destination = Path(entry["to"]), Path(entry["from"])
        if not source.exists():
            print(f"  skipping {source.name}: not where the manifest says it is")
            continue
        if destination.exists():
            print(f"  skipping {source.name}: something is already at the old place")
            continue
        shutil.move(str(source), str(destination))
        put_back += 1
    # Leave no empty tenant folder behind, so a later run does not have to
    # reason about whether an empty folder means migrated or not migrated.
    target = Path(record["data_root"]) / record["tenant"]
    if target.is_dir() and not any(target.iterdir()):
        target.rmdir()
        print(f"  removed the empty {target.name}/ it had been moved into")

    print(f"\n  Put back {put_back} of {len(moved)}.")
    print("  The index is derived, so delete index\\*.sqlite3 and let it rebuild.\n")
    return 0 if put_back == len(moved) else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Move existing data into a tenant folder.")
    parser.add_argument("--apply", action="store_true", help="actually move the files")
    parser.add_argument("--undo", action="store_true", help="reverse a previous --apply")
    parser.add_argument("--tenant", default=BOOTSTRAP_TENANT,
                        help=f"tenant to migrate into (default: {BOOTSTRAP_TENANT})")
    args = parser.parse_args(argv)

    print("\nsyslab-server / migrate to tenant folders")
    if args.undo:
        return undo()
    tenant = context.validate_tenant_id(args.tenant)
    return apply(tenant) if args.apply else show(tenant)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        import traceback
        print(f"\n  The migration crashed: {type(exc).__name__}: {exc}")
        print("  Nothing further was attempted. If files had already moved, the")
        print(f"  manifest at {CONTROL_DIR / MANIFEST} says which; --undo puts them back.\n")
        traceback.print_exc()
        raise SystemExit(2)
