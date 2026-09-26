"""GET /api/v1/documents, GET /api/v1/documents/{name}, POST /api/v1/ingest/{name}. Step 4.8.

Thin wrappers over Step 2.3 (app/tools.py, app/ingest.py, app/intake.py), so
what these tests exist to prove is narrower than 4.6's: that tenant scoping
carries into each wrapper the same way it does for /retrieve, and that the one
deliberate departure from a straight passthrough -- list_documents() dropping
`folder`, an absolute path on this server's own disk -- actually happens.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import config, context, plane, tenancy, tools

TOKEN = "a-service-token-for-the-website-long-enough"


@pytest.fixture()
def linked(tenant_storage, monkeypatch):
    """A real tenant with one real document, reachable as website:42."""
    monkeypatch.setattr(config, "RETRIEVAL_TOKENS", {TOKEN: "website"})
    connection = tenancy.connect()
    tenant = tenancy.create_tenant("Acme Ltd", connection=connection)
    tenancy.link_alias("website", "42", tenant["id"], connection=connection)
    connection.close()

    with context.use_tenant(tenant["id"]):
        tools.write_pdf("meridian_invoice.pdf", title="Invoice QT-4417",
                        body="Payment falls due thirty days from invoice date. " * 20)
    return tenant


@pytest.fixture()
def other_tenant(linked):
    """A second tenant, with its own document the first tenant must never see."""
    connection = tenancy.connect()
    tenant = tenancy.create_tenant("Thornbury LLC", connection=connection)
    tenancy.link_alias("website", "99", tenant["id"], connection=connection)
    connection.close()

    with context.use_tenant(tenant["id"]):
        tools.write_pdf("thornbury_deed.pdf", title="Deed", body="Confidential terms. " * 20)
    return tenant


@pytest.fixture()
def client():
    """The real router, mounted on a bare app -- this route's own auth is under test."""
    app = FastAPI()
    app.include_router(plane.router)
    return TestClient(app, raise_server_exceptions=False)


def headers(tenant="42", token=TOKEN):
    return {"Authorization": f"Bearer {token}", "X-Syslab-Tenant": tenant}


# --------------------------------------------------------------------------
# GET /api/v1/documents
# --------------------------------------------------------------------------

def test_lists_this_tenants_documents(linked, client):
    body = client.get("/api/v1/documents", headers=headers()).json()
    assert body["count"] == 1
    assert body["documents"][0]["name"] == "meridian_invoice.pdf"


def test_the_response_has_exactly_the_documented_fields(linked, client):
    body = client.get("/api/v1/documents", headers=headers()).json()
    assert set(body) == {"count", "documents"}
    assert set(body["documents"][0]) == {"name", "size_kb", "modified", "read_with"}


def test_the_servers_own_filesystem_path_never_crosses_the_wire(linked, client):
    """The one deliberate departure from tools.list_files()'s own shape.

    /api/files can say where on this box a file lives; /api/v1 is the frozen
    surface a customer's own website relies on, and that is not this tenant's
    business to know or this server's business to tell them.
    """
    body = client.get("/api/v1/documents", headers=headers()).json()
    assert "folder" not in body
    dumped = str(body)
    assert str(config.DATA_ROOT) not in dumped


def test_a_tenant_never_sees_another_tenants_documents(other_tenant, client):
    body = client.get("/api/v1/documents", headers=headers(tenant="42")).json()
    names = {doc["name"] for doc in body["documents"]}
    assert names == {"meridian_invoice.pdf"}

    body = client.get("/api/v1/documents", headers=headers(tenant="99")).json()
    names = {doc["name"] for doc in body["documents"]}
    assert names == {"thornbury_deed.pdf"}


# --------------------------------------------------------------------------
# GET /api/v1/documents/{name}
# --------------------------------------------------------------------------

def test_reports_a_freshly_written_documents_status(linked, client):
    body = client.get("/api/v1/documents/meridian_invoice.pdf", headers=headers()).json()
    assert body["name"] == "meridian_invoice.pdf"
    assert set(body) == {"name", "ready", "producers", "missing", "stale", "failed"}


def test_reports_ready_once_ingested(linked, client):
    client.post("/api/v1/ingest/meridian_invoice.pdf", headers=headers())
    body = client.get("/api/v1/documents/meridian_invoice.pdf", headers=headers()).json()
    assert body["ready"] is True
    assert body["missing"] == []


def test_a_name_that_does_not_exist_for_this_tenant_is_404(linked, client):
    response = client.get("/api/v1/documents/nope.pdf", headers=headers())
    assert response.status_code == 404


def test_another_tenants_document_name_is_also_404_not_this_tenants_data(
    other_tenant, client
):
    """The 404 says "not here", the same for a made-up name and a real one
    that belongs to somebody else -- this module's own rule for tenant ids,
    applied to filenames for the same reason."""
    response = client.get("/api/v1/documents/thornbury_deed.pdf", headers=headers(tenant="42"))
    assert response.status_code == 404


# --------------------------------------------------------------------------
# POST /api/v1/ingest/{name}
# --------------------------------------------------------------------------

def test_ingesting_a_document_indexes_it(linked, client):
    body = client.post("/api/v1/ingest/meridian_invoice.pdf", headers=headers()).json()
    assert body["name"] == "meridian_invoice.pdf"
    assert body["indexed"] is True


def test_ingesting_leaves_the_document_ready(linked, client):
    client.post("/api/v1/ingest/meridian_invoice.pdf", headers=headers())
    status = client.get("/api/v1/documents/meridian_invoice.pdf", headers=headers()).json()
    assert status["ready"] is True


def test_a_missing_name_is_404(linked, client):
    response = client.post("/api/v1/ingest/nope.pdf", headers=headers())
    assert response.status_code == 404


def test_a_path_escaping_name_is_400_not_500(linked, client):
    response = client.post("/api/v1/ingest/../secrets.pdf", headers=headers())
    assert response.status_code in (400, 404)


def test_ingesting_another_tenants_document_by_name_is_404(other_tenant, client):
    response = client.post("/api/v1/ingest/thornbury_deed.pdf", headers=headers(tenant="42"))
    assert response.status_code == 404


# --------------------------------------------------------------------------
# CONSTRAINT: none of these routes work without a tenant, same as /retrieve
# --------------------------------------------------------------------------

@pytest.mark.parametrize("path,method", [
    ("/api/v1/documents", "get"),
    ("/api/v1/documents/meridian_invoice.pdf", "get"),
    ("/api/v1/ingest/meridian_invoice.pdf", "post"),
])
def test_no_tenant_header_is_400(linked, client, path, method):
    response = getattr(client, method)(
        path, headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 400


@pytest.mark.parametrize("path,method", [
    ("/api/v1/documents", "get"),
    ("/api/v1/documents/meridian_invoice.pdf", "get"),
    ("/api/v1/ingest/meridian_invoice.pdf", "post"),
])
def test_an_unlinked_tenant_is_404_never_403(linked, client, path, method):
    response = getattr(client, method)(path, headers=headers(tenant="does-not-exist"))
    assert response.status_code == 404
