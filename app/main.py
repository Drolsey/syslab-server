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
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from app import agent, config, context, gateway, jobs, search, tenancy, tools
from app.config import (
    APP_HOST,
    APP_PORT,
    DATA_ROOT,
    LLM_MODEL,
    LOOPBACK,
    MAX_UPLOAD_BYTES,
    MIN_TOKEN_LENGTH,
    WEAK_TOKENS,
    UnsafePathError,
    code_fingerprint,
    data_dir,
    ensure_data_dir,
    resolve_in_data_dir,
)
from app.llm import LlmError

WEB_DIR = Path(__file__).resolve().parent / "web"
ALLOWED_SUFFIXES = {".pdf", ".xlsx", ".xlsm"}

# In public mode the interactive docs and the schema behind them are not
# served at all. docs_url alone is not enough: the docs page is only a reader
# for /openapi.json, and leaving that up publishes every route, including the
# ones that write files, to anyone who asks. Both go, or neither does.
app = FastAPI(
    title="syslab-server",
    docs_url=None if config.PUBLIC_MODE else "/api/docs",
    redoc_url=None,
    openapi_url=None if config.PUBLIC_MODE else "/openapi.json",
)

# The inference plane (Step 3.2). Its routes carry their own token check and
# never resolve a tenant -- see app/gateway.py and the boundary in Section 2
# of the architecture plan. Mounted here so all three planes share one
# process, as the plan's architecture diagram has them.
app.include_router(gateway.router)

# Recorded at import so the Phase 05 check can tell a process that started
# itself at boot from one someone started by hand afterwards.
STARTED_AT = time.time()
CODE_FINGERPRINT = code_fingerprint()


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------

class LoginRequest(BaseModel):
    token: str = Field(min_length=1, max_length=500)


class JobRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)


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
# The most client addresses whose recent failures are remembered at once.
# There is no correct number; there is only "bounded" versus "not bounded".
MAX_TRACKED_CLIENTS = 4096

# A plain dict, NOT a defaultdict, and that is the fix rather than a style
# preference. Reading _failures[client] on a defaultdict CREATES the key, so
# the old code grew an entry for every address that ever tried to sign in,
# successful or not, and never removed one: the timestamps inside a key were
# pruned, the keys themselves never were. On a tailnet that is a leak slow
# enough never to matter. Facing the internet it is free memory exhaustion
# from an attacker who only has to vary their source address.
_failures: dict[str, list[float]] = {}


def token_is_configured() -> bool:
    """Read from config at call time, not import time, so tests can vary it."""
    token = (config.APP_TOKEN or "").strip()
    return token.lower() not in WEAK_TOKENS and len(token) >= MIN_TOKEN_LENGTH


def token_matches(candidate: str) -> bool:
    """Does this match the operator's own token in .env?"""
    # compare_digest, not ==, so a wrong guess takes the same time as a right one
    return token_is_configured() and hmac.compare_digest(candidate, config.APP_TOKEN)


def tenant_for_token(candidate: str) -> str | None:
    """Which tenant does this token belong to? None means nobody.

    Two sources, in this order:

    1. The control plane. This is the real one: a token issued by
       scripts\tenant.py, stored as a hash, revocable, and belonging to exactly
       one tenant.
    2. APP_TOKEN in .env, which means the bootstrap tenant. This is a BRIDGE,
       kept so that this install and every existing device keep working on the
       day tenancy lands. It is not revocable and it is not in the control
       plane. It goes away once real tokens are issued.

    A control plane that will not open authenticates nobody through path 1 and
    falls through to path 2, so a broken control plane leaves the operator able
    to get in and fix it, and nobody else able to get in at all.
    """
    candidate = (candidate or "").strip()
    if not candidate:
        return None
    try:
        tenant = tenancy.resolve_token(candidate)
    except Exception:  # noqa: BLE001
        tenant = None
    if tenant:
        return tenant["id"]
    if token_matches(candidate):
        return config.BOOTSTRAP_TENANT
    return None


def anyone_can_sign_in() -> bool:
    """Is there any credential at all? If not, this app serves nothing."""
    return token_is_configured() or tenancy.has_any_active_token()


def supplied_token(request: Request) -> str:
    header = request.headers.get("x-syslab-token")
    if header:
        return header
    authorization = request.headers.get("authorization", "")
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return request.cookies.get(TOKEN_COOKIE, "")


