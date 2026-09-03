"""The control plane. Every test works on a throwaway file, never control/."""

from __future__ import annotations

import sqlite3

import pytest

from app import tenancy


@pytest.fixture()
def control(tmp_path):
    connection = tenancy.connect(tmp_path / "control.sqlite3")
    yield connection
    connection.close()


@pytest.fixture()
def path(tmp_path):
    return tmp_path / "control.sqlite3"


# --------------------------------------------------------------------------
# tenants
# --------------------------------------------------------------------------

def test_a_new_tenant_is_active_and_findable(control):
    tenant = tenancy.create_tenant("Acme Ltd", connection=control)
    assert tenant["name"] == "Acme Ltd"
    assert tenant["active"] is True
    assert tenancy.get_tenant(tenant["id"], connection=control) == tenant


def test_generated_ids_are_opaque_and_unique(control):
    first = tenancy.create_tenant("Acme Ltd", connection=control)
    second = tenancy.create_tenant("Acme Ltd", connection=control)
    assert first["id"] != second["id"]
    # The id becomes a folder name. It must not carry the customer's name.
    assert "acme" not in first["id"].lower()
    assert len(first["id"]) >= 8


def test_a_tenant_needs_a_name(control):
    with pytest.raises(tenancy.TenancyError):
        tenancy.create_tenant("   ", connection=control)


@pytest.mark.parametrize("bad", ["../secrets", "a/b", "C:\\x", "9lives", "", "UPPER",
                                 "x" * 33, "with space", ".hidden"])
def test_a_hand_given_id_that_could_escape_a_folder_is_refused(control, bad):
    with pytest.raises(tenancy.TenancyError):
        tenancy.create_tenant("Evil", tenant_id=bad, connection=control)


def test_a_duplicate_id_is_refused(control):
    tenancy.create_tenant("First", tenant_id="default", connection=control)
    with pytest.raises(tenancy.TenancyError):
        tenancy.create_tenant("Second", tenant_id="default", connection=control)


def test_listing_can_hide_disabled_tenants(control):
    live = tenancy.create_tenant("Live", connection=control)
    gone = tenancy.create_tenant("Gone", connection=control)
    tenancy.set_disabled(gone["id"], True, connection=control)

    everyone = {t["id"] for t in tenancy.list_tenants(connection=control)}
    active = {t["id"] for t in tenancy.list_tenants(include_disabled=False,
                                                    connection=control)}
    assert everyone == {live["id"], gone["id"]}
    assert active == {live["id"]}


def test_disabling_an_unknown_tenant_is_refused(control):
    with pytest.raises(tenancy.TenancyError):
        tenancy.set_disabled("nobody", True, connection=control)


# --------------------------------------------------------------------------
# tokens
# --------------------------------------------------------------------------

def test_a_token_resolves_to_its_own_tenant_and_no_other(control):
    acme = tenancy.create_tenant("Acme", connection=control)
    globex = tenancy.create_tenant("Globex", connection=control)
    acme_token = tenancy.issue_token(acme["id"], connection=control)
    globex_token = tenancy.issue_token(globex["id"], connection=control)

    assert tenancy.resolve_token(acme_token, connection=control)["id"] == acme["id"]
    assert tenancy.resolve_token(globex_token, connection=control)["id"] == globex["id"]


@pytest.mark.parametrize("rubbish", ["", "   ", "not-a-token", None, 12345, b"bytes"])
def test_a_token_that_is_not_a_token_is_refused(control, rubbish):
    tenancy.create_tenant("Acme", connection=control)
    assert tenancy.resolve_token(rubbish, connection=control) is None


