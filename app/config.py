"""Central configuration. Everything environment-driven, every path a pathlib.Path.

Keeping paths and settings here is what makes the Windows -> Linux move later a
copy of the folder rather than a hunt through the code for hardcoded drive letters.
"""

from __future__ import annotations

import hmac
import os
import re
from pathlib import Path, PurePosixPath

from app.context import current_tenant

try:
    from dotenv import load_dotenv
except ImportError:  # dotenv is not installed until Phase 04 requirements land
    def load_dotenv(*_args, **_kwargs):
        return False

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _env(key: str, default: str) -> str:
    value = os.getenv(key, default)
    return value.strip() if isinstance(value, str) else default


# --- model ---
# The OpenAI-compatible endpoint app/llm.py actually talks to. vLLM in
# production (Step 3 of the architecture plan), Ollama's own /v1 shim on a
# dev laptop -- either way this is the only backend the running app uses.
LLM_BASE_URL = _env("LLM_BASE_URL", "http://127.0.0.1:8000/v1")
LLM_MODEL = _env("LLM_MODEL", "Qwen/Qwen3-14B-AWQ")
LLM_TIMEOUT = int(_env("LLM_TIMEOUT", "300"))
# Qwen3 can "think" before answering. Off by default: it roughly triples the
# wait for a marginal gain on these tools, and keeps the transcript readable.
LLM_THINK = _env("LLM_THINK", "false").lower() in {"1", "true", "yes", "on"}
# How many tool calls the model may make before we stop it, per question.
MAX_TOOL_STEPS = int(_env("MAX_TOOL_STEPS", "10"))

# --- Ollama, dev-only ---
# Not what the running app talks to (see LLM_BASE_URL above). Kept so
# scripts/check_services.py and scripts/bench_models.py can still probe a
# dev laptop's Ollama install directly, per the dev-laptop profile in
# Section 13 of the architecture plan.
OLLAMA_HOST = _env("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen3:8b")

def _path(key: str, default: Path) -> Path:
    """A path from the environment, relative ones anchored to the project.

    DATA_DIR=./data used to be resolved against the current working directory,
    so the same setting meant a different folder depending on where a command
    was run from: the scheduled task cds to the project first and saw the real
    data folder, a script run from anywhere else quietly created an empty one
    beside itself. Anchoring to the project makes the setting mean one thing.
    """
    raw = _env(key, "")
    if not raw:
        return default.expanduser().resolve()
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = PROJECT_ROOT / candidate
    return candidate.resolve()


# --- files ---
# The env var names stay DATA_DIR and INDEX_DIR because they are what is in
# every .env already, and what they point at has not changed: the root. What
# changed is that nothing reads a single folder any more. Each tenant gets a
# folder under DATA_ROOT and an index file under INDEX_ROOT, and the constants
# were RENAMED rather than kept as aliases so that any code still expecting one
# global folder fails at import with a NameError instead of quietly serving the
# wrong customer.
DATA_ROOT = _path("DATA_DIR", PROJECT_ROOT / "data")

# The tenant this install's existing data belongs to, and the one the gate
# scripts work as. app/main.py uses it as a placeholder until sub-step 1.5
# resolves a real tenant from the request token.
BOOTSTRAP_TENANT = _env("BOOTSTRAP_TENANT", "default")
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_MB", "50")) * 1024 * 1024

# --- server ---
# 127.0.0.1 until Phase 06 puts a token check on the door. This app writes
# files; it should not be listening on the network without one.
APP_HOST = _env("APP_HOST", "127.0.0.1")
APP_PORT = int(_env("APP_PORT", "8000"))

# Is this reachable from the public internet rather than only a tailnet?
# Two things are safe on a tailnet and not safe once anyone can reach them:
# the interactive API docs, which publish the whole surface including the
# routes that write files, and the admin page at "/". Both are hidden when
# this is on. Off by default because the wrong default here is one-directional:
# a server that is public while this says otherwise is the bad outcome, and it
# is the one a forgotten setting produces.
PUBLIC_MODE = _env("PUBLIC_MODE", "false").lower() in {"1", "true", "yes", "on"}

