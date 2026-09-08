"""Step 3.5: the /v1 contract cannot break under the website's feet.

The website deploys straight to production on a push to `main`. If a change
here narrows what `/v1` accepts, nobody finds out from this repository's tests
-- they find out from a chat that stopped answering, on somebody else's
deployment, with no obvious connection to a commit made over here. This is the
Python analogue of the compatibility checker the website already runs, and it
exists so that particular failure is caught on this side of the wire.

    python scripts/check_api_compat.py            # compare against the frozen contract
    python scripts/check_api_compat.py --freeze   # re-freeze, deliberately

Three outcomes, not two:

    exit 0   compatible -- identical, or additive only (new routes, new
             OPTIONAL fields). Additions are printed, because a frozen file
             that has quietly stopped describing the server is not a contract.
    exit 1   BREAKING -- something a working caller depends on is gone or
             narrower. Do not deploy this against the live website.
    exit 2   cannot judge -- no frozen file, or the app will not import. An
             unanswerable question must not be reported as a pass.

**Only `/v1` is frozen, on purpose.** The local plane (`/api/...`) is this
install's own admin surface, changes freely, and has exactly one client that
ships in the same commit as the server. Freezing it would produce a stream of
failures that mean nothing, and a gate people learn to ignore is worse than no
gate. `/v1` is the one surface with a caller that deploys separately.

**What this can and cannot see.** It compares the request contract: which
routes exist, which methods, which fields are required, their types, and
whether unknown fields are still accepted. It cannot compare response bodies,
because the gateway returns vLLM's JSON unmodified and FastAPI therefore has no
response model to declare -- that shape is OpenAI's, not ours, and it is
checked against the running server by scripts/check_gateway.py instead. Two
gates, two questions: this one asks "did we narrow the door", that one asks
"does what comes back still look right".

Written the way this repo demands its checks be written: broken on purpose
first. Adding a required field to ChatCompletionRequest, deleting a route, or
setting `extra="forbid"` on the request model must each make this fail, and
each did before this file was committed.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deps import require  # noqa: E402

require("fastapi", "pydantic")

FROZEN = ROOT / "docs" / "api" / "gateway-v1.released.json"
PREFIX = "/v1"
LINE = "-" * 72

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


# --------------------------------------------------------------------------
# reading the live schema
# --------------------------------------------------------------------------

def current_contract() -> dict:
    """FastAPI's own schema, narrowed to /v1 and to what /v1 refers to.

    Imported rather than fetched over HTTP so this runs in CI with no server,
    no GPU and no model. It is the same object the server would serve; taking
    it from the source removes the possibility of checking a stale process.
    """
    from app.main import app  # imported late: require() gives the better error

    schema = app.openapi()
    paths = {p: v for p, v in schema.get("paths", {}).items() if p.startswith(PREFIX)}

    components = schema.get("components", {}).get("schemas", {})
    kept: dict = {}
    _keep_referenced(paths, components, kept)

    contract = {
        "openapi": schema.get("openapi"),
        "info": {"title": schema.get("info", {}).get("title"), "contract": "gateway-v1"},
        "paths": paths,
    }
    if kept:
        contract["components"] = {"schemas": kept}
    return contract


def _keep_referenced(node, components: dict, kept: dict) -> None:
    """Walk anything, and pull across every component schema it names.

    Transitive on purpose: a request model that references another model
    would otherwise freeze as a dangling $ref, and a dangling $ref compares
    equal to every other dangling $ref.
    """
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            name = ref.rsplit("/", 1)[-1]
            if name not in kept and name in components:
                kept[name] = components[name]
                _keep_referenced(components[name], components, kept)
        for value in node.values():
            _keep_referenced(value, components, kept)
    elif isinstance(node, list):
        for value in node:
            _keep_referenced(value, components, kept)


def _resolve(schema, doc: dict):
    """Follow a $ref into this document's own components. One hop is enough:
    _keep_referenced flattened everything into the same file."""
    seen = set()
    while isinstance(schema, dict) and isinstance(schema.get("$ref"), str):
        ref = schema["$ref"]
        if ref in seen:
            return {}
        seen.add(ref)
        name = ref.rsplit("/", 1)[-1]
        schema = doc.get("components", {}).get("schemas", {}).get(name, {})
    return schema if isinstance(schema, dict) else {}


def _request_schema(operation: dict, doc: dict) -> dict:
    body = operation.get("requestBody") or {}
    content = body.get("content") or {}
    media = content.get("application/json") or {}
    return _resolve(media.get("schema") or {}, doc)


def _type_of(prop: dict) -> str:
    """A comparable one-line type. anyOf is how pydantic writes Optional, and
    an Optional widening to another Optional is not what this gate is for, so
    the members are sorted and joined rather than compared positionally."""
    if not isinstance(prop, dict):
        return "?"
    if "type" in prop:
        return str(prop["type"])
    for key in ("anyOf", "oneOf", "allOf"):
        if key in prop and isinstance(prop[key], list):
            return f"{key}[" + "|".join(sorted(_type_of(m) for m in prop[key])) + "]"
    if "$ref" in prop:
        return str(prop["$ref"])
    return "any"


# --------------------------------------------------------------------------
# the comparison
# --------------------------------------------------------------------------

def compare(frozen: dict, current: dict) -> tuple[list[str], list[str]]:
    """Returns (breaking, additive). Every rule here is asked from the caller's
    side: what does a client that works today rely on continuing to be true."""
    breaking: list[str] = []
    additive: list[str] = []

    frozen_paths = frozen.get("paths", {})
    current_paths = current.get("paths", {})

    for path in sorted(set(current_paths) - set(frozen_paths)):
        additive.append(f"new route {path} (not in the frozen contract)")

    for path, frozen_ops in sorted(frozen_paths.items()):
        current_ops = current_paths.get(path)
        if current_ops is None:
            breaking.append(f"{path} was removed. A caller pinned to it now gets 404.")
            continue

        for method in METHODS:
            frozen_op = frozen_ops.get(method)
            if not isinstance(frozen_op, dict):
                continue
            current_op = current_ops.get(method)
            if not isinstance(current_op, dict):
                breaking.append(f"{method.upper()} {path} was removed.")
                continue
            _compare_operation(path, method, frozen_op, current_op, frozen, current,
                               breaking, additive)

        for method in METHODS:
            if method in current_ops and method not in frozen_ops:
                additive.append(f"new method {method.upper()} {path}")

    return breaking, additive


def _compare_operation(path, method, frozen_op, current_op, frozen, current,
                       breaking, additive) -> None:
    where = f"{method.upper()} {path}"

    frozen_body = _request_schema(frozen_op, frozen)
    current_body = _request_schema(current_op, current)

    frozen_props = frozen_body.get("properties") or {}
    current_props = current_body.get("properties") or {}
    frozen_required = set(frozen_body.get("required") or [])
    current_required = set(current_body.get("required") or [])

    # A field that used to be optional and is now mandatory breaks every
    # caller that was not already sending it, which is the most common way an
    # API breaks without anyone feeling like they removed anything.
    for field in sorted(current_required - frozen_required):
        breaking.append(
            f"{where}: `{field}` is now required. Existing callers do not send it."
        )

    for field in sorted(frozen_props):
        if field not in current_props:
            level = breaking if field in frozen_required else additive
            message = f"{where}: `{field}` is gone from the request contract."
            level.append(message if field in frozen_required
                         else message + " It was optional, so this is a narrowing "
                                        "worth noticing rather than a break.")
            continue
        was, now = _type_of(frozen_props[field]), _type_of(current_props[field])
        if was != now:
            breaking.append(f"{where}: `{field}` changed type, {was} -> {now}.")

    for field in sorted(set(current_props) - set(frozen_props)):
        if field not in current_required:
            additive.append(f"{where}: new optional field `{field}`")

    # extra="allow" is load-bearing here. The gateway passes tools,
    # tool_choice, temperature and everything else straight through without
    # naming them, so an OpenAI client's ordinary request works untouched.
    # Turning that off rejects fields this schema never listed, which no
    # amount of reading the field list would predict.
    was_open = frozen_body.get("additionalProperties", False) is not False
    is_open = current_body.get("additionalProperties", False) is not False
    if was_open and not is_open:
        breaking.append(
            f"{where}: unknown fields are no longer accepted. The gateway passes "
            "OpenAI's fields through without naming them; forbidding extras "
            "rejects `tools`, `tool_choice` and everything else it never listed."
        )

    frozen_responses = set(frozen_op.get("responses") or {})
    current_responses = set(current_op.get("responses") or {})
    for code in sorted(frozen_responses - current_responses):
        breaking.append(f"{where}: response {code} is no longer declared.")


# --------------------------------------------------------------------------
# entry points
# --------------------------------------------------------------------------

def freeze() -> int:
    contract = current_contract()
    contract["x-syslab-frozen"] = {
        "frozen": date.today().isoformat(),
        "why": "Step 3.5. The website deploys separately; this is what it may rely on.",
        "checked-by": "scripts/check_api_compat.py",
    }
    FROZEN.parent.mkdir(parents=True, exist_ok=True)
    existed = FROZEN.exists()
    FROZEN.write_text(json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"\n  {'Re-froze' if existed else 'Froze'} {len(contract.get('paths', {}))} route(s) "
          f"to {FROZEN.relative_to(ROOT)}")
    print("\n  Re-freezing is a deliberate act and belongs in its own commit, next to")
    print("  the changelog entry that says what changed and why it is safe.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", action="store_true",
                        help="write the frozen contract from the current app")
    args = parser.parse_args()

    if args.freeze:
        return freeze()

    print("\nsyslab-server / gateway v1 contract")
    print(f"  Frozen:  {FROZEN}")

    if not FROZEN.is_file():
        print("\n  CANNOT JUDGE  There is no frozen contract to compare against.")
        print("  Create it once, deliberately:  python scripts/check_api_compat.py --freeze\n")
        return 2

    try:
        frozen = json.loads(FROZEN.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"\n  CANNOT JUDGE  The frozen contract is unreadable: {exc}\n")
        return 2

    try:
        current = current_contract()
    except Exception as exc:  # noqa: BLE001 -- any import error is the same answer here
        print(f"\n  CANNOT JUDGE  The app would not import: {exc!r}")
        print("  Fix that first. A contract check that cannot load the app proves nothing.\n")
        return 2

    print(f"  Frozen routes:  {len(frozen.get('paths', {}))}")
    print(f"  Current routes: {len(current.get('paths', {}))}\n")

    breaking, additive = compare(frozen, current)

    print(f"Breaking changes\n{LINE}")
    if breaking:
        for item in breaking:
            print(f"  FAIL  {item}")
    else:
        print("  PASS  Nothing a working caller depends on has been removed or narrowed.")

    print(f"\nAdditions since the freeze\n{LINE}")
    if additive:
        for item in additive:
            print(f"  ADDED {item}")
        print("\n  Additions do not break a caller and do not fail this gate. They do mean")
        print("  the frozen file has stopped describing the server. Re-freeze when the")
        print("  addition is intentional and shipped:  --freeze")
    else:
        print("  none. The frozen contract still describes the server exactly.")

    print(f"\nGate\n{LINE}")
    if breaking:
        print(f"  {len(breaking)} breaking change(s). The website deploys straight to")
        print("  production on a push, so this would be found by a customer rather than")
        print("  by a test. If the break is genuinely intended, it needs a new version in")
        print("  the path (/v2), not a re-freeze of v1: re-freezing records the break")
        print("  instead of preventing it.\n")
        return 1

    print("  PASS  The v1 contract still holds.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
