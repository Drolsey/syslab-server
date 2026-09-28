"""The operator dashboard's door: /admin and /admin/api/* (Step 11).

Three locks, in the order a request meets them, and each is there because the
one before it can be misconfigured:

1. NOT MOUNTED in PUBLIC_MODE (app/main.py), and refused per request if the
   setting says public anyway.
2. The TCP peer must be inside ADMIN_ALLOWED_NETWORKS. Never a forwarded
   header: behind a tunnel every request arrives from the tunnel's container,
   and a header anyone can set is not an address. Docker's 172.16.0.0/12 is
   left out of the default for that reason.
3. An operator session, in a cookie scoped to /admin so the browser never
   sends it to /v1, /api or /api/v1, and SameSite=Strict.

Plus one rule for every write: it must carry `X-Syslab-Admin: 1`. No CORS is
configured on this server, so a page on another site cannot add that header,
and a forged form post from one fails here rather than acting as the operator.

Refusals from the first two locks are 404, not 403: "nothing here" is a
smaller disclosure than "something here that you may not use".
"""

from __future__ import annotations

import ipaddress
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from app import config, context, corpus, intake, jobs, operators, plane, retrieve, tenancy, throttle

COOKIE = "syslab_admin"
COOKIE_PATH = "/admin"
WEB = Path(__file__).resolve().parent / "web" / "admin"
# Served by name, from a list, rather than by a path parameter into a folder.
ASSETS = {"admin.css": "text/css", "admin.js": "text/javascript", "logo.png": "image/png"}


def peer(request: Request) -> str:
    return request.client.host if request.client else ""


def _networks() -> list:
    """Parsed per call so a test can change the setting; a bad entry is skipped (fails closed)."""
    parsed = []
    for entry in config.ADMIN_ALLOWED_NETWORKS:
        try:
            parsed.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return parsed


def on_allowed_network(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    # A dual-stack listener reports an IPv4 client as ::ffff:a.b.c.d.
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in network for network in _networks() if network.version == ip.version)


async def lan_only(request: Request) -> None:
    if config.PUBLIC_MODE or not on_allowed_network(peer(request)):
        raise HTTPException(404, "Not found.")


async def writes_carry_the_header(request: Request) -> None:
    if request.method not in {"GET", "HEAD"} and request.headers.get("x-syslab-admin") != "1":
        raise HTTPException(403, "This request did not come from the dashboard.")


async def require_operator(request: Request) -> dict:
    operator = operators.resolve_session(request.cookies.get(COOKIE, ""))
    if operator is None:
        raise HTTPException(401, "Not signed in.")
    request.state.operator = operator
    return operator


def audit(request: Request, action: str, target: str | None = None,
          detail: dict | None = None) -> None:
    """Record a change made through the dashboard, under the operator who made it."""
    operator = getattr(request.state, "operator", None)
    operators.audit(operator["id"] if operator else None, action, target, detail, peer(request))


router = APIRouter(
    include_in_schema=False,  # not part of any published surface
    dependencies=[Depends(lan_only), Depends(writes_carry_the_header)],
)


# --------------------------------------------------------------------------
# the page
# --------------------------------------------------------------------------

