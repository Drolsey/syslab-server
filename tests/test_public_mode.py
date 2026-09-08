"""Step 3.3: what a stranger can reach, and what the throttle remembers.

Finding 4.6 of the architecture plan: two endpoints are safe on a tailnet and
not on the internet, and the login throttle grows without bound. Both are
fixed here, and both are the kind of fix that is only worth anything if
something fails when it is undone.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app import config, main

TEST_TOKEN = "a-test-token-long-enough-to-count"


@pytest.fixture(autouse=True)
def storage(tenant_storage, monkeypatch):
    monkeypatch.setattr(config, "APP_TOKEN", TEST_TOKEN)
    main._failures.clear()
    yield tenant_storage
    main._failures.clear()


@pytest.fixture
def stranger():
    return TestClient(main.app)


# --- the public surface ---------------------------------------------------

def test_the_admin_page_is_served_on_a_private_network(stranger, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", False)
    assert stranger.get("/").status_code == 200


def test_the_admin_page_is_gone_in_public_mode(stranger, monkeypatch):
    monkeypatch.setattr(config, "PUBLIC_MODE", True)
    response = stranger.get("/")
    # 404 rather than 401: "there is nothing here" discloses less than
    # "there is something here that needs a password".
    assert response.status_code == 404


def test_chat_still_refuses_a_stranger_either_way(stranger, monkeypatch):
    """The inference plane's door does not depend on PUBLIC_MODE at all.

    Worth asserting because it would be an easy and disastrous mistake to
    make the token check conditional on the same flag that hides the docs.
    """
    ask = {"model": "syslab-default", "messages": [{"role": "user", "content": "hi"}]}
    for public in (False, True):
        monkeypatch.setattr(config, "PUBLIC_MODE", public)
        assert stranger.post("/v1/chat/completions", json=ask).status_code in (401, 503)


# --- the throttle ---------------------------------------------------------

def test_a_failed_sign_in_is_remembered(stranger):
    stranger.post("/api/login", json={"token": "wrong"})
    assert len(main._failures) == 1


def test_a_successful_sign_in_leaves_nothing_behind(stranger):
    stranger.post("/api/login", json={"token": TEST_TOKEN})
    assert main._failures == {}


def test_failures_that_have_aged_out_are_forgotten_by_key(stranger):
    """The actual bug in finding 4.6.

    The old code pruned timestamps WITHIN a key and never removed the key, so
    the dictionary only ever grew. Age one entry past the window and the key
    itself must go, not just its contents.
    """
    stale = time.time() - main.FAILURE_WINDOW_SECONDS - 1
    main._failures["10.0.0.1"] = [stale]
    main._failures["10.0.0.2"] = [stale]

    stranger.post("/api/login", json={"token": "wrong"})

    assert "10.0.0.1" not in main._failures
    assert "10.0.0.2" not in main._failures


def test_the_throttle_is_bounded_under_a_flood(stranger, monkeypatch):
    """An attacker varying their source address must not be able to grow this
    without limit. The cap is arbitrary; being capped at all is not."""
    monkeypatch.setattr(main, "MAX_TRACKED_CLIENTS", 10)
    now = time.time()
    for n in range(500):
        main._failures[f"10.0.{n // 256}.{n % 256}"] = [now]

    stranger.post("/api/login", json={"token": "wrong"})

    assert len(main._failures) <= main.MAX_TRACKED_CLIENTS + 1


# That bounding the throttle did not cost it the job it exists to do is
# already asserted by test_api.py's lock-out test, which walks the whole
# MAX_FAILURES sequence. It is deliberately slow (the endpoint sleeps on each
# wrong token), so it is not repeated here.