async def require_auth(request: Request):
    """Applied to every API route except login. Deliberately one function.

    It answers two questions at once, on purpose: may this request happen, and
    whose data is it? Splitting them would mean a request that is authenticated
    but ownerless, which is the state everything else in this app now refuses
    to be in.

    ASYNC, and that is not cosmetic. FastAPI runs a `def` dependency in one
    anyio worker thread and a `def` endpoint in another; a context flows into a
    worker thread and never back out, so a tenant set in a sync dependency is
    gone before the endpoint runs. Measured in 1.1, asserted in
    tests/test_context.py.

    The yield is the teardown: the tenant is put back when the response is
    done, so nothing carries into the next request on this thread.
    """
    if not anyone_can_sign_in():
        raise HTTPException(
            503,
            "There is no way to sign in to this server: APP_TOKEN is not set in "
            ".env and no tenant has an active token. It refuses to serve anything "
            "rather than open the door. Generate one with: py scripts/new_token.py "
            "or py scripts/tenant.py new \"Name\"",
        )

    tenant = tenant_for_token(supplied_token(request))
    if tenant is None:
        raise HTTPException(401, "Not signed in.")

    reset = context.set_tenant(tenant)
    try:
        yield
    finally:
        context.reset_tenant(reset)


def _sweep_failures(cutoff: float) -> None:
    """Forget clients whose failures have all aged out, and cap what is left.

    Forgetting a throttle entry is always the safe direction: it gives an
    attacker nothing they did not already have by waiting out the window, and
    it can never lock out someone who belongs here.
    """
    for client in [c for c, times in _failures.items() if not any(t > cutoff for t in times)]:
        del _failures[client]
    if len(_failures) > MAX_TRACKED_CLIENTS:
        # Still over the cap, so somebody is deliberately varying their
        # address. Drop the least recently failing first.
        oldest_first = sorted(_failures.items(), key=lambda item: max(item[1]))
        for client, _ in oldest_first[: len(_failures) - MAX_TRACKED_CLIENTS]:
            del _failures[client]


def client_address(request: Request) -> str:
    """Who to hold the login throttle against.

    Behind Cloudflare Tunnel every request arrives from the cloudflared
    container, so `request.client.host` is one address for the whole internet.
    The throttle then counts the world's wrong guesses into a single bucket:
    eight from anybody locks out everybody, which turns a rate limit into a
    denial of service against the operator. Cloudflare puts the real address in
    CF-Connecting-IP, and it sets that header itself, discarding whatever the
    caller sent.

    Guarded by a setting rather than always trusted, because the danger runs
    the other way round when nothing is in front: a header anyone may set is a
    throttle anyone may evade by varying one string. TRUST_CLIENT_IP_HEADER is
    therefore off by default and is only true when the app is genuinely
    unreachable except through the tunnel -- which is the same condition
    docs/runbook.md makes the operator assert when they publish it.
    """
    if config.TRUST_CLIENT_IP_HEADER:
        forwarded = request.headers.get("cf-connecting-ip", "").strip()
        if forwarded:
            # One address, never a list: CF-Connecting-IP is a single value.
            # X-Forwarded-For is deliberately not read -- it is caller-appended
            # and the left-most entry is whatever an attacker typed.
            return forwarded[:64]
    return request.client.host if request.client else "unknown"


def _recent_failures(client: str) -> list[float]:
    cutoff = time.time() - FAILURE_WINDOW_SECONDS
    _sweep_failures(cutoff)
    recent = [t for t in _failures.get(client, []) if t > cutoff]
    # Only write back a key that has something in it. An empty list is the
    # same information as no key at all, and one of the two is unbounded.
    if recent:
        _failures[client] = recent
    else:
        _failures.pop(client, None)
    return recent


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
    # The page is an operator tool, not a product surface (Section 11 of the
    # architecture plan). Unauthenticated it is harmless on a tailnet and it
    # is an advertisement on the public internet, so in public mode there is
    # nothing here. 404 rather than 401: "nothing to see" is a smaller
    # disclosure than "something here needs a password".
    if config.PUBLIC_MODE:
        raise HTTPException(404, "Not found.")
    return HTMLResponse((WEB_DIR / "index.html").read_text(encoding="utf-8"))


