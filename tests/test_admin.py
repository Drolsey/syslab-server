"""The operator dashboard's door, Step 11.3.

Accounts, sessions and the three locks in app/admin.py: public mode, the
network guard, and the session cookie -- plus the header every write carries.
The router is mounted on the real app for the cross-surface tests, because
"the admin cookie opens nothing else" is a claim about the whole server.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import admin, config, gateway, main, operators, tenancy, throttle

LAN = "192.168.1.20"
PASSWORD = "correct horse battery"
WRITE = {"X-Syslab-Admin": "1"}


def from_address(app, host: str):
    """TestClient reports its peer as 'testclient'; the network guard needs an address."""
    async def asgi(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope, client=(host, 50000))
        await app(scope, receive, send)
    return asgi


def client_for(app, host: str = LAN) -> TestClient:
    return TestClient(from_address(app, host), raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def clean_throttle():
    throttle._failures.clear()
    yield
    throttle._failures.clear()


@pytest.fixture()
def amro():
    return operators.create("amro", "Amro Taha", PASSWORD)


@pytest.fixture()
def bare():
    app = FastAPI()
    app.include_router(admin.router)
    return app


def sign_in(client: TestClient, username: str = "amro", password: str = PASSWORD):
    return client.post("/admin/api/login", json={"username": username, "password": password},
                       headers=WRITE)


def audit_actions() -> list[str]:
    connection = tenancy.connect()
    try:
        return [r["action"] for r in connection.execute("SELECT action FROM audit_log ORDER BY id")]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

def test_a_password_round_trips_and_is_never_stored_plain():
    stored = operators.hash_password(PASSWORD)
    assert PASSWORD not in stored and stored.startswith("scrypt$")
    assert operators.check_password(PASSWORD, stored)
    assert not operators.check_password(PASSWORD + "x", stored)
    assert not operators.check_password(PASSWORD, "not-a-hash")


def test_short_passwords_and_bad_usernames_are_refused():
    with pytest.raises(operators.OperatorError):
        operators.create("amro", "A", "short")
    with pytest.raises(operators.OperatorError):
        operators.create("Robert'); DROP TABLE", "x", PASSWORD)
    with pytest.raises(operators.OperatorError):
        operators.create("9lives", "x", PASSWORD)


def test_the_public_shape_has_no_hash(amro):
    assert "password_hash" not in amro
    assert all("password_hash" not in o for o in operators.list_all())


def test_login_is_one_none_for_every_way_of_being_wrong(amro):
    assert operators.verify_login("amro", PASSWORD)["id"] == "amro"
    assert operators.verify_login("amro", "wrong password here") is None
    assert operators.verify_login("nobody", PASSWORD) is None
    assert operators.verify_login("NOT VALID!", PASSWORD) is None
    operators.set_disabled("amro", True)
    assert operators.verify_login("amro", PASSWORD) is None


def test_disabling_and_new_passwords_end_sessions(amro):
    token = operators.start_session("amro")
    assert operators.resolve_session(token)["id"] == "amro"
    operators.set_disabled("amro", True)
    assert operators.resolve_session(token) is None

    operators.set_disabled("amro", False)
    token = operators.start_session("amro")
    operators.set_password("amro", "another good password")
    assert operators.resolve_session(token) is None


def test_an_expired_session_is_refused(amro):
    token = operators.start_session("amro")
    connection = tenancy.connect()
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(timespec="seconds")
    connection.execute("UPDATE operator_sessions SET expires_at = ?", (past,))
    connection.commit()
    connection.close()
    assert operators.resolve_session(token) is None


def test_only_the_session_hash_reaches_the_disk(amro):
    token = operators.start_session("amro")
    connection = sqlite3.connect(config.CONTROL_PATH)
    dumped = "\n".join(connection.iterdump())
    connection.close()
    assert token not in dumped
    assert PASSWORD not in dumped


# --------------------------------------------------------------------------
# the network guard and public mode
# --------------------------------------------------------------------------

@pytest.mark.parametrize("host", ["127.0.0.1", "192.168.1.20", "10.1.2.3", "::1",
                                  "::ffff:192.168.1.20"])
def test_lan_and_loopback_addresses_are_allowed(host):
    assert admin.on_allowed_network(host)


@pytest.mark.parametrize("host", ["203.0.113.7", "172.17.0.2", "172.20.1.1", "testclient", "",
                                  "8.8.8.8", "2001:db8::1"])
def test_public_and_docker_addresses_are_not(host):
    assert not admin.on_allowed_network(host)


def test_a_public_address_gets_404_even_for_the_page(bare, amro):
    client = client_for(bare, "203.0.113.7")
    assert client.get("/admin").status_code == 404
    assert sign_in(client).status_code == 404


def test_the_docker_bridge_gets_404(bare, amro):
    assert client_for(bare, "172.18.0.5").get("/admin").status_code == 404


def test_public_mode_gets_404_even_from_the_lan(bare, amro, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    client = client_for(bare)
    assert client.get("/admin").status_code == 404
    assert sign_in(client).status_code == 404


def test_a_malformed_network_setting_fails_closed(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_ALLOWED_NETWORKS", ["not-a-network"])
    assert not admin.on_allowed_network("192.168.1.20")


def test_the_page_and_assets_are_served_on_the_lan(bare):
    client = client_for(bare)
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/assets/logo.png").status_code == 200
    assert client.get("/admin/assets/../../app/config.py").status_code == 404
    assert client.get("/admin/assets/index.html").status_code == 404


# --------------------------------------------------------------------------
# sessions over HTTP
# --------------------------------------------------------------------------

def test_signing_in_sets_a_scoped_strict_cookie(bare, amro):
    response = sign_in(client_for(bare))
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert f"{admin.COOKIE}=" in cookie
    assert "path=/admin" in cookie and "httponly" in cookie and "samesite=strict" in cookie


def test_me_needs_a_session(bare, amro):
    client = client_for(bare)
    assert client.get("/admin/api/me").status_code == 401
    sign_in(client)
    assert client.get("/admin/api/me").json()["operator"]["id"] == "amro"
    client.post("/admin/api/logout", headers=WRITE)
    assert client.get("/admin/api/me").status_code == 401


def test_a_write_without_the_header_is_403(bare, amro):
    client = client_for(bare)
    response = client.post("/admin/api/login", json={"username": "amro", "password": PASSWORD})
    assert response.status_code == 403
    assert client.get("/admin/api/me").status_code == 401  # and it did not sign in


def test_no_operator_yet_is_a_503_that_says_how(bare):
    response = sign_in(client_for(bare))
    assert response.status_code == 503
    assert "operator_account.py new" in response.json()["detail"]


def test_wrong_passwords_are_throttled_per_account(bare, amro):
    for n in range(throttle.MAX_FAILURES):
        # A different address each time: the account key still counts them.
        assert sign_in(client_for(bare, f"192.168.1.{100 + n}"), password="wrong wrong wrong").status_code == 401
    assert sign_in(client_for(bare, "192.168.1.250")).status_code == 429


def test_changing_the_password_needs_the_current_one(bare, amro):
    client = client_for(bare)
    sign_in(client)
    bad = client.post("/admin/api/me/password", json={"current": "nope nope nope", "new": "x" * 14},
                      headers=WRITE)
    assert bad.status_code == 400
    good = client.post("/admin/api/me/password", json={"current": PASSWORD, "new": "a brand new password"},
                       headers=WRITE)
    assert good.status_code == 200
    assert client.get("/admin/api/me").status_code == 401  # the session ended with the old password
    assert sign_in(client, password="a brand new password").status_code == 200


def test_signing_in_and_out_is_audited_without_secrets(bare, amro):
    client = client_for(bare)
    sign_in(client, password="wrong wrong wrong")
    sign_in(client)
    client.post("/admin/api/logout", headers=WRITE)
    assert audit_actions() == ["login_failed", "login", "logout"]
    connection = sqlite3.connect(config.CONTROL_PATH)
    dumped = "\n".join(connection.iterdump())
    connection.close()
    assert "wrong wrong wrong" not in dumped and PASSWORD not in dumped


# --------------------------------------------------------------------------
# the admin cookie opens nothing else
# --------------------------------------------------------------------------

def test_the_admin_session_is_not_a_credential_anywhere_else(amro, monkeypatch):
    monkeypatch.setattr(gateway, "GATEWAY_TOKENS", {"g" * 32})  # gateway holds its own copy
    monkeypatch.setattr(config, "RETRIEVAL_TOKENS", {"r" * 32: "website"})
    monkeypatch.setattr(config, "APP_TOKEN", "a" * 32)
    client = client_for(main.app)
    token = operators.start_session("amro")
    for cookie_name in (admin.COOKIE, main.TOKEN_COOKIE):
        client.cookies.set(cookie_name, token)
        assert client.get("/v1/models").status_code == 401
        assert client.get("/api/files").status_code == 401
        assert client.get("/api/v1/documents", headers={"X-Syslab-Tenant": "1"}).status_code == 401
        client.cookies.clear()
    for header in ({"Authorization": f"Bearer {token}"}, {"X-Syslab-Token": token}):
        assert client.get("/v1/models", headers=header).status_code == 401
        assert client.get("/api/files", headers=header).status_code == 401
