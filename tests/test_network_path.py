"""Step 3.5: who the throttle thinks you are, once there is a tunnel in front.

Putting Cloudflare Tunnel in front of this app changes something no test was
watching: every request now arrives from the cloudflared container, so
`request.client.host` is one address for the entire internet. The login
throttle keys on that. Eight wrong tokens from anyone would lock out everyone,
which converts a rate limit into a denial of service against the operator --
and it would have happened silently, on the day the tunnel went up, with
nothing in this repository failing.

The header that fixes it is only safe under a condition a header cannot state,
so the condition is a setting (TRUST_CLIENT_IP_HEADER) and these tests assert
both halves: that it works when it is on, and that it is ignored when it is
off. The second half is the one that matters. A CF-Connecting-IP that is
trusted while the port is also open is a throttle anyone evades by varying one
string, which is worse than the bug it fixes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, main

TEST_TOKEN = "a-test-token-long-enough-to-count"

# What cloudflared looks like from the app's side: one source address for
# everybody. TestClient reports "testclient" as the peer, which stands in for
# it perfectly -- the point is that it is the same string every time.
PROXY_PEER = "testclient"


@pytest.fixture(autouse=True)
def storage(tenant_storage, monkeypatch):
    monkeypatch.setattr(config, "APP_TOKEN", TEST_TOKEN)
    main._failures.clear()
    yield tenant_storage
    main._failures.clear()


@pytest.fixture
def client():
    return TestClient(main.app)


def wrong_token(client, ip: str):
    return client.post(
        "/api/login", json={"token": "wrong"}, headers={"CF-Connecting-IP": ip}
    )


# --- trusted: the header names the caller --------------------------------

def test_two_callers_behind_the_tunnel_are_counted_separately(client, monkeypatch):
    monkeypatch.setattr(config, "TRUST_CLIENT_IP_HEADER", True)

    wrong_token(client, "203.0.113.7")
    wrong_token(client, "198.51.100.4")

    assert set(main._failures) == {"203.0.113.7", "198.51.100.4"}


def test_one_caller_cannot_lock_out_another(client, monkeypatch):
    """The whole reason this setting exists.

    One address burns the entire allowance; a different address must still be
    able to sign in. Without the header this is impossible to express -- both
    are the same key.
    """
    monkeypatch.setattr(config, "TRUST_CLIENT_IP_HEADER", True)
    for _ in range(main.MAX_FAILURES):
        wrong_token(client, "203.0.113.7")

    assert wrong_token(client, "203.0.113.7").status_code == 429

    good = client.post(
        "/api/login",
        json={"token": TEST_TOKEN},
        headers={"CF-Connecting-IP": "198.51.100.4"},
    )
    assert good.status_code == 200


# --- untrusted: the header is not a way in -------------------------------

def test_the_header_is_ignored_when_nothing_is_in_front(client):
    """Default configuration. A caller who sets the header gains nothing.

    Asserted by the key that lands in the throttle, not by a status code: a
    wrong token returns 401 either way, so a status-code assertion would pass
    just as happily with the setting ignored entirely.
    """
    assert config.TRUST_CLIENT_IP_HEADER is False  # pinned by conftest

    wrong_token(client, "203.0.113.7")

    assert set(main._failures) == {PROXY_PEER}


def test_varying_the_header_does_not_evade_the_throttle(client):
    """Undo the guard and this is what you get: an attacker with an unlimited
    number of free attempts, because every guess is a fresh key."""
    for n in range(main.MAX_FAILURES):
        wrong_token(client, f"203.0.113.{n}")

    assert len(main._failures) == 1
    assert wrong_token(client, "203.0.113.250").status_code == 429


def test_an_empty_header_falls_back_rather_than_becoming_a_key(client, monkeypatch):
    """Trusted, but the header is absent or blank -- a direct request that got
    past the firewall, or a proxy misconfiguration. It must fall back to the
    peer address, never key the throttle on an empty string, which would put
    every such request in one bucket named "".
    """
    monkeypatch.setattr(config, "TRUST_CLIENT_IP_HEADER", True)

    client.post("/api/login", json={"token": "wrong"}, headers={"CF-Connecting-IP": "  "})

    assert set(main._failures) == {PROXY_PEER}


def test_a_long_header_cannot_grow_a_key_without_limit(client, monkeypatch):
    """The throttle is capped by number of keys; nothing capped the size of
    one. A header is caller-supplied, so it is truncated."""
    monkeypatch.setattr(config, "TRUST_CLIENT_IP_HEADER", True)

    wrong_token(client, "9" * 5000)

    assert all(len(key) <= 64 for key in main._failures)