# Is there a reverse proxy in front that sets CF-Connecting-IP itself?
# Separate from PUBLIC_MODE on purpose: PUBLIC_MODE says "strangers can reach
# this", while this says "and they can only reach it THROUGH the tunnel". Only
# the second one makes the header trustworthy, and getting it wrong is
# dangerous in both directions -- trusting the header with the port also open
# lets anyone evade the login throttle by varying one string, and not trusting
# it behind the tunnel collapses every caller into one bucket so eight wrong
# guesses from anybody lock out everybody. Off by default: that failure is the
# loud one.
TRUST_CLIENT_IP_HEADER = _env("TRUST_CLIENT_IP_HEADER", "false").lower() in {
    "1", "true", "yes", "on"
}

# --- the search index ---
# Deliberately outside DATA_DIR: it is a derived cache, not user data, and
# deleting it must be obviously safe.
INDEX_ROOT = _path("INDEX_DIR", PROJECT_ROOT / "index")

# --- derived artifacts (Step 2, the ingestion contract) ---
# What producers make out of a source file, and a manifest row per (file,
# producer) saying whether it worked. Outside DATA_DIR for the same reason the
# index is: nothing here is user data, and every byte of it can be thrown away
# and rebuilt from data/. A producer that ever makes something which CANNOT be
# regenerated is not a producer, and its output does not belong under here.
#
# Separate from INDEX_DIR rather than folded into it because the index is one
# consumer of this pipeline, not its owner: the FTS5 file stays deletable on
# its own, and page images do not become something you lose by rebuilding a
# search index.
DERIVED_ROOT = _path("DERIVED_DIR", PROJECT_ROOT / "derived")

# --- control plane ---
# Who exists, and which token belongs to whom. Deliberately outside DATA_DIR,
# which is customer content, and outside INDEX_DIR, which is a derived cache
# this project is happy to delete. This is neither: losing it loses every
# tenant's identity, so it is the one directory here worth backing up.
CONTROL_DIR = _path("CONTROL_DIR", PROJECT_ROOT / "control")
CONTROL_PATH = CONTROL_DIR / "control.sqlite3"

# --- the job lane, for work too slow to answer a request with ---
# One worker by default: the GPU serialises this work anyway, and extra
# workers would only contend for it.
JOB_WORKERS = int(_env("JOB_WORKERS", "1"))
JOB_MAX_QUEUED = int(_env("JOB_MAX_QUEUED", "20"))
# How long a finished job stays readable before it is forgotten.
JOB_RETENTION_SECONDS = int(_env("JOB_RETENTION_SECONDS", "1800"))

# --- customer database (optional, read only) ---
DB_HOST = _env("DB_HOST", "")
DB_PORT = int(_env("DB_PORT", "5432"))
DB_DATABASE = _env("DB_DATABASE", "")
DB_USER = _env("DB_USER", "")
DB_PASSWORD = _env("DB_PASSWORD", "")
DB_SSLMODE = _env("DB_SSLMODE", "require")
DB_CONNECT_TIMEOUT = int(_env("DB_CONNECT_TIMEOUT", "10"))
# The server cuts a query off at this point rather than letting the assistant
# sit on someone's production database.
DB_STATEMENT_TIMEOUT_MS = int(_env("DB_STATEMENT_TIMEOUT_MS", "15000"))
DB_MAX_ROWS = int(_env("DB_MAX_ROWS", "200"))

# The cap above protects the model's context window: 200 rows is roughly what
# it can read without losing track. A file has no such limit -- nobody is
# reading it a token at a time -- so an export gets its own, far larger ceiling.
# Conflating the two is why "save the query as a spreadsheet" used to produce a
# 200-row spreadsheet from a 49,795-row table.
DB_EXPORT_MAX_ROWS = int(_env("DB_EXPORT_MAX_ROWS", "100000"))

# An export is not an interactive query. 15 seconds is right for something a
# person is waiting on in a chat window; it is far too short for writing tens
# of thousands of rows to disk, and cutting that off produces a timeout the
# model cannot do anything useful about.
DB_EXPORT_TIMEOUT_MS = int(_env("DB_EXPORT_TIMEOUT_MS", "180000"))

# --- auth (enforced from Phase 06) ---
APP_TOKEN = _env("APP_TOKEN", "")