@router.get("/admin", response_class=HTMLResponse)
def page() -> HTMLResponse:
    return HTMLResponse((WEB / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-cache"})


@router.get("/admin/assets/{name}")
def asset(name: str) -> FileResponse:
    if name not in ASSETS:
        raise HTTPException(404, "Not found.")
    return FileResponse(WEB / name, media_type=ASSETS[name], headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------------
# signing in
# --------------------------------------------------------------------------

class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=operators.MAX_PASSWORD)


class PasswordChange(BaseModel):
    current: str = Field(min_length=1, max_length=operators.MAX_PASSWORD)
    new: str = Field(min_length=1, max_length=operators.MAX_PASSWORD)


@router.post("/admin/api/login")
def login(request: Request, response: Response, body: LoginRequest = Body(...)) -> dict:
    """Sync on purpose: scrypt and the failure delay block, so they get a worker thread."""
    if not operators.any_active():
        raise HTTPException(
            503,
            "No operator account exists yet. On the server, run: "
            "py scripts/operator_account.py new <username>",
        )
    # Held against the address AND the account, in the same throttle the
    # tenant login uses: one address cannot guess many accounts, and many
    # addresses cannot share the guessing of one.
    keys = [throttle.client_address(request), f"operator:{body.username.strip().lower()[:40]}"]
    if any(len(throttle._recent_failures(k)) >= throttle.MAX_FAILURES for k in keys):
        raise HTTPException(429, "Too many wrong attempts. Wait fifteen minutes.")

    operator = operators.verify_login(body.username, body.password)
    if operator is None:
        for key in keys:
            throttle._failures.setdefault(key, []).append(time.time())
        operators.audit(None, "login_failed", body.username.strip().lower()[:40],
                        client=peer(request))
        time.sleep(0.4)
        raise HTTPException(401, "That username or password is not right.")

    for key in keys:
        throttle._failures.pop(key, None)
    token = operators.start_session(operator["id"], peer(request))
    response.set_cookie(
        COOKIE, token, httponly=True, samesite="strict", path=COOKIE_PATH,
        max_age=config.ADMIN_SESSION_HOURS * 3600,
    )
    request.state.operator = operator
    audit(request, "login")
    return {"operator": operator}


@router.post("/admin/api/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(COOKIE, "")
    operator = operators.resolve_session(token)
    if token:
        operators.end_session(token)
    if operator:
        request.state.operator = operator
        audit(request, "logout")
    response.delete_cookie(COOKIE, path=COOKIE_PATH)
    return {"ok": True}


@router.get("/admin/api/me")
def me(operator: dict = Depends(require_operator)) -> dict:
    return {"operator": operator}


@router.post("/admin/api/me/password")
def change_password(request: Request, body: PasswordChange = Body(...),
                    operator: dict = Depends(require_operator)) -> dict:
    if operators.verify_login(operator["id"], body.current) is None:
        raise HTTPException(400, "The current password is not right.")
    try:
        operators.set_password(operator["id"], body.new)
    except operators.OperatorError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(request, "password_changed", operator["id"])
    return {"ok": True, "signed_out": True}


# --------------------------------------------------------------------------
# overview (11.4)
# --------------------------------------------------------------------------

STARTED_AT = time.time()
CODE_FINGERPRINT = config.code_fingerprint()


def _probe(base_url: str) -> dict:
    """Is an OpenAI-compatible server answering, and what does it serve?"""
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=2) as reply:
            data = json.loads(reply.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"reachable": False, "error": str(exc)[:200]}
    models = [
        {"id": m.get("id"), "max_model_len": m.get("max_model_len")}
        for m in data.get("data", []) if isinstance(m, dict)
    ]
    return {"reachable": True, "models": models}


def _companies() -> list[dict]:
    """Every tenant, plus the bootstrap tenant if it has only a folder and no row."""
    rows = tenancy.list_tenants()
    if all(t["id"] != config.BOOTSTRAP_TENANT for t in rows) and \
            (config.DATA_ROOT / config.BOOTSTRAP_TENANT).is_dir():
        rows.append({"id": config.BOOTSTRAP_TENANT, "name": "Bootstrap (APP_TOKEN)",
                     "created_at": None, "active": True, "bootstrap": True})
    return rows


def _company(company_id: str) -> dict:
    for company in _companies():
        if company["id"] == company_id:
            return company
    raise HTTPException(404, f"No company {company_id!r}.")


@router.get("/admin/api/overview")
def overview(_: dict = Depends(require_operator)) -> dict:
    companies = _companies()
    documents = 0
    for company in companies:
        with context.use_tenant(company["id"]):
            documents += len(config.data_files())
    return {
        "app": {
            "started_at": datetime.fromtimestamp(STARTED_AT, timezone.utc).isoformat(timespec="seconds"),
            "uptime_seconds": round(time.time() - STARTED_AT),
            "code_fingerprint": CODE_FINGERPRINT,
        },
        "chat": {"base_url": config.LLM_BASE_URL, **_probe(config.LLM_BASE_URL)},
        "embed": {"base_url": config.EMBED_BASE_URL, "registered": "vector" in retrieve.registered(),
                  **_probe(config.EMBED_BASE_URL)},
        "retrievers": sorted(retrieve.registered()),
        "jobs": jobs.lane.counts(),
        "companies": {"total": len(companies), "active": sum(bool(c["active"]) for c in companies),
                      "documents": documents},
        "recent": operators.recent_audit(8),
    }


# --------------------------------------------------------------------------
# companies (11.5)
# --------------------------------------------------------------------------

class CompanyCreate(BaseModel):
    id: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=100)


class FolderCreate(BaseModel):
    path: str = Field(min_length=1, max_length=corpus.MAX_PATH)


class FileRef(BaseModel):
    path: str = Field(min_length=1, max_length=corpus.MAX_PATH)


@router.get("/admin/api/companies")
def list_companies(_: dict = Depends(require_operator)) -> dict:
    out = []
    for company in _companies():
        with context.use_tenant(company["id"]):
            out.append({**company, **corpus.summary()})
    return {"companies": out, "accepted_types": corpus.accepted_suffixes()}


@router.post("/admin/api/companies")
def create_company(request: Request, body: CompanyCreate = Body(...),
                   _: dict = Depends(require_operator)) -> dict:
    try:
        company = tenancy.create_tenant(body.name, tenant_id=body.id.strip().lower())
    except tenancy.TenancyError as exc:
        raise HTTPException(400, str(exc)) from exc
    with context.use_tenant(company["id"]):
        config.ensure_data_dir()
    audit(request, "company_created", company["id"], {"name": company["name"]})
    return {"company": company}


def _set_active(request: Request, company_id: str, active: bool) -> dict:
    if _company(company_id).get("bootstrap"):
        raise HTTPException(400, "The bootstrap company is managed through .env, not here.")
    company = tenancy.set_disabled(company_id, not active)
    audit(request, "company_enabled" if active else "company_disabled", company_id)
    return {"company": company}


@router.post("/admin/api/companies/{company_id}/disable")
def disable_company(request: Request, company_id: str, _: dict = Depends(require_operator)) -> dict:
    return _set_active(request, company_id, False)


@router.post("/admin/api/companies/{company_id}/enable")
def enable_company(request: Request, company_id: str, _: dict = Depends(require_operator)) -> dict:
    return _set_active(request, company_id, True)


@router.delete("/admin/api/companies/{company_id}")
def delete_company(request: Request, company_id: str, confirm: str = "",
                   _: dict = Depends(require_operator)) -> dict:
    company = _company(company_id)
    if company.get("bootstrap") or company_id == config.BOOTSTRAP_TENANT:
        raise HTTPException(400, "The bootstrap company cannot be deleted from the dashboard.")
    if confirm != company_id:
        raise HTTPException(400, "Type the company id to confirm.")
    if company["active"]:
        raise HTTPException(409, "Disable the company first. Deleting is the second, separate step.")
    result = corpus.retire_company(company_id)
    audit(request, "company_deleted", company_id, {"files": result["files"]})
    return result


# --------------------------------------------------------------------------
# a company's corpus (11.5)
# --------------------------------------------------------------------------

@router.get("/admin/api/companies/{company_id}/files")
def browse(company_id: str, path: str = "", _: dict = Depends(require_operator)) -> dict:
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            return corpus.listing(path)
        except corpus.CorpusError as exc:
            raise HTTPException(404, str(exc)) from exc


@router.post("/admin/api/companies/{company_id}/files")
def upload(request: Request, company_id: str, file: UploadFile = File(...),
           folder: str = Form(""), replace: bool = Form(False),
           _: dict = Depends(require_operator)) -> dict:
    """One file, into `folder` (which may itself be a/b/c from a folder upload). Never ingests."""
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            result = corpus.store(folder, file.filename or "", file.file, replace=replace)
        except corpus.CorpusError as exc:
            raise HTTPException(400, str(exc)) from exc
    if result["status"] in {"stored", "replaced"}:
        audit(request, "file_uploaded", f"{company_id}:{result['path']}",
              {"status": result["status"], "bytes": result.get("bytes")})
    return result


@router.post("/admin/api/companies/{company_id}/folders")
def new_folder(request: Request, company_id: str, body: FolderCreate = Body(...),
               _: dict = Depends(require_operator)) -> dict:
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            made = corpus.make_folder(body.path)
        except corpus.CorpusError as exc:
            raise HTTPException(400, str(exc)) from exc
    audit(request, "folder_created", f"{company_id}:{made}")
    return {"path": made}


@router.post("/admin/api/companies/{company_id}/trash")
def remove(request: Request, company_id: str, body: FileRef = Body(...),
           _: dict = Depends(require_operator)) -> dict:
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            result = corpus.trash(body.path)
        except corpus.CorpusError as exc:
            raise HTTPException(400, str(exc)) from exc
    audit(request, "moved_to_trash", f"{company_id}:{result['path']}")
    return {"path": result["path"], "swept": result["swept"]}


@router.get("/admin/api/companies/{company_id}/download")
def download(company_id: str, path: str, _: dict = Depends(require_operator)) -> FileResponse:
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            found = corpus.download_path(path)
        except corpus.CorpusError as exc:
            raise HTTPException(404, str(exc)) from exc
    return FileResponse(found, filename=found.name)


@router.post("/admin/api/companies/{company_id}/ingest")
def ingest_company(request: Request, company_id: str, _: dict = Depends(require_operator)) -> dict:
    """Reconcile and produce the whole folder, as a job. Picks up anything outstanding."""
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            job = jobs.lane.submit(intake.FOLDER_INGEST, {"only_fast": False})
        except jobs.JobError as exc:
            raise HTTPException(429, str(exc)) from exc
        public = job.public(jobs.lane.position_of(job.id))
    audit(request, "ingest_started", company_id, {"job": job.id})
    return public


@router.post("/admin/api/companies/{company_id}/ingest-file")
def ingest_one(request: Request, company_id: str, body: FileRef = Body(...),
               _: dict = Depends(require_operator)) -> dict:
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            path = corpus.download_path(body.path)
        except corpus.CorpusError as exc:
            raise HTTPException(404, str(exc)) from exc
        outcome = intake.arrived(path)
    audit(request, "file_reingested", f"{company_id}:{body.path}")
    return outcome


@router.get("/admin/api/companies/{company_id}/jobs")
def company_jobs(company_id: str, _: dict = Depends(require_operator)) -> dict:
    _company(company_id)
    with context.use_tenant(company_id):
        return jobs.lane.snapshot()


@router.post("/admin/api/companies/{company_id}/jobs/{job_id}/cancel")
def cancel_job(request: Request, company_id: str, job_id: str,
               _: dict = Depends(require_operator)) -> dict:
    _company(company_id)
    with context.use_tenant(company_id):
        try:
            job = jobs.lane.cancel(job_id).public()
        except jobs.JobError as exc:
            raise HTTPException(404 if "No job with id" in str(exc) else 409, str(exc)) from exc
    audit(request, "job_cancelled", f"{company_id}:{job_id}")
    return job


# --------------------------------------------------------------------------
# retrieval tester (11.6)
# --------------------------------------------------------------------------

@router.post("/admin/api/companies/{company_id}/retrieve")
def try_retrieval(company_id: str, body: plane.RetrieveRequest = Body(...),
                  _: dict = Depends(require_operator)) -> dict:
    """Exactly what POST /api/v1/retrieve answers for this company: the same function.

    Read-only, so not audited.
    """
    _company(company_id)
    with context.use_tenant(company_id):
        return plane.retrieve_passages(body)
