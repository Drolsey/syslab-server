"""The front door: one FastAPI app serving one HTML page and a small JSON API.

Run it:
    python -m app.main

Until Phase 06 this binds to 127.0.0.1 on purpose. There is no authentication
yet, and this app can write files, so it should not be listening on the network
before there is a check on the door.
"""

from __future__ import annotations

import hmac
import re
import secrets
import time
import unicodedata
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app import agent, config, tools
from app.config import (
    APP_HOST,
    APP_PORT,
    DATA_DIR,
    LOOPBACK,
    MAX_UPLOAD_BYTES,
    MIN_TOKEN_LENGTH,
    OLLAMA_MODEL,
    WEAK_TOKENS,
    UnsafePathError,
    code_fingerprint,
    ensure_data_dir,
    resolve_in_data_dir,
)
from app.llm import LlmError

WEB_DIR = Path(__file__).resolve().parent / "web"
ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xlsm"}

app = FastAPI(title="syslab-server", docs_url="/api/docs", redoc_url=None)

# Recorded at import so the Phase 05 check can tell a process that started
# itself at boot from one someone started by hand afterwards.
STARTED_AT = time.time()
CODE_FINGERPRINT = code_fingerprint()


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    history: list[dict[str, Any]] = Field(default_factory=list, max_length=60)


# --------------------------------------------------------------------------
# authentication
# --------------------------------------------------------------------------
#
# Tailscale already decides which machines can reach this app, and everything
# on the tailnet is WireGuard-encrypted end to end. This check is the second
# lock. It exists because the app writes real files, and one deliberate check
# in the app is worth more than trusting a network setting to stay configured
# the way you left it.

TOKEN_COOKIE = "syslab_token"
MAX_FAILURES = 8
FAILURE_WINDOW_SECONDS = 900

_failures: dict[str, list[float]] = defaultdict(list)


def token_is_configured() -> bool:
    """Read from config at call time, not import time, so tests can vary it."""
    token = (config.APP_TOKEN or "").strip()
    return token.lower() not in WEAK_TOKENS and len(token) >= MIN_TOKEN_LENGTH


def token_matches(candidate: str) -> bool:
    # compare_digest, not ==, so a wrong guess takes the same time as a right one
    return token_is_configured() and hmac.compare_digest(candidate, config.APP_TOKEN)


def supplied_token(request: Request) -> str:
    header = request.headers.get("x-syslab-token")
    if header:
        return header
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get(TOKEN_COOKIE, "")


def require_auth(request: Request) -> None:
    """Applied to every API route except login. Deliberately one function."""
    if not token_is_configured():
        raise HTTPException(
            503,
            "APP_TOKEN is not set in .env, so this app refuses to serve anything. "
            "Generate one with: python scripts/new_token.py",
        )
    if not token_matches(supplied_token(request)):
        raise HTTPException(401, "Not signed in.")


def _recent_failures(client: str) -> list[float]:
    cutoff = time.time() - FAILURE_WINDOW_SECONDS
    _failures[client] = [t for t in _failures[client] if t > cutoff]
    return _failures[client]


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def safe_upload_name(raw: str) -> str:
    """Reduce a browser-supplied filename to something safe to write.

    Browsers can send a full path, a name with a slash in it, or unicode that
    normalises into one. Take the last component, strip anything exotic, and
    require an extension we actually handle.
    """
    name = unicodedata.normalize("NFKC", raw or "").strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name).strip(". ")
    if not name:
        raise HTTPException(400, "That file needs a usable name.")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise HTTPException(
            400,
            f"Only {', '.join(sorted(ALLOWED_SUFFIXES))} files are accepted, not {suffix or 'a file with no extension'}.",
        )
    return name


def unique_path(name: str) -> Path:
    """Never silently overwrite something already in the folder."""
    path = resolve_in_data_dir(name)
    if not path.exists():
        return path
    stem, suffix = Path(name).stem, Path(name).suffix
    for n in range(2, 100):
        candidate = resolve_in_data_dir(f"{stem} ({n}){suffix}")
        if not candidate.exists():
            return candidate
    raise HTTPException(409, "Too many files with that name already.")


