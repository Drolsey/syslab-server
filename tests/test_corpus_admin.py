"""The dashboard's companies, corpus and retrieval tester, Steps 11.5 and 11.6.

The file rules first (app/corpus.py: what a browser-supplied path may become,
what an upload may be, what trash leaves behind), then the whole flow over
HTTP as an operator does it: create a company, upload a folder, ingest, look,
search, remove, delete.
"""

from __future__ import annotations

import io
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, context, corpus, ingest, main, operators, passages, plane, tenancy, tools
from tests.test_admin import PASSWORD, WRITE, client_for, sign_in

BODY = "The supplier shall indemnify the buyer against loss of freight in transit. " * 30


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def test_a_relative_path_is_cleaned_segment_by_segment():
    assert corpus.clean_relative("Contracts/2024/lease.pdf") == "Contracts/2024/lease.pdf"
    assert corpus.clean_relative("a\\b\\c.pdf") == "a/b/c.pdf"
    assert corpus.clean_relative("we<ird>/na:me?.pdf") == "we_ird_/na_me_.pdf"
    # A client's corpus may be named in Arabic; letters in any script survive.
    assert corpus.clean_relative("عقود/عقد إيجار.pdf") == "عقود/عقد إيجار.pdf"


def test_hidden_segments_are_skipped_not_renamed():
    assert corpus.clean_relative(".git/config.pdf") is None
    assert corpus.clean_relative("docs/.DS_Store") is None
    assert corpus.clean_relative("docs/._lease.pdf") is None


def test_climbing_out_and_absurd_paths_are_refused():
    with pytest.raises(corpus.CorpusError):
        corpus.clean_relative("../escape.pdf")
    with pytest.raises(corpus.CorpusError):
        corpus.clean_relative("a/../../escape.pdf")
    with pytest.raises(corpus.CorpusError):
        corpus.clean_relative("/".join(["d"] * (corpus.MAX_DEPTH + 1)) + "/x.pdf")
    with pytest.raises(corpus.CorpusError):
        corpus.clean_relative("x" * (corpus.MAX_PATH + 1) + ".pdf")


# --------------------------------------------------------------------------
# storing
# --------------------------------------------------------------------------

def test_store_outcomes(tenant_storage):
    assert corpus.store("a", "x.pdf", io.BytesIO(b"%PDF-1"))["status"] == "stored"
    assert corpus.store("a", "x.pdf", io.BytesIO(b"%PDF-2"))["status"] == "exists"
    assert (tenant_storage / "a" / "x.pdf").read_bytes() == b"%PDF-1"
    assert corpus.store("a", "x.pdf", io.BytesIO(b"%PDF-2"), replace=True)["status"] == "replaced"
    assert (tenant_storage / "a" / "x.pdf").read_bytes() == b"%PDF-2"
    assert corpus.store("a", "x.exe", io.BytesIO(b"MZ"))["status"] == "skipped"
    assert corpus.store(".git", "x.pdf", io.BytesIO(b"%PDF"))["status"] == "skipped"
    assert not (tenant_storage / "a" / "x.exe").exists()


def test_an_oversized_or_empty_upload_leaves_nothing_behind(tenant_storage):
    with pytest.raises(corpus.CorpusError):
        corpus.store("big", "x.pdf", io.BytesIO(b"x" * 2048), max_bytes=1024)
    with pytest.raises(corpus.CorpusError):
        corpus.store("big", "y.pdf", io.BytesIO(b""))
    assert [p.name for p in (tenant_storage / "big").iterdir()] == []


def test_store_cannot_write_over_a_folder(tenant_storage):
    (tenant_storage / "clash.pdf").mkdir()
    with pytest.raises(corpus.CorpusError):
        corpus.store("", "clash.pdf", io.BytesIO(b"%PDF"), replace=True)


# --------------------------------------------------------------------------
# trash
# --------------------------------------------------------------------------

def test_trashing_a_folder_moves_it_and_leaves_no_orphans(tenant_storage):
    tools.write_pdf("old/lease.pdf", title="Lease", body=BODY)
    tools.write_pdf("keep/lease.pdf", title="Lease", body=BODY)
    assert {r["source"] for r in passages.search_passages("freight")["results"]} == {
        "old/lease.pdf", "keep/lease.pdf"}

    result = corpus.trash("old")
    assert result["path"] == "old"
    assert not (tenant_storage / "old").exists()
    assert list(config.TRASH_ROOT.rglob("lease.pdf"))  # moved, not deleted
    assert result["swept"]["artifacts"] == ["old/lease.pdf"]
    assert {r["source"] for r in passages.search_passages("freight")["results"]} == {"keep/lease.pdf"}
    assert ingest.status("keep/lease.pdf")["ready"]