# --- the inference plane (Step 3.2) ---
# Deliberately separate from APP_TOKEN and the tenant token system: the
# inference plane never learns what a conversation is and never resolves a
# tenant (Section 2 of the architecture plan), so it has no business sharing
# a credential with something that does. A comma-separated list because more
# than one caller (the website, the operator testing by hand) needs its own
# revocable value; the tenant-aware "kind" column on real tokens is Step 4.
GATEWAY_TOKENS = {t.strip() for t in _env("GATEWAY_TOKENS", "").split(",") if t.strip()}
# Requests allowed per token per rolling minute. Cheap and in-memory on
# purpose: this is a courtesy limit against a misbehaving caller, not the
# real capacity control -- that is vLLM's own queue and continuous batching.
GATEWAY_RATE_LIMIT_PER_MINUTE = int(_env("GATEWAY_RATE_LIMIT_PER_MINUTE", "60"))
# What /v1/models advertises, and the only names a request may ask for. The
# website pins an alias, never a raw model name (Section 13): changing the
# served model is this one line, not a change on the website's side. The
# full TOML profile file is Step 3.4; one alias is enough until there is a
# second model (embeddings) to alias alongside it.
MODEL_ALIASES = {"syslab-default": LLM_MODEL}

# Addresses that mean "this machine only", and tokens that are not tokens.
# They live here rather than in main.py so the check scripts can read them
# without importing the whole web framework.
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
WEAK_TOKENS = {"", "change-me", "changeme", "password", "token", "secret"}
MIN_TOKEN_LENGTH = 16


# --- the retrieval plane (Step 4.0) ---
def _retrieval_tokens(raw: str) -> dict[str, str]:
    """Parse `system:token,system:token` into {token: external system}.

    THE SYSTEM COMES FROM THE TOKEN, NEVER FROM A HEADER, and that is the whole
    reason this is a mapping rather than the flat set GATEWAY_TOKENS is. A
    foreign id is only meaningful inside one system's id space: if a caller
    could name its own system, one system's token would resolve ids in
    another's namespace, and the separation the tenant_alias primary key exists
    to provide would be worth nothing.

    Malformed entries are dropped rather than raised on, because this runs at
    import time and a config typo that stops the process leaves an operator
    with a server that will not start and no plane to read the error from. A
    dropped token fails closed -- it authenticates nobody -- and
    RETRIEVAL_TOKEN_PROBLEMS carries the count so a startup check can say so.
    """
    tokens: dict[str, str] = {}
    problems: list[str] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        system, separator, token = entry.partition(":")
        system, token = system.strip(), token.strip()
        if not separator or not system or not token:
            problems.append(f"{entry[:12]!r} is not system:token")
            continue
        if not _VALID_SYSTEM.match(system):
            problems.append(f"{system!r} is not a usable system name")
            continue
        if len(token) < MIN_TOKEN_LENGTH:
            problems.append(f"the token for {system!r} is shorter than {MIN_TOKEN_LENGTH}")
            continue
        tokens[token] = system
    _retrieval_tokens.problems = problems  # type: ignore[attr-defined]
    return tokens


# Kept in step with tenancy.VALID_EXTERNAL_SYSTEM, which cannot be imported
# here: tenancy imports config, so the arrow only points one way. The shape is
# a short lower-case label and a test asserts the two agree.
_VALID_SYSTEM = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

RETRIEVAL_TOKENS = _retrieval_tokens(_env("RETRIEVAL_TOKENS", ""))
RETRIEVAL_TOKEN_PROBLEMS: list[str] = getattr(_retrieval_tokens, "problems", [])


def token_is_configured() -> bool:
    token = (APP_TOKEN or "").strip()
    return token.lower() not in WEAK_TOKENS and len(token) >= MIN_TOKEN_LENGTH


