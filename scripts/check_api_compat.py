"""Steps 3.5 and 4.6: two contracts that cannot break under the website's feet.

The website deploys straight to production on a push to `main`. If a change
here narrows what it may send, nobody finds out from this repository's tests
-- they find out from a chat that stopped answering, on somebody else's
deployment, with no obvious connection to a commit made over here. This is the
Python analogue of the compatibility checker the website already runs, and it
exists so that particular failure is caught on this side of the wire.

    python scripts/check_api_compat.py               # compare every contract
    python scripts/check_api_compat.py --freeze      # re-freeze all, deliberately
    python scripts/check_api_compat.py --freeze retrieval   # or just one

Three outcomes, not two:

    exit 0   compatible -- identical, or additive only (new routes, new
             OPTIONAL fields). Additions are printed, because a frozen file
             that has quietly stopped describing the server is not a contract.
    exit 1   BREAKING -- something a working caller depends on is gone or
             narrower. Do not deploy this against the live website.
    exit 2   cannot judge -- no frozen file, or the app will not import. An
             unanswerable question must not be reported as a pass.

**TWO surfaces are frozen and the local plane still is not, on purpose.**
`/v1` is the inference plane (Step 3.5) and `/api/v1` is the retrieval plane
(Step 4.6); both have a caller that deploys separately, which is the only
reason a freeze earns its keep. `/api/...` without the `v1` is this install's
own admin surface, changes freely, and has exactly one client that ships in the
same commit as the server -- freezing it would produce a stream of failures
that mean nothing, and a gate people learn to ignore is worse than no gate.

**The two are compared independently and reported separately**, because a break
in one says nothing about the other and a single merged verdict would make the
retrieval plane's first narrowing read as a gateway regression.

**Neither freeze covers a response body, and for the two planes the reason is
different.** The gateway returns vLLM's JSON unmodified, so that shape is
OpenAI's rather than ours and `scripts/check_gateway.py` checks it against the
running server. `/api/v1/retrieve` returns a `dict`, so FastAPI emits an open
object for its 200 -- and declaring a response model to close it would fight
this gate's own rule that a field may be ADDED, because pydantic strips what a
model does not name. The retrieval response shape is therefore pinned by
`tests/test_plane_retrieve.py`, which asserts the EXACT key set of the body and
of each passage. That is said here rather than left to be discovered: the
load-bearing fields of section 6 -- `found_by`, `coverage`, `truncated`,
`what_this_means` -- are all in the response, so a reader who took this file to
cover them would be trusting a freeze that covered none of them.

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
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _deps import require  # noqa: E402

require("fastapi", "pydantic")

LINE = "-" * 72


@dataclass(frozen=True)
class Contract:
    """One frozen surface: what to narrow the schema to, and where it lives.

    A list rather than two copies of this file, and rather than one merged
    contract, for the reason in the header: the two planes break independently
    and a merged verdict would misattribute the first narrowing of either.
    """

    name: str
    prefix: str
    path: Path
    why: str


CONTRACTS = (
    Contract(
        "gateway", "/v1",
        ROOT / "docs" / "api" / "gateway-v1.released.json",
        "Step 3.5. The website deploys separately; this is what it may rely on.",
    ),
    Contract(
        "retrieval", "/api/v1",
        ROOT / "docs" / "api" / "retrieval-v1.released.json",
        "Step 4.6. The retrieval plane. Fields may be ADDED; nothing may narrow. "
        "`found_by`, `coverage` and the absent score are where later steps will "
        "push, and all three were chosen for that.",
    ),
)

# `/api/v1` does not start with `/v1`, so the two prefixes cannot capture each
# other's routes. Asserted rather than assumed, because the day somebody mounts
# the retrieval plane at `/v1/api` the overlap would show up as a contract that
# freezes twice and compares against itself.
assert not any(
    a.prefix != b.prefix and a.prefix.startswith(b.prefix)
    for a in CONTRACTS for b in CONTRACTS
), "one frozen prefix is inside another, so the two contracts would overlap"

METHODS = ("get", "put", "post", "delete", "patch", "head", "options", "trace")


# --------------------------------------------------------------------------
# reading the live schema
# --------------------------------------------------------------------------

def current_contract(contract: Contract) -> dict:
    """FastAPI's own schema, narrowed to one prefix and to what it refers to.

    Imported rather than fetched over HTTP so this runs in CI with no server,
    no GPU and no model. It is the same object the server would serve; taking
    it from the source removes the possibility of checking a stale process.
    """
    from app.main import app  # imported late: require() gives the better error

    schema = app.openapi()
    paths = {
        p: v for p, v in schema.get("paths", {}).items()
        if p.startswith(contract.prefix)
    }

    components = schema.get("components", {}).get("schemas", {})
    kept: dict = {}
    _keep_referenced(paths, components, kept)

    out = {
        "openapi": schema.get("openapi"),
        "info": {
            "title": schema.get("info", {}).get("title"),
            "contract": f"{contract.name}-v1",
        },
        "paths": paths,
    }
    if kept:
        out["components"] = {"schemas": kept}
    return out


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

def freeze(wanted: str) -> int:
    for contract in CONTRACTS:
        if wanted not in ("all", contract.name):
            continue
        out = current_contract(contract)
        out["x-syslab-frozen"] = {
            "frozen": date.today().isoformat(),
            "why": contract.why,
            "checked-by": "scripts/check_api_compat.py",
        }
        contract.path.parent.mkdir(parents=True, exist_ok=True)
        existed = contract.path.exists()
        contract.path.write_text(
            json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\n  {'Re-froze' if existed else 'Froze'} {len(out.get('paths', {}))} "
              f"route(s) of {contract.prefix} to {contract.path.relative_to(ROOT)}")
    print("\n  Re-freezing is a deliberate act and belongs in its own commit, next to")
    print("  the changelog entry that says what changed and why it is safe.\n")
    return 0


def judge(contract: Contract) -> int:
    """One contract, compared and reported. Returns its own exit code."""
    print(f"\nsyslab-server / {contract.name} contract  ({contract.prefix})")
    print(f"  Frozen:  {contract.path}")

    if not contract.path.is_file():
        print("\n  CANNOT JUDGE  There is no frozen contract to compare against.")
        print("  Create it once, deliberately:  python scripts/check_api_compat.py "
              f"--freeze {contract.name}\n")
        return 2

    try:
        frozen = json.loads(contract.path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"\n  CANNOT JUDGE  The frozen contract is unreadable: {exc}\n")
        return 2

    try:
        current = current_contract(contract)
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
        print(f"  addition is intentional and shipped:  --freeze {contract.name}")
    else:
        print("  none. The frozen contract still describes the server exactly.")

    if breaking:
        print(f"\n  {len(breaking)} breaking change(s) in {contract.prefix}. The website")
        print("  deploys straight to production on a push, so this would be found by a")
        print("  customer rather than by a test. If the break is genuinely intended it")
        print("  needs a new version in the path, not a re-freeze: re-freezing records")
        print("  the break instead of preventing it.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    names = [contract.name for contract in CONTRACTS]
    parser.add_argument(
        "--freeze", nargs="?", const="all", choices=["all", *names], default=None,
        help="write the frozen contract from the current app; names one or all")
    args = parser.parse_args()

    if args.freeze:
        return freeze(args.freeze)

    # Every contract is judged even when an earlier one fails, so one run says
    # everything that is wrong. Stopping at the first would hide a retrieval
    # break behind a gateway break and cost a second run to find it.
    codes = {contract.name: judge(contract) for contract in CONTRACTS}

    print(f"\nGate\n{LINE}")
    for name, code in codes.items():
        verdict = {0: "PASS ", 1: "FAIL ", 2: "UNSURE"}[code]
        print(f"  {verdict} {name}")
    print()
    # 1 beats 2: a known break is worse news than an unanswerable question, and
    # the exit code should report the worse one.
    if 1 in codes.values():
        return 1
    return 2 if 2 in codes.values() else 0


if __name__ == "__main__":
    sys.exit(main())