def test_trash_refuses_the_root_and_the_outside(tenant_storage):
    for bad in ("", ".", "../other", "missing.pdf"):
        with pytest.raises(corpus.CorpusError):
            corpus.trash(bad)


def test_listing_shows_folders_then_files_with_state(tenant_storage):
    tools.write_pdf("contracts/a.pdf", title="A", body=BODY)
    (tenant_storage / "notes.bin").write_bytes(b"\0")
    top = corpus.listing("")
    assert [f["name"] for f in top["folders"]] == ["contracts"]
    assert [(f["name"], f["state"]) for f in top["files"]] == [("notes.bin", "unsupported")]
    inside = corpus.listing("contracts")
    assert inside["path"] == "contracts"
    assert [(f["path"], f["state"]) for f in inside["files"]] == [("contracts/a.pdf", "ready")]
    with pytest.raises(corpus.CorpusError):
        corpus.listing("../..")


# --------------------------------------------------------------------------
# the flow over HTTP
# --------------------------------------------------------------------------

@pytest.fixture()
def operator_client(tenant_storage):
    operators.create("amro", "Amro Taha", PASSWORD)
    client = client_for(main.app)
    assert sign_in(client).status_code == 200
    return client


def wait_for_jobs(client, company, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs = client.get(f"/admin/api/companies/{company}/jobs").json()["jobs"]
        if jobs and all(j["status"] in {"done", "failed", "cancelled"} for j in jobs):
            return jobs
        time.sleep(0.2)
    raise AssertionError("ingestion did not finish")


def pdf_bytes(tenant_storage, name: str) -> bytes:
    tools.write_pdf(f"_scratch/{name}", title="Lease", body=BODY)
    return (tenant_storage / "_scratch" / name).read_bytes()


def test_an_operator_builds_a_corpus_end_to_end(tenant_storage, operator_client):
    client = operator_client
    lease = pdf_bytes(tenant_storage, "lease.pdf")

    made = client.post("/admin/api/companies", json={"id": "acme-beta", "name": "Acme Beta"}, headers=WRITE)
    assert made.status_code == 200
    assert (config.DATA_ROOT / "acme-beta").is_dir()

    for folder in ("contracts", "contracts/2024"):
        up = client.post("/admin/api/companies/acme-beta/files", headers=WRITE,
                         data={"folder": folder}, files={"file": ("lease.pdf", lease, "application/pdf")})
        assert up.json()["status"] == "stored", up.json()
    junk = client.post("/admin/api/companies/acme-beta/files", headers=WRITE,
                       data={"folder": "contracts"}, files={"file": ("setup.exe", b"MZ", "application/octet-stream")})
    assert junk.json()["status"] == "skipped"

    assert client.post("/admin/api/companies/acme-beta/ingest", headers=WRITE).status_code == 200
    assert all(j["status"] == "done" for j in wait_for_jobs(client, "acme-beta"))

    listing = client.get("/admin/api/companies/acme-beta/files", params={"path": "contracts"}).json()
    assert [f["name"] for f in listing["folders"]] == ["2024"]
    assert [(f["path"], f["state"]) for f in listing["files"]] == [("contracts/lease.pdf", "ready")]

    summary = {c["id"]: c for c in client.get("/admin/api/companies").json()["companies"]}["acme-beta"]
    assert (summary["documents"], summary["ready"], summary["failed"]) == (2, 2, 0)

    got = client.get("/admin/api/companies/acme-beta/download", params={"path": "contracts/2024/lease.pdf"})
    assert got.status_code == 200 and got.content == lease

    found = client.post("/admin/api/companies/acme-beta/retrieve", json={"query": "freight"}, headers=WRITE).json()
    assert {p["source"] for p in found["passages"]} == {"contracts/lease.pdf", "contracts/2024/lease.pdf"}

    gone = client.post("/admin/api/companies/acme-beta/trash", json={"path": "contracts/2024"}, headers=WRITE)
    assert gone.status_code == 200
    found = client.post("/admin/api/companies/acme-beta/retrieve", json={"query": "freight"}, headers=WRITE).json()
    assert {p["source"] for p in found["passages"]} == {"contracts/lease.pdf"}

    # Delete is two steps and a typed confirmation.
    assert client.delete("/admin/api/companies/acme-beta", params={"confirm": "acme-beta"}, headers=WRITE).status_code == 409
    assert client.post("/admin/api/companies/acme-beta/disable", headers=WRITE).status_code == 200
    assert client.delete("/admin/api/companies/acme-beta", params={"confirm": "nope"}, headers=WRITE).status_code == 400
    deleted = client.delete("/admin/api/companies/acme-beta", params={"confirm": "acme-beta"}, headers=WRITE)
    assert deleted.status_code == 200
    assert not (config.DATA_ROOT / "acme-beta").exists()
    assert list((config.DATA_ROOT / corpus.REMOVED).iterdir())  # moved aside, not destroyed

    connection = tenancy.connect()
    actions = [r["action"] for r in connection.execute("SELECT action FROM audit_log ORDER BY id")]
    connection.close()
    for expected in ("login", "company_created", "file_uploaded", "ingest_started", "moved_to_trash",
                     "company_disabled", "company_deleted"):
        assert expected in actions, expected


def test_the_retrieval_tester_is_the_retrieval_api(tenant_storage, operator_client, monkeypatch):
    """Same function, same answer: the tester cannot drift from what database-agent sees."""
    token = "a-service-token-for-the-website-long-enough"
    monkeypatch.setattr(config, "RETRIEVAL_TOKENS", {token: "website"})
    connection = tenancy.connect()
    tenant = tenancy.create_tenant("Same Answer", tenant_id="same-answer", connection=connection)
    tenancy.link_alias("website", "5", tenant["id"], connection=connection)
    connection.close()
    with context.use_tenant("same-answer"):
        tools.write_pdf("contracts/a.pdf", title="A", body=BODY)

    app = FastAPI()
    app.include_router(plane.router)
    body = {"query": "indemnify freight", "k": 3}
    from_plane = TestClient(app).post("/api/v1/retrieve", json=body, headers={
        "Authorization": f"Bearer {token}", "X-Syslab-Tenant": "5"}).json()
    from_admin = operator_client.post("/admin/api/companies/same-answer/retrieve", json=body, headers=WRITE).json()
    assert from_admin == from_plane


def test_every_company_route_needs_a_session(tenant_storage):
    operators.create("amro", "Amro Taha", PASSWORD)
    client = client_for(main.app)
    for method, path in (("get", "/admin/api/overview"), ("get", "/admin/api/companies"),
                         ("post", "/admin/api/companies"), ("get", "/admin/api/companies/x/files"),
                         ("post", "/admin/api/companies/x/files"), ("post", "/admin/api/companies/x/trash"),
                         ("post", "/admin/api/companies/x/retrieve"), ("delete", "/admin/api/companies/x")):
        assert getattr(client, method)(path, headers=WRITE).status_code == 401, path


def test_an_unknown_company_is_404(operator_client):
    assert operator_client.get("/admin/api/companies/nobody/files").status_code == 404
    assert operator_client.post("/admin/api/companies/nobody/trash", json={"path": "x"},
                                headers=WRITE).status_code == 404


def test_the_bootstrap_company_is_listed_but_not_deletable(tenant_storage, operator_client):
    listed = {c["id"]: c for c in operator_client.get("/admin/api/companies").json()["companies"]}
    assert listed[config.BOOTSTRAP_TENANT]["bootstrap"]
    assert operator_client.post(f"/admin/api/companies/{config.BOOTSTRAP_TENANT}/disable",
                                headers=WRITE).status_code == 400
    assert operator_client.delete(f"/admin/api/companies/{config.BOOTSTRAP_TENANT}",
                                  params={"confirm": config.BOOTSTRAP_TENANT}, headers=WRITE).status_code == 400


def test_the_overview_answers_with_nothing_running(operator_client, monkeypatch):
    monkeypatch.setattr(config, "LLM_BASE_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setattr(config, "EMBED_BASE_URL", "http://127.0.0.1:9/v1")
    data = operator_client.get("/admin/api/overview").json()
    assert data["chat"]["reachable"] is False and data["embed"]["reachable"] is False
    assert data["jobs"]["persistent"] is False
    assert data["recent"][0]["action"] == "login"