def tokens_equal(candidate: object, known: str) -> bool:
    """Constant-time token comparison that its own input cannot crash.

    hmac.compare_digest RAISES TypeError on a str holding any non-ASCII
    character, so `Authorization: Bearer unicode-yes-really` turned what should
    be a 401 into a 500 on every plane that compared a token -- an
    unauthenticated caller reaching a traceback by sending one accented letter.

    Comparing the UTF-8 bytes keeps the constant-time property, which is the
    only reason compare_digest is here at all, and makes a malformed token
    simply a wrong one. Every plane compares through this and none of them
    calls compare_digest on a str directly.
    """
    if not isinstance(candidate, str) or not isinstance(known, str) or not known:
        return False
    return hmac.compare_digest(candidate.encode("utf-8"), known.encode("utf-8"))


class UnsafePathError(ValueError):
    """Raised when a requested path escapes DATA_DIR."""


def data_dir() -> Path:
    """The current tenant's document folder.

    Raises NoTenantError if nothing has said whose request this is. That is the
    point: there is no folder to fall back to, because falling back is how one
    customer's request reads another customer's files.
    """
    return DATA_ROOT / current_tenant()


def index_path() -> Path:
    """The current tenant's search index.

    One file per tenant rather than one file with an owner column. The failure
    modes are not comparable: a forgotten WHERE returns another customer's
    document text silently, while a wrong path returns nothing or raises.
    """
    return INDEX_ROOT / f"{current_tenant()}.sqlite3"


def ensure_data_dir() -> Path:
    folder = data_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_index_dir() -> Path:
    # The root, not a per-tenant folder: the index files sit directly in it.
    INDEX_ROOT.mkdir(parents=True, exist_ok=True)
    return INDEX_ROOT


def derived_dir() -> Path:
    """The current tenant's derived-artifact folder.

    A folder each rather than a shared folder with an owner column, for the
    same reason index_path() is a file each: deleting one tenant's derived
    artifacts must be `rm -rf` of one path, and a bug must land somewhere
    empty rather than somewhere belonging to somebody else.
    """
    return DERIVED_ROOT / current_tenant()


def manifest_path() -> Path:
    """The current tenant's ingestion manifest.

    Inside derived_dir(), not beside it, so that deleting the folder deletes
    the record of what was in it. A manifest that outlived its artifacts would
    claim a document is ready and point at files that are gone.
    """
    return derived_dir() / "manifest.sqlite3"


def ensure_derived_dir() -> Path:
    folder = derived_dir()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def ensure_control_dir() -> Path:
    CONTROL_DIR.mkdir(parents=True, exist_ok=True)
    return CONTROL_DIR


def resolve_in_data_dir(name: str) -> Path:
    """Turn a model-supplied filename into a real path inside the tenant's folder.

    The model chooses these strings, so treat them as untrusted input. Anything
    that escapes DATA_DIR is rejected rather than clamped. Backslashes are
    normalised first so a Windows-style string is judged the same way on Linux,
    where a backslash would otherwise be an ordinary filename character.
    """
    root = ensure_data_dir()
    cleaned = str(name).strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise UnsafePathError("No filename given.")

    parts = PurePosixPath(cleaned).parts
    if any(part == ".." for part in parts):
        raise UnsafePathError(f"Refusing path outside the data folder: {name}")
    # C:/..., \\server\share, /etc/...
    if len(cleaned) > 1 and cleaned[1] == ":":
        raise UnsafePathError(f"Refusing absolute path: {name}")

    candidate = (root / cleaned).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise UnsafePathError(f"Cannot resolve path: {name}") from exc
    # Judged against THIS TENANT's folder, so the same check that used to stop
    # an escape to C:\Windows now also stops a walk sideways into another
    # tenant's folder, which is a subdirectory of the same root.
    if resolved != root and root not in resolved.parents:
        raise UnsafePathError(f"Refusing path outside the data folder: {name}")
    return resolved


def code_fingerprint() -> str:
    """A cheap stamp of the code currently on disk.

    A running service is a snapshot of the code as it was when it started.
    Editing a file changes nothing until the service restarts, and that gap is
    genuinely confusing: the checks pass, the behaviour is old. The app reports
    this at startup and scripts/check_services.py compares it against disk.
    """
    here = Path(__file__).resolve().parent
    newest = 0.0
    count = 0
    for pattern in ("*.py", "web/*.html"):
        for path in sorted(here.glob(pattern)):
            count += 1
            newest = max(newest, path.stat().st_mtime)
    return f"{count}f-{int(newest)}"
