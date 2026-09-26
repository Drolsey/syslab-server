"""The inference plane cannot reach tenant storage, asserted on the source.

Section 2 of the architecture plan bounds the damage from a leaked service
token to "someone used your GPU" rather than "someone read every customer's
documents". That claim is only true while app/gateway.py stays incapable of
resolving a tenant or opening tenant storage, and the cheapest honest way to
keep it true is to read the file and fail if it ever learns how.

Written the way scripts/check_isolation.py demands its own checks be written:
break the property on purpose and confirm this says so. Add a line calling
config.data_dir() to app/gateway.py and this must fail.

    python scripts/check_gateway_isolation.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

GATEWAY = Path(__file__).resolve().parent.parent / "app" / "gateway.py"
LINE = "-" * 66

# Each is a way the inference plane could reach a customer's data. The
# import checks matter most: without app.context there is no tenant to set,
# and without a tenant every storage entry point in app/config.py raises.
FORBIDDEN = [
    (r"^\s*from\s+app\s+import\s+.*\bcontext\b", "imports app.context"),
    (r"^\s*from\s+app\s+import\s+.*\btenancy\b", "imports app.tenancy"),
    (r"^\s*from\s+app\s+import\s+.*\bsearch\b", "imports app.search"),
    (r"^\s*from\s+app\s+import\s+.*\btools\b", "imports app.tools"),
    (r"^\s*from\s+app\s+import\s+.*\bdb\b", "imports app.db"),
    # Step 2 added three more modules that reach tenant storage. This list is
    # only as good as its completeness: a module that touches derived/ and is
    # not named here is a hole that looks exactly like a pass.
    (r"^\s*from\s+app\s+import\s+.*\bingest\b", "imports app.ingest"),
    (r"^\s*from\s+app\s+import\s+.*\bproducers\b", "imports app.producers"),
    (r"^\s*from\s+app\s+import\s+.*\bintake\b", "imports app.intake"),
    # Step 4.2 added the source seam, and it is the most direct reach of the
    # lot: sources.active().list() enumerates a tenant's material and
    # .fetch() hands back a path into it, neither of which needs any other
    # storage import to be useful. Named the moment it existed -- this list
    # has twice been found stale AFTER the fact, and both times it was a
    # module Step 2 added.
    (r"^\s*from\s+app\s+import\s+.*\bsources\b", "imports app.sources"),
    (r"^\s*from\s+app\.sources\s+import", "imports from app.sources"),
    # Step 4.4 added the passage index. It opens the tenant's index file and
    # hands back QUOTED TEXT out of their documents, which is a more direct
    # leak than the document index has ever been: a search result is a
    # filename, a passage is a paragraph of the contract. Named the moment it
    # existed. app/retrieve.py is deliberately NOT here -- it holds a dataclass,
    # a protocol and a dict, and reaches no storage at all.
    (r"^\s*from\s+app\s+import\s+.*\bpassages\b", "imports app.passages"),
    (r"^\s*from\s+app\.passages\s+import", "imports from app.passages"),
    # Step 4.0 added the retrieval plane, which is this one's opposite: it
    # cannot do anything WITHOUT a tenant, so importing it would hand the
    # inference plane a ready-made way to get one. Named here the moment it
    # existed, rather than after a third gate is caught trusting a stale list.
    (r"^\s*from\s+app\s+import\s+.*\bplane\b", "imports app.plane"),
    (r"^\s*from\s+app\.plane\s+import", "imports from app.plane"),
    (r"\bresolve_alias\s*\(", "calls resolve_alias()"),
    (r"^\s*from\s+app\.ingest\s+import", "imports from app.ingest"),
    (r"^\s*from\s+app\.intake\s+import", "imports from app.intake"),
    (r"^\s*from\s+app\.context\s+import", "imports from app.context"),
    (r"^\s*from\s+app\.tenancy\s+import", "imports from app.tenancy"),
    (r"^\s*from\s+app\.db\s+import", "imports from app.db"),
    (r"\bdata_dir\s*\(", "calls data_dir()"),
    (r"\bindex_path\s*\(", "calls index_path()"),
    (r"\bderived_dir\s*\(", "calls derived_dir()"),
    (r"\bmanifest_path\s*\(", "calls manifest_path()"),
    (r"\bensure_derived_dir\s*\(", "calls ensure_derived_dir()"),
    (r"\bensure_data_dir\s*\(", "calls ensure_data_dir()"),
    (r"\bresolve_in_data_dir\s*\(", "calls resolve_in_data_dir()"),
    (r"\bcurrent_tenant\s*\(", "calls current_tenant()"),
    (r"\bset_tenant\s*\(", "calls set_tenant()"),
    (r"\buse_tenant\s*\(", "calls use_tenant()"),
    (r"\bdb\.settings\s*\(", "calls db.settings()"),
]


def example(description: str) -> str:
    """A line of code that this description says should be caught.

    DERIVED from the description rather than written out beside it, so there is
    nothing extra to keep in sync. The description already states what the
    pattern is for; this turns that sentence back into the code it describes.
    """
    if description.startswith("imports from app."):
        return f"from app.{description.removeprefix('imports from app.')} import something"
    if description.startswith("imports app."):
        return f"from app import {description.removeprefix('imports app.')}"
    if description.startswith("calls "):
        return f"    result = {description.removeprefix('calls ')}"
    return ""


def patterns_are_alive() -> list[str]:
    """Check every pattern still matches the thing it claims to forbid.

    WRITTEN BECAUSE ONE OF THEM DID NOT. Adding the app.passages rows at Step
    4.4 put a literal backspace character into the source where the regex was
    supposed to say a word boundary -- an escape that got eaten between an
    editor and the file. The pattern compiled, ran against every line of
    app/gateway.py, matched nothing, and printed PASS. It was indistinguishable
    from a clean bill of health, and it was found only by breaking gateway.py
    on purpose and noticing that nothing complained.

    That is this gate's own lesson turned on itself. The file already says it
    has twice been found STALE -- missing a module that existed. This is worse
    than stale: a row that is present, looks right, and is inert. A check
    nothing has ever seen fail is a claim rather than a check.
    """
    return [
        description for pattern, description in FORBIDDEN
        if not (example(description)
                and re.search(pattern, example(description), flags=re.MULTILINE))
    ]


def main() -> int:
    print("\nsyslab-server / inference plane isolation check")
    print(f"  Reading: {GATEWAY}\n")

    if not GATEWAY.is_file():
        print(f"  FAIL  app/gateway.py does not exist at {GATEWAY}")
        return 1

    source = GATEWAY.read_text(encoding="utf-8")
    # Comments and docstrings legitimately NAME these things while explaining
    # why they are absent, so judge code only: strip whole-line comments, and
    # the module docstring, before matching.
    without_docstring = re.sub(r'^""".*?"""', "", source, count=1, flags=re.DOTALL)
    code = "\n".join(
        line for line in without_docstring.splitlines() if not line.strip().startswith("#")
    )

    print("Every pattern below catches what it says it does")
    print(LINE)
    inert = patterns_are_alive()
    if inert:
        for description in inert:
            print(f"  FAIL  the check for {description!r} matches nothing")
        print(f"\n  {len(inert)} of {len(FORBIDDEN)} checks are inert. Every PASS")
        print("  printed below them would be meaningless, so nothing else runs.")
        print()
        return 1
    print(f"  PASS  all {len(FORBIDDEN)} patterns match the code they describe")

    print("\nForbidden reaches into tenant storage")
    print(LINE)
    failures = []
    for pattern, description in FORBIDDEN:
        hit = re.search(pattern, code, flags=re.MULTILINE)
        if hit:
            failures.append(description)
            print(f"  FAIL  app/gateway.py {description}")
        else:
            print(f"  PASS  never {description}")

    print(f"\nGate\n{LINE}")
    if failures:
        print(f"  {len(failures)} boundary violation(s). The inference plane is supposed to")
        print("  be unable to reach any customer's documents: a token that leaks from")
        print("  the website should cost you GPU time, not a customer's files. If this")
        print("  reach is genuinely needed, it belongs on the retrieval plane, which")
        print("  requires a tenant, not here.\n")
        return 1

    print("  PASS  The inference plane cannot resolve a tenant or open tenant storage.")
    print("  A leaked GATEWAY_TOKEN costs GPU time, not documents.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