@app.post("/api/login")
def login(request: Request, response: Response, body: LoginRequest = Body(...)) -> dict:
    if not anyone_can_sign_in():
        raise HTTPException(
            503,
            "There is no way to sign in to this server. Generate a token with: "
            "py scripts/new_token.py or py scripts/tenant.py new \"Name\"",
        )

    client = client_address(request)
    if len(_recent_failures(client)) >= MAX_FAILURES:
        raise HTTPException(429, "Too many wrong tokens. Wait fifteen minutes.")

    presented = body.token.strip()
    if tenant_for_token(presented) is None:
        _failures.setdefault(client, []).append(time.time())
        time.sleep(0.4)  # slow down anyone trying tokens in a loop
        remaining = MAX_FAILURES - len(_recent_failures(client))
        raise HTTPException(401, f"That token is not right. {remaining} attempts left.")

    _failures.pop(client, None)
    # The token they signed in with, NOT config.APP_TOKEN. Setting the cookie
    # to the operator's token handed every tenant the operator's credential,
    # which with one tenant looked like a tidy way to normalise it and with two
    # is a privilege escalation.
    response.set_cookie(
        TOKEN_COOKIE,
        presented,
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
        "model": LLM_MODEL,
        "data_dir": str(data_dir()),
        "started_at": datetime.fromtimestamp(STARTED_AT).isoformat(timespec="seconds"),
        "job_kinds": jobs.lane.kinds,
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

    # Read in chunks and stop at the limit. Reading the whole body first and
    # checking its length afterwards means the limit is enforced only after the
    # server has already held the entire file in memory, so MAX_UPLOAD_MB
    # protects the disk and nothing else.
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(1_048_576)
        if not chunk:
            break
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"That file is larger than the {MAX_UPLOAD_BYTES / 1_048_576:.0f} MB "
                "limit set by MAX_UPLOAD_MB.",
            )
        chunks.append(chunk)
    contents = b"".join(chunks)
    if not contents:
        raise HTTPException(400, "That file is empty.")
    path = unique_path(name)
    path.write_bytes(contents)

    # Index it now, while we have it. Doing this at upload is what lets someone
    # later find a document by what is in it rather than by remembering its name.
    # A failure here must never lose the upload: the file is already on disk and
    # scripts/check_search.py can rebuild the index at any time.
    indexed = False
    try:
        indexed = bool(search.index_file(path).get("indexed"))
    except Exception:  # noqa: BLE001
        pass

    return {
        "name": path.name,
        "size_kb": round(len(contents) / 1024, 1),
        "renamed": path.name != name,
        "searchable": indexed,
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


# --------------------------------------------------------------------------
# the job lane
# --------------------------------------------------------------------------
#
# Slow work does not block a request. Submitting returns 202 and an id; the
# browser polls. This is what stops one person's image generation from making
# everybody else wait for an answer.

@app.post("/api/jobs", status_code=202, dependencies=[Depends(require_auth)])
def submit_job(body: JobRequest = Body(...)) -> dict:
    try:
        job = jobs.lane.submit(body.kind, body.params)
    except jobs.JobError as exc:
        raise HTTPException(400, str(exc)) from exc
    return job.public(jobs.lane.position_of(job.id))


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
def list_jobs() -> dict:
    return jobs.lane.snapshot()


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
def get_job(job_id: str) -> dict:
    try:
        job = jobs.lane.get(job_id)
    except jobs.JobError as exc:
        raise HTTPException(404, str(exc)) from exc
    return job.public(jobs.lane.position_of(job_id))


@app.delete("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
def cancel_job(job_id: str) -> dict:
    try:
        return jobs.lane.cancel(job_id).public()
    except jobs.JobError as exc:
        code = 404 if "No job with id" in str(exc) else 409
        raise HTTPException(code, str(exc)) from exc


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

    # Not ensure_data_dir(): startup has no request and therefore no tenant, and
    # asking for one here would raise. The root is what needs to exist; a
    # tenant's folder is made when a tenant first uses it.
    DATA_ROOT.mkdir(parents=True, exist_ok=True)

    # The one guard worth having: never listen beyond this machine without a
    # real token. Getting this order wrong is how a file-writing app ends up
    # open on a network, and it is a mistake you only get to make once.
    if APP_HOST not in LOOPBACK and not token_is_configured():
        print("\nRefusing to start.\n")
        print(f"  APP_HOST is {APP_HOST}, which listens beyond this machine, but")
        print("  APP_TOKEN is missing or too weak. This app can write files.\n")
        print("  Fix it:   py scripts/new_token.py")
        print("  Or set:   APP_HOST=127.0.0.1\n")
        raise SystemExit(2)

    print(f"syslab-server: model {LLM_MODEL}, files under {DATA_ROOT}")
    if APP_HOST in LOOPBACK:
        print("Listening on this machine only. Phase 06 opens it to your tailnet.")
    else:
        print(f"Listening on {APP_HOST}: reachable from your tailnet, token required.")
    print(f"Open http://127.0.0.1:{APP_PORT}")
    uvicorn.run(app, host=APP_HOST, port=APP_PORT, log_level="info")


if __name__ == "__main__":
    main()
