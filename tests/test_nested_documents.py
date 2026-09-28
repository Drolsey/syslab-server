"""Step 11.1: documents in subfolders, named by their path in the tenant's folder.

Before this, every index, manifest row and chunk id keyed on the bare filename,
so contracts/report.pdf and invoices/report.pdf were one document: the second
upload overwrote the first one's rows and the first one's passages answered
questions about the second. These tests hold the two apart, and hold the one
promise that made the change safe to ship without a migration -- a file at the
top of the folder keeps exactly the name it always had.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, context, ingest, passages, plane, producers, search, sources, tenancy, tools

ALPHA = "Alpha clause: the supplier indemnifies the buyer against freight loss. " * 30
BETA = "Beta clause: rent is reviewed every thirty six months by agreement. " * 30


@pytest.fixture()
def two_reports(tenant_storage):
    """Same filename, two folders, different contents."""
    tools.write_pdf("alpha/report.pdf", title="Alpha", body=ALPHA)
    tools.write_pdf("beta/report.pdf", title="Beta", body=BETA)
    return tenant_storage


def test_both_are_listed_by_their_path(two_reports):
    names = {f["name"] for f in tools.list_files()["files"]}
    assert names == {"alpha/report.pdf", "beta/report.pdf"}
    assert {i.id for i in sources.active().list()} == names


def test_each_has_its_own_manifest_rows_and_artifacts(two_reports):
    for name in ("alpha/report.pdf", "beta/report.pdf"):
        state = ingest.status(name)
        assert state["name"] == name
        assert state["ready"], state
    assert "freight" in producers.text_of("alpha/report.pdf")
    assert "freight" not in producers.text_of("beta/report.pdf")
    # One folder level per document under each producer, '/' escaped.
    text_folder = config.derived_dir() / producers.TEXT.name
    assert {p.name for p in text_folder.iterdir()} == {"alpha%2Freport.pdf", "beta%2Freport.pdf"}


def test_search_and_passages_cite_the_right_one(two_reports):
    hits = search.search("freight")["results"]
    assert [h["name"] for h in hits] == ["alpha/report.pdf"]

    found = passages.search_passages("rent reviewed")["results"]
    assert found and {r["source"] for r in found} == {"beta/report.pdf"}
    assert all(r["chunk_id"].startswith("beta/report.pdf#") for r in found)


def test_the_source_filter_takes_the_path(two_reports):
    found = passages.search_passages("clause", sources=["alpha/report.pdf"])["results"]
    assert found and {r["source"] for r in found} == {"alpha/report.pdf"}


def test_deleting_one_leaves_the_other(two_reports):
    (config.data_dir() / "alpha" / "report.pdf").unlink()

    swept = ingest.forget_missing()
    assert swept["gone"] == ["alpha/report.pdf"]
    assert passages.forget_missing() == ["alpha/report.pdf"]
    assert search.forget_missing() == ["alpha/report.pdf"]

    assert ingest.status("beta/report.pdf")["ready"]
    assert producers.text_of("alpha/report.pdf") is None
    assert "rent" in producers.text_of("beta/report.pdf")
    assert search.search("freight")["results"] == []
    assert {r["source"] for r in passages.search_passages("clause")["results"]} == {
        "beta/report.pdf"
    }


def test_a_top_level_file_keeps_its_bare_name(tenant_storage):
    """The backward-compatibility promise: nothing stored before 11.1 moves."""
    tools.write_pdf("invoice.pdf", title="Invoice", body=ALPHA)
    assert ingest.status("invoice.pdf")["name"] == "invoice.pdf"
    assert (config.derived_dir() / producers.TEXT.name / "invoice.pdf").is_dir()


def test_hidden_folders_and_files_are_not_documents(tenant_storage):
    tools.write_pdf("visible/ok.pdf", title="Ok", body=ALPHA)
    hidden = config.data_dir() / ".cache"
    hidden.mkdir()
    (hidden / "x.pdf").write_bytes((config.data_dir() / "visible" / "ok.pdf").read_bytes())
    (config.data_dir() / "visible" / ".y.pdf").write_bytes(b"%PDF-1.4")
    assert [f["name"] for f in tools.list_files()["files"]] == ["visible/ok.pdf"]


def test_paths_out_of_the_folder_are_still_refused(tenant_storage):
    with pytest.raises(config.UnsafePathError):
        config.resolve_in_data_dir("alpha/../../escape.pdf")
    with pytest.raises(config.UnsafePathError):
        config.doc_id(config.DATA_ROOT / "escape.pdf")
    with pytest.raises(ingest.IngestError):
        ingest.forget("alpha/../escape.pdf")


def test_derived_key_reverses_exactly():
    from urllib.parse import unquote

    for name in ("a.pdf", "a/b.pdf", "50% off/c.pdf", "odd%2Fname.pdf", "x/y/z.pdf"):
        key = config.derived_key(name)
        assert "/" not in key
        assert unquote(key) == name


def test_the_plane_routes_accept_a_path(tenant_storage, monkeypatch):
    token = "a-service-token-for-the-website-long-enough"
    monkeypatch.setattr(config, "RETRIEVAL_TOKENS", {token: "website"})
    connection = tenancy.connect()
    tenant = tenancy.create_tenant("Nested Ltd", connection=connection)
    tenancy.link_alias("website", "7", tenant["id"], connection=connection)
    connection.close()
    with context.use_tenant(tenant["id"]):
        tools.write_pdf("contracts/lease.pdf", title="Lease", body=BETA)

    app = FastAPI()
    app.include_router(plane.router)
    client = TestClient(app, raise_server_exceptions=False)
    headers = {"Authorization": f"Bearer {token}", "X-Syslab-Tenant": "7"}

    listed = client.get("/api/v1/documents", headers=headers).json()
    assert [d["name"] for d in listed["documents"]] == ["contracts/lease.pdf"]
    status = client.get("/api/v1/documents/contracts/lease.pdf", headers=headers)
    assert status.status_code == 200
    assert status.json()["name"] == "contracts/lease.pdf"
    assert client.post("/api/v1/ingest/contracts/lease.pdf", headers=headers).status_code == 200
    assert client.get("/api/v1/documents/contracts/../../x.pdf", headers=headers).status_code in {
        400, 404
    }
