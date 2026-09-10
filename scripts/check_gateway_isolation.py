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

    print("Forbidden reaches into tenant storage")
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
