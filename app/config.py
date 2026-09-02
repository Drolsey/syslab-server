"""Central configuration. Everything environment-driven, every path a pathlib.Path.

Keeping paths and settings here is what makes the Windows -> Linux move later a
copy of the folder rather than a hunt through the code for hardcoded drive letters.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

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
OLLAMA_MODEL = _env("OLLAMA_MODEL", "qwen3:8b")
OLLAMA_HOST = _env("OLLAMA_HOST", "http://127.0.0.1:11434")
OLLAMA_TIMEOUT = int(_env("OLLAMA_TIMEOUT", "300"))
# Qwen3 can "think" before answering. Off by default: it roughly triples the
# wait for a marginal gain on these tools, and keeps the transcript readable.
OLLAMA_THINK = _env("OLLAMA_THINK", "false").lower() in {"1", "true", "yes", "on"}
# How many tool calls the model may make before we stop it, per question.
MAX_TOOL_STEPS = int(_env("MAX_TOOL_STEPS", "10"))

# --- files ---
DATA_DIR = Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))).expanduser().resolve()
MAX_UPLOAD_BYTES = int(_env("MAX_UPLOAD_MB", "50")) * 1024 * 1024

# --- server ---
# 127.0.0.1 until Phase 06 puts a token check on the door. This app writes
# files; it should not be listening on the network without one.
APP_HOST = _env("APP_HOST", "127.0.0.1")
APP_PORT = int(_env("APP_PORT", "8000"))

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

# --- auth (enforced from Phase 06) ---
APP_TOKEN = _env("APP_TOKEN", "")

# Addresses that mean "this machine only", and tokens that are not tokens.
# They live here rather than in main.py so the check scripts can read them
# without importing the whole web framework.
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
WEAK_TOKENS = {"", "change-me", "changeme", "password", "token", "secret"}
MIN_TOKEN_LENGTH = 16


def token_is_configured() -> bool:
    token = (APP_TOKEN or "").strip()
    return token.lower() not in WEAK_TOKENS and len(token) >= MIN_TOKEN_LENGTH


class UnsafePathError(ValueError):
    """Raised when a requested path escapes DATA_DIR."""


def ensure_data_dir() -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR


def resolve_in_data_dir(name: str) -> Path:
    """Turn a model-supplied filename into a real path inside DATA_DIR.

    The model chooses these strings, so treat them as untrusted input. Anything
    that escapes DATA_DIR is rejected rather than clamped. Backslashes are
    normalised first so a Windows-style string is judged the same way on Linux,
    where a backslash would otherwise be an ordinary filename character.
    """
    ensure_data_dir()
    cleaned = str(name).strip().replace("\\", "/").strip("/")
    if not cleaned:
        raise UnsafePathError("No filename given.")

    parts = PurePosixPath(cleaned).parts
    if any(part == ".." for part in parts):
        raise UnsafePathError(f"Refusing path outside the data folder: {name}")
    # C:/..., \\server\share, /etc/...
    if len(cleaned) > 1 and cleaned[1] == ":":
        raise UnsafePathError(f"Refusing absolute path: {name}")

    candidate = (DATA_DIR / cleaned).expanduser()
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise UnsafePathError(f"Cannot resolve path: {name}") from exc
    if resolved != DATA_DIR and DATA_DIR not in resolved.parents:
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
