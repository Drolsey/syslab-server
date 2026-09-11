"""Step 4.0: the tenant bridge, and the plane dependency that crosses it.

The property under test throughout is one sentence: a foreign id must never
become a filesystem path. Everything else here is a way of asking that
question, and every test that returns 404 is asserting that the answer to an
id nothing linked is the SAME answer whatever shape the id had -- because a
different answer for a malformed one tells a stranger which of their guesses
had the right shape.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app import config, context, plane, tenancy

WEBSITE_TOKEN = "a-service-token-for-the-website-long-enough"
PARTNER_TOKEN = "a-service-token-for-somebody-else-entirely"


@pytest.fixture()
def control():
    """The throwaway control plane, at the path conftest already redirected to.

    Deliberately NOT a path of this file's choosing: app/plane.py resolves an
    alias without passing a connection, so it opens tenancy.CONTROL_PATH. A
    fixture that opened some other file would leave the dependency looking in
    an empty database and every test below asserting 404 for the wrong reason.
    """
    connection = tenancy.connect()
    yield connection
    connection.close()


@pytest.fixture()
def linked(control, monkeypatch):
    """One tenant, linked as website:42, and a second system with no links."""
    monkeypatch.setattr(
        config, "RETRIEVAL_TOKENS",
        {WEBSITE_TOKEN: "website", PARTNER_TOKEN: "partner"},
    )
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    return tenant


@pytest.fixture()
def probe():
    """A single route behind the real dependency.

    The plane has no routes of its own until 4.5. What 4.0 delivers is the
    dependency, so that is what is mounted: a route that reports which tenant
    the ContextVar ended up holding.

    raise_server_exceptions=False so a 500 arrives as a 500 rather than as a
    traceback out of the client -- the point of several tests below is the
    difference between a refusal and a crash.
    """
    app = FastAPI()

    @app.get("/api/v1/probe", dependencies=[Depends(plane.require_tenant)])
    def probe_route():
        return {"tenant": context.current_tenant()}

    return TestClient(app, raise_server_exceptions=False)


def ask(client, external_id, token=WEBSITE_TOKEN, headers=None):
    sent = {"Authorization": f"Bearer {token}"}
    if external_id is not None:
        sent["X-Syslab-Tenant"] = external_id
    sent.update(headers or {})
    return client.get("/api/v1/probe", headers=sent)


# --------------------------------------------------------------------------
# the bridge itself
# --------------------------------------------------------------------------

def test_a_linked_id_resolves_to_our_tenant(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    found = tenancy.resolve_alias("website", "42", connection=control)
    assert found["id"] == tenant["id"]


def test_the_local_id_is_ours_and_not_theirs(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    # The whole point of the table: their id and ours are different strings,
    # and ours is the only one that can become a folder name.
    assert tenant["id"] != "42"
    context.validate_tenant_id(tenant["id"])


def test_every_generated_id_satisfies_the_rule_that_makes_it_a_path(control):
    """A generated local id must pass the check every path applies to it.

    This is the bug that made the two-systems test below fail, and it is worth
    its own test with its own name. new_id picked its first character from an
    alphabet holding eight digits, while context.VALID_TENANT_ID requires an id
    to start with a letter -- so roughly one generated tenant in four was
    stored by create_tenant and then refused by context.set_tenant on its
    first request, permanently. Nothing noticed because every tenant that
    exists was created with an explicit id.

    200 samples: at the old 26% failure rate, the chance of this passing by
    accident is about 10^-27.
    """
    for _ in range(200):
        context.validate_tenant_id(tenancy.new_id(control))


def test_a_generated_id_is_checked_and_not_merely_trusted(control, monkeypatch):
    """create_tenant validates what it generated, not only what it was given.

    The check used to live in the `else` branch, so the one id in the system
    nobody checked was the one the system made itself.
    """
    monkeypatch.setattr(tenancy, "new_id", lambda connection=None: "9nope")
    with pytest.raises(tenancy.TenancyError):
        tenancy.create_tenant("Acme Ltd", connection=control)


def test_the_same_id_in_two_systems_is_two_tenants(control):
    one = tenancy.create_tenant("Acme Ltd", connection=control)
    two = tenancy.create_tenant("Beta Ltd", connection=control)
    tenancy.link_alias("website", "42", one["id"], connection=control)
    tenancy.link_alias("partner", "42", two["id"], connection=control)
    assert tenancy.resolve_alias("website", "42", connection=control)["id"] == one["id"]
    assert tenancy.resolve_alias("partner", "42", connection=control)["id"] == two["id"]


@pytest.mark.parametrize("external_id", [
    "../../etc/passwd",
    "..\\..\\windows\\system32",
    "Acme Ltd",
    "C:/Windows",
    "",
    "   ",
    "x" * 200,
    "with\nnewline",
    "nul\x00byte",
    None,
    42,
])
def test_an_unlinked_id_of_any_shape_is_nothing(control, external_id):
    assert tenancy.resolve_alias("website", external_id, connection=control) is None


def test_an_unknown_system_resolves_to_nothing(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    assert tenancy.resolve_alias("nobody", "42", connection=control) is None
    # And a system name that could not be one is refused the same way, rather
    # than raising into a request handler.
    assert tenancy.resolve_alias("../etc", "42", connection=control) is None


def test_an_id_is_not_improved_on_the_way_in(control):
    """Linked as 'A1', resolved as 'A1', and 'a1' is a different customer."""
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "A1", tenant["id"], connection=control)
    assert tenancy.resolve_alias("website", "A1", connection=control)["id"] == tenant["id"]
    assert tenancy.resolve_alias("website", "a1", connection=control) is None


def test_a_link_will_not_quietly_move(control):
    one = tenancy.create_tenant("Acme Ltd", connection=control)
    two = tenancy.create_tenant("Beta Ltd", connection=control)
    tenancy.link_alias("website", "42", one["id"], connection=control)
    with pytest.raises(tenancy.TenancyError):
        tenancy.link_alias("website", "42", two["id"], connection=control)
    # Still the first one. Re-pointing is unlink-then-link, deliberately.
    assert tenancy.resolve_alias("website", "42", connection=control)["id"] == one["id"]


def test_unlinking_leaves_the_tenant_alone(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    assert tenancy.unlink_alias("website", "42", connection=control) is True
    assert tenancy.resolve_alias("website", "42", connection=control) is None
    assert tenancy.get_tenant(tenant["id"], connection=control) is not None
    assert tenancy.unlink_alias("website", "42", connection=control) is False


def test_a_disabled_tenants_alias_stops_resolving(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    tenancy.set_disabled(tenant["id"], True, connection=control)
    assert tenancy.resolve_alias("website", "42", connection=control) is None
    tenancy.set_disabled(tenant["id"], False, connection=control)
    assert tenancy.resolve_alias("website", "42", connection=control) is not None


def test_deleting_a_tenant_takes_its_aliases(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    tenancy.link_alias("website", "42", tenant["id"], connection=control)
    tenancy.set_disabled(tenant["id"], True, connection=control)
    tenancy.delete_tenant(tenant["id"], connection=control)
    # An orphaned row is the one failure here that looks like a working system:
    # a foreign id resolving to a tenant that no longer exists.
    assert tenancy.list_aliases(tenant["id"], connection=control) == []
    assert tenancy.resolve_alias("website", "42", connection=control) is None


def test_a_link_needs_a_tenant_that_exists(control):
    with pytest.raises(tenancy.TenancyError):
        tenancy.link_alias("website", "42", "nosuchtenant", connection=control)


# --------------------------------------------------------------------------
# the dependency
# --------------------------------------------------------------------------

def test_the_plane_resolves_a_foreign_id_to_our_tenant(probe, linked):
    response = ask(probe, "42")
    assert response.status_code == 200
    assert response.json()["tenant"] == linked["id"]


@pytest.mark.parametrize("external_id", [
    "9999", "../../etc/passwd", "Acme Ltd", "x" * 500,
])
def test_an_unlinked_id_is_404_and_never_403(probe, linked, external_id):
    assert ask(probe, external_id).status_code == 404


def test_one_systems_token_cannot_reach_another_systems_id(probe, linked):
    assert ask(probe, "42", token=PARTNER_TOKEN).status_code == 404


def test_a_caller_cannot_name_its_own_system(probe, linked):
    """The system comes from the token. A header that could override it would
    make the (system, id) key decoration."""
    spoofed = ask(probe, "42", token=PARTNER_TOKEN,
                  headers={"X-Syslab-System": "website"})
    assert spoofed.status_code == 404


def test_an_unknown_service_token_is_401(probe, linked):
    assert ask(probe, "42", token="not-a-service-token").status_code == 401


def test_a_non_ascii_bearer_token_is_401_and_not_a_crash(probe, linked):
    """hmac.compare_digest RAISES on a str holding non-ASCII, so one accented
    letter used to be a 500 reached by a caller who had not authenticated.
    Sent as raw bytes because that is what a header is on the wire."""
    response = probe.get("/api/v1/probe", headers={
        "Authorization": b"Bearer \xfcnicode-token",
        "X-Syslab-Tenant": "42",
    })
    assert response.status_code == 401


def test_no_tenant_header_is_400(probe, linked):
    assert ask(probe, None).status_code == 400
    assert ask(probe, "   ").status_code == 400


def test_with_no_service_tokens_the_plane_serves_nobody(probe, linked, monkeypatch):
    monkeypatch.setattr(config, "RETRIEVAL_TOKENS", {})
    assert ask(probe, "42").status_code == 503


def test_the_tenant_does_not_survive_the_request(probe, linked):
    ask(probe, "42")
    assert context.tenant_if_set() is None


def test_config_and_tenancy_agree_on_what_a_system_name_is():
    """Two regexes, one rule. config cannot import tenancy -- tenancy imports
    config -- so the shape is written twice and asserted equal here."""
    assert config._VALID_SYSTEM.pattern == tenancy.VALID_EXTERNAL_SYSTEM.pattern


@pytest.mark.parametrize("raw, expected", [
    ("website:aaaaaaaaaaaaaaaaaaaa", {"aaaaaaaaaaaaaaaaaaaa": "website"}),
    ("  website : aaaaaaaaaaaaaaaaaaaa  ", {"aaaaaaaaaaaaaaaaaaaa": "website"}),
    ("website:short", {}),                      # under MIN_TOKEN_LENGTH
    ("nocolonatall", {}),
    ("Website:aaaaaaaaaaaaaaaaaaaa", {}),       # capitals are not a system name
    ("../etc:aaaaaaaaaaaaaaaaaaaa", {}),
    (":aaaaaaaaaaaaaaaaaaaa", {}),
    ("website:", {}),
    ("", {}),
])
def test_retrieval_tokens_parse_fails_closed(raw, expected):
    assert config._retrieval_tokens(raw) == expected