def test_a_truncated_or_padded_token_is_refused(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    token = tenancy.issue_token(tenant["id"], connection=control)
    assert tenancy.resolve_token(token[:-1], connection=control) is None
    assert tenancy.resolve_token(token + "x", connection=control) is None
    assert tenancy.resolve_token(token, connection=control) is not None


def test_a_revoked_token_stops_working_and_leaves_others_alone(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    doomed = tenancy.issue_token(tenant["id"], label="old laptop", connection=control)
    kept = tenancy.issue_token(tenant["id"], label="server", connection=control)

    tenancy.revoke_token(tenancy.token_hash(doomed)[:12], connection=control)

    assert tenancy.resolve_token(doomed, connection=control) is None
    assert tenancy.resolve_token(kept, connection=control)["id"] == tenant["id"]


def test_revoking_twice_is_reported_rather_than_pretended(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    token = tenancy.issue_token(tenant["id"], connection=control)
    fingerprint = tenancy.token_hash(token)[:12]

    first = tenancy.revoke_token(fingerprint, connection=control)
    second = tenancy.revoke_token(fingerprint, connection=control)
    assert first["already_revoked"] is False
    assert second["already_revoked"] is True


def test_an_ambiguous_or_unknown_revoke_is_refused(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    tenancy.issue_token(tenant["id"], connection=control)
    # Too short to be unambiguous: refused rather than resolved to the first
    # match, because revoking the wrong customer's token is not a mistake worth
    # being helpful about.
    with pytest.raises(tenancy.TenancyError):
        tenancy.revoke_token("ab", connection=control)
    with pytest.raises(tenancy.TenancyError):
        tenancy.revoke_token("f" * 40, connection=control)


# --------------------------------------------------------------------------
# suspension
# --------------------------------------------------------------------------

def test_disabling_a_tenant_stops_its_live_tokens_and_is_reversible(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    token = tenancy.issue_token(tenant["id"], connection=control)

    tenancy.set_disabled(tenant["id"], True, connection=control)
    assert tenancy.resolve_token(token, connection=control) is None

    tenancy.set_disabled(tenant["id"], False, connection=control)
    assert tenancy.resolve_token(token, connection=control)["id"] == tenant["id"]


def test_no_token_is_issued_for_a_disabled_tenant(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    tenancy.set_disabled(tenant["id"], True, connection=control)
    with pytest.raises(tenancy.TenancyError):
        tenancy.issue_token(tenant["id"], connection=control)


def test_a_token_for_an_unknown_tenant_is_refused(control):
    with pytest.raises(tenancy.TenancyError):
        tenancy.issue_token("nobody", connection=control)


# --------------------------------------------------------------------------
# the file itself
# --------------------------------------------------------------------------

def test_the_token_never_reaches_the_disk(path):
    connection = tenancy.connect(path)
    tenant = tenancy.create_tenant("Acme", connection=connection)
    token = tenancy.issue_token(tenant["id"], connection=connection)
    connection.commit()
    connection.close()

    blob = b""
    for candidate in (path, path.with_name(path.name + "-wal")):
        if candidate.exists():
            blob += candidate.read_bytes()

    assert token.encode("utf-8") not in blob, "the control plane is a set of keys"
    assert tenancy.token_hash(token).encode("ascii") in blob, "the hash should be there"


def test_list_tokens_cannot_return_a_token(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    token = tenancy.issue_token(tenant["id"], label="laptop", connection=control)

    rows = tenancy.list_tokens(tenant["id"], connection=control)
    assert len(rows) == 1
    flattened = " ".join(str(value) for value in rows[0].values())
    assert token not in flattened
    assert rows[0]["fingerprint"] == tenancy.token_hash(token)[:12]


def test_resolving_records_that_the_token_was_used(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    token = tenancy.issue_token(tenant["id"], connection=control)
    assert tenancy.list_tokens(tenant["id"], connection=control)[0]["last_seen_at"] is None

    tenancy.resolve_token(token, connection=control)
    assert tenancy.list_tokens(tenant["id"], connection=control)[0]["last_seen_at"] is not None


def test_touch_can_be_turned_off(control):
    tenant = tenancy.create_tenant("Acme", connection=control)
    token = tenancy.issue_token(tenant["id"], connection=control)
    tenancy.resolve_token(token, connection=control, touch=False)
    assert tenancy.list_tokens(tenant["id"], connection=control)[0]["last_seen_at"] is None


def test_a_control_plane_from_a_newer_build_is_refused(path):
    connection = tenancy.connect(path)
    connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'",
                       (str(tenancy.SCHEMA_VERSION + 1),))
    connection.commit()
    connection.close()

    with pytest.raises(tenancy.TenancyError):
        tenancy.connect(path)


def test_an_unreadable_schema_version_is_refused(path):
    connection = tenancy.connect(path)
    connection.execute("UPDATE meta SET value = 'banana' WHERE key = 'schema_version'")
    connection.commit()
    connection.close()

    with pytest.raises(tenancy.TenancyError):
        tenancy.connect(path)


def test_reopening_the_same_file_keeps_everything(path):
    first = tenancy.connect(path)
    tenant = tenancy.create_tenant("Acme", connection=first)
    token = tenancy.issue_token(tenant["id"], connection=first)
    first.commit()
    first.close()

    second = tenancy.connect(path)
    try:
        assert tenancy.resolve_token(token, connection=second)["id"] == tenant["id"]
    finally:
        second.close()


def test_a_token_row_cannot_point_at_a_tenant_that_does_not_exist(control):
    # Foreign keys are on, so the store cannot end up with an orphan token that
    # would resolve to nothing halfway through a request.
    with pytest.raises((sqlite3.IntegrityError, tenancy.TenancyError)):
        control.execute(
            "INSERT INTO tokens (token_sha256, tenant_id, label, created_at, "
            "last_seen_at, revoked_at) VALUES ('deadbeef','nobody',NULL,'now',NULL,NULL)"
        )
