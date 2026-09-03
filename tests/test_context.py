"""Whose request is this, and where does that answer survive?

The second half of this file is not really about app/context.py. It pins two
facts about the runtime that later sub-steps depend on: a plain thread does not
inherit a context, and neither does a sync FastAPI dependency hand one forward.
Both are the kind of thing that is easy to assume and expensive to assume
wrongly, so they are asserted here rather than remembered.
"""

from __future__ import annotations

import threading

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from app import context


# --------------------------------------------------------------------------
# there is no default tenant
# --------------------------------------------------------------------------

def test_asking_without_setting_raises():
    with context.no_tenant():
        with pytest.raises(context.NoTenantError):
            context.current_tenant()


def test_the_error_says_what_to_do_about_it():
    with context.no_tenant():
        with pytest.raises(context.NoTenantError) as caught:
            context.current_tenant()
    message = str(caught.value)
    assert "use_tenant" in message
    assert "no default tenant" in message


def test_tenant_if_set_gives_none_instead_of_raising():
    with context.no_tenant():
        assert context.tenant_if_set() is None
    with context.use_tenant("acme"):
        assert context.tenant_if_set() == "acme"


# --------------------------------------------------------------------------
# setting, nesting, restoring
# --------------------------------------------------------------------------

def test_use_tenant_sets_and_puts_back():
    with context.no_tenant():
        with context.use_tenant("acme"):
            assert context.current_tenant() == "acme"
        assert context.tenant_if_set() is None


def test_nesting_restores_the_outer_tenant():
    with context.use_tenant("acme"):
        with context.use_tenant("globex"):
            assert context.current_tenant() == "globex"
        # A background task that briefly acts as someone else must not leave
        # the request it was called from pointing at the wrong customer.
        assert context.current_tenant() == "acme"


def test_the_outer_tenant_survives_an_exception_inside():
    with context.use_tenant("acme"):
        with pytest.raises(ValueError):
            with context.use_tenant("globex"):
                raise ValueError("boom")
        assert context.current_tenant() == "acme"


def test_set_and_reset_for_code_that_cannot_use_with():
    with context.no_tenant():
        token = context.set_tenant("acme")
        assert context.current_tenant() == "acme"
        context.reset_tenant(token)
        assert context.tenant_if_set() is None


# --------------------------------------------------------------------------
# an id cannot become a surprising path
# --------------------------------------------------------------------------

@pytest.mark.parametrize("good", ["acme", "a", "default", "t9", "a-b_c", "x" * 32])
def test_a_usable_id_is_accepted(good):
    assert context.validate_tenant_id(good) == good


@pytest.mark.parametrize("bad", [
    "", "   ", "../secrets", "a/b", "a\\b", "C:\\x", "9lives", "-lead", "_lead",
    ".hidden", "UPPER", "MiXeD", "with space", "x" * 33, "acme!", "acme.db",
])
def test_an_unusable_id_is_refused(bad):
    with pytest.raises(context.BadTenantError):
        context.validate_tenant_id(bad)


@pytest.mark.parametrize("bad", [None, 12345, b"acme", ["acme"], object()])
def test_an_id_that_is_not_a_string_is_refused(bad):
    with pytest.raises(context.BadTenantError):
        context.validate_tenant_id(bad)


def test_a_bad_id_cannot_be_set_at_all():
    # The guard is at the point of setting, not at the point of use, so a bad
    # id never gets as far as a path.
    with pytest.raises(context.BadTenantError):
        with context.use_tenant("../secrets"):
            pass


def test_the_case_error_names_the_id_that_would_have_worked():
    with pytest.raises(context.BadTenantError) as caught:
        context.validate_tenant_id("Acme")
    assert "'acme'" in str(caught.value)


# --------------------------------------------------------------------------
# CONSTRAINT: a thread does not inherit a context
# --------------------------------------------------------------------------

def test_a_plain_thread_starts_with_no_tenant():
    """This is why the job lane must set the context from the job record.

    If this test ever fails because threads started inheriting contexts, the
    worker in app/jobs.py could stop setting it and nothing would break. Until
    then, a handler that runs without an explicit use_tenant() is running for
    nobody, and current_tenant() will say so.
    """
    seen: dict[str, str | None] = {}

    def worker():
        seen["value"] = context.tenant_if_set()

    with context.use_tenant("acme"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert context.current_tenant() == "acme", "the caller still knows"

    assert seen["value"] is None, "the worker thread does not"


def test_what_a_thread_sets_does_not_leak_back_out():
    def worker():
        context.set_tenant("globex")

    with context.use_tenant("acme"):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join()
        assert context.current_tenant() == "acme"


# --------------------------------------------------------------------------
# CONSTRAINT: where the web layer may set the tenant
# --------------------------------------------------------------------------

def _probe_app() -> FastAPI:
    app = FastAPI()

    def sync_dependency():
        context.set_tenant("syncdep")

    async def async_dependency():
        context.set_tenant("asyncdep")

    @app.get("/after-sync-dependency", dependencies=[Depends(sync_dependency)])
    def after_sync():
        return {"seen": context.tenant_if_set()}

    @app.get("/after-async-dependency", dependencies=[Depends(async_dependency)])
    def after_async():
        return {"seen": context.tenant_if_set()}

    @app.middleware("http")
    async def set_in_middleware(request: Request, call_next):
        if request.url.path == "/after-middleware":
            context.set_tenant("middleware")
        return await call_next(request)

    @app.get("/after-middleware")
    def after_middleware():
        return {"seen": context.tenant_if_set()}

    return app


def test_a_sync_dependency_cannot_hand_the_tenant_forward():
    """app/main.py's require_auth is `def`, not `async def`, today.

    FastAPI runs a sync dependency in one anyio worker thread and the sync
    endpoint in another. Context flows INTO a worker thread and never back out,
    so a tenant set in a sync dependency is gone by the time the endpoint runs.

    This is the reason sub-step 1.5 must make require_auth async, or set the
    tenant in middleware. It is asserted rather than remembered because the
    failure it prevents looks like "tenancy is completely broken" on every
    request, and the cause is four words long.
    """
    with TestClient(_probe_app()) as client:
        assert client.get("/after-sync-dependency").json() == {"seen": None}


def test_an_async_dependency_does_hand_the_tenant_forward():
    with TestClient(_probe_app()) as client:
        assert client.get("/after-async-dependency").json() == {"seen": "asyncdep"}


def test_middleware_can_hand_the_tenant_forward_too():
    with TestClient(_probe_app()) as client:
        assert client.get("/after-middleware").json() == {"seen": "middleware"}