# --------------------------------------------------------------------------
# routes
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.post("/api/login")
def login(request: Request, response: Response, body: LoginRequest = Body(...)) -> dict:
    if not token_is_configured():
        raise HTTPException(
            503,
            "APP_TOKEN is not set in .env. Generate one with: python scripts/new_token.py",
        )

    client = request.client.host if request.client else "unknown"
    if len(_recent_failures(client)) >= MAX_FAILURES:
        raise HTTPException(429, "Too many wrong tokens. Wait fifteen minutes.")

    if not token_matches(body.token.strip()):
        _failures[client].append(time.time())
        time.sleep(0.4)  # slow down anyone trying tokens in a loop
        remaining = MAX_FAILURES - len(_recent_failures(client))
        raise HTTPException(401, f"That token is not right. {remaining} attempts left.")

    _failures.pop(client, None)
    response.set_cookie(
        TOKEN_COOKIE,
        config.APP_TOKEN,
        httponly=True,      # javascript on the page cannot read it
        samesite="lax",     # another site cannot make your browser use it
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return {"ok": True}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(TOKEN_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/health", dependencies=[Depends(require_auth)])
def health() -> dict:
    return {
        "ok": True,
        "model": OLLAMA_MODEL,
        "data_dir": str(DATA_DIR),
        "started_at": datetime.fromtimestamp(STARTED_AT).isoformat(timespec="seconds"),
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "code_fingerprint": CODE_FINGERPRINT,
    }


@app.get("/api/files", dependencies=[Depends(require_auth)])
def get_files() -> dict:
    return tools.list_files()


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload(file: UploadFile = File(...)) -> dict:
    ensure_data_dir()
    name = safe_upload_name(file.filename or "")
    contents = await file.read()
    if not contents:
        raise HTTPException(400, "That file is empty.")
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            413,
            f"That file is {len(contents) / 1_048_576:.1f} MB. The limit is "
            f"{MAX_UPLOAD_BYTES / 1_048_576:.0f} MB, set by MAX_UPLOAD_MB.",
        )
    path = unique_path(name)
    path.write_bytes(contents)
    return {
        "name": path.name,
        "size_kb": round(len(contents) / 1024, 1),
        "renamed": path.name != name,
    }


@app.get("/api/files/{name}", dependencies=[Depends(require_auth)])
def download(name: str) -> FileResponse:
    try:
        path = resolve_in_data_dir(name)
    except UnsafePathError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not path.is_file():
        raise HTTPException(404, f"No file named {name!r}.")
    return FileResponse(path, filename=path.name)


@app.post("/api/chat", dependencies=[Depends(require_auth)])
def chat(request: ChatRequest = Body(...)) -> JSONResponse:
    try:
        outcome = agent.ask(request.message, history=request.history)
    except LlmError as exc:
        # The model server being down is an expected condition, not a bug.
        return JSONResponse(status_code=503, content={"detail": str(exc)})

    return JSONResponse(
        {
            "answer": outcome["answer"],
            "steps": [
                {
                    "tool": step["tool"],
                    "arguments": step["arguments"],
                    "ok": step["ok"],
                    "error": step["error"],
                }
                for step in outcome["steps"]
            ],
            "history": [m for m in outcome["messages"] if m.get("role") != "system"],
            "hit_step_limit": outcome.get("hit_step_limit", False),
        }
    )


def main() -> None:
    import uvicorn

    ensure_data_dir()

    # The one guard worth having: never listen beyond this machine without a
    # real token. Getting this order wrong is how a file-writing app ends up
    # open on a network, and it is a mistake you only get to make once.
    if APP_HOST not in LOOPBACK and not token_is_configured():
        print("\nRefusing to start.\n")
        print(f"  APP_HOST is {APP_HOST}, which listens beyond this machine, but")
        print("  APP_TOKEN is missing or too weak. This app can write files.\n")
        print("  Fix it:   python scripts/new_token.py")
        print("  Or set:   APP_HOST=127.0.0.1\n")
        raise SystemExit(2)

    print(f"syslab-server: model {OLLAMA_MODEL}, files in {DATA_DIR}")
    if APP_HOST in LOOPBACK:
        print("Listening on this machine only. Phase 06 opens it to your tailnet.")
    else:
        print(f"Listening on {APP_HOST}: reachable from your tailnet, token required.")
    print(f"Open http://127.0.0.1:{APP_PORT}")
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="info")


if __name__ == "__main__":
    main()
