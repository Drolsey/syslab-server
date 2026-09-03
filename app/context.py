"""Whose request is this?

One question, asked by every layer that touches storage, and answered without
any of them passing it to each other.

WHY AMBIENT AND NOT A PARAMETER
    agent._coerce_arguments derives each tool's required arguments from its
    signature and reports them to the model by name. A `tenant` parameter on
    read_pdf would therefore be demanded from the model, appear in its error
    messages, and become a security boundary that a small model can see and
    try to guess. Keeping the owner out of every signature keeps it out of the
    model's world entirely, and leaves app/agent.py untouched.

THE RULE THAT MAKES AMBIENT SAFE
    current_tenant() RAISES when nothing has set it. There is no default
    tenant, at any layer, ever. The one dangerous failure mode of ambient
    context is a code path that quietly falls back to a default and serves the
    wrong customer's data; making it raise converts that silent leak into a
    loud crash in a test.

    If you ever find yourself wanting a default here, the thing you actually
    want is use_tenant() around the call.

A CONTEXTVAR IS NOT INHERITED BY A THREAD YOU START
    threading.Thread begins with an empty context, so a tenant set on the
    request thread is invisible inside a worker. The job lane must set the
    context from the job record before calling a handler, and there is a test
    that fails if that stops being true. anyio's threadpool, which is how
    FastAPI runs a sync endpoint, DOES copy the caller's context, but only
    outward: what a threadpool worker sets is not visible to its caller.

    The practical consequence for the web layer: the tenant must be set
    somewhere that runs on the event loop, an async dependency or middleware,
    not in a sync dependency whose changes die with its worker thread.

This module deliberately imports nothing from the rest of the app. config
imports it, and tenancy imports config, so it has to sit at the bottom.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Iterator

# A tenant id becomes a folder name and a database filename. It is validated
# here, at the bottom of the import graph, so that both the control plane and
# anything that resolves a path apply exactly one rule.
#
# Lower-case only, because Windows would see "Acme" and "acme" as one folder
# while Linux would see two. Starts with a letter, so an id can never look
# like a number, a flag, or a dotfile.
VALID_TENANT_ID = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class NoTenantError(RuntimeError):
    """Something asked whose data this is, and nothing had said.

    This is a bug in the caller, not a condition to handle. It means a code
    path reached storage without an owner, which before tenancy existed was
    every code path, and must now be none of them.
    """


class BadTenantError(ValueError):
    """A tenant id that must never be allowed to become a path."""


_tenant: ContextVar[str | None] = ContextVar("syslab_tenant", default=None)


def validate_tenant_id(value: object) -> str:
    """Check an id before it can become a folder name. Refuse, never clamp."""
    if not isinstance(value, str):
        raise BadTenantError(f"A tenant id must be a string, not {type(value).__name__}.")
    candidate = value.strip()
    if not candidate:
        raise BadTenantError("A tenant id cannot be empty.")
    if candidate != candidate.lower():
        raise BadTenantError(
            f"{candidate!r} is not a usable tenant id: ids are lower-case, because "
            f"an id becomes a folder name and Windows would see {candidate!r} and "
            f"{candidate.lower()!r} as the same folder while Linux would see two. "
            f"Use {candidate.lower()!r}."
        )
    if not VALID_TENANT_ID.match(candidate):
        raise BadTenantError(
            f"{candidate!r} is not a usable tenant id. It becomes a folder name, so "
            "it must start with a lower-case letter and hold only letters, digits, "
            "hyphen and underscore, up to 32 characters."
        )
    return candidate


def current_tenant() -> str:
    """Whose data are we touching? Raises if nobody has said.

    Every path, index and connection in this app goes through here. If it
    raises, some code reached storage without an owner. Fix the caller; do not
    give this function a default.
    """
    tenant = _tenant.get()
    if tenant is None:
        raise NoTenantError(
            "No tenant is set for this call, so there is no way to tell whose data "
            "this is. Wrap the work in context.use_tenant(<id>). This is deliberate: "
            "there is no default tenant, because a default is how one customer's "
            "request quietly reads another customer's files."
        )
    return tenant


def tenant_if_set() -> str | None:
    """The tenant, or None, without raising.

    For logging and diagnostics ONLY. Never choose a file, an index, a
    connection or a row on the strength of this: a None here means "we do not
    know", and code that treats not knowing as a value is exactly the bug
    current_tenant() exists to prevent.
    """
    return _tenant.get()


def set_tenant(tenant_id: str) -> Token:
    """Set the tenant and return a token to undo it.

    For framework code that cannot wrap its work in a `with`, such as
    middleware. Everywhere else, prefer use_tenant().
    """
    return _tenant.set(validate_tenant_id(tenant_id))


def reset_tenant(token: Token) -> None:
    _tenant.reset(token)


@contextmanager
def use_tenant(tenant_id: str) -> Iterator[str]:
    """Run a block as one tenant, and put back whatever was there before.

    Nesting is supported and restores the outer tenant on the way out, so a
    background task that briefly acts as someone else cannot leave the request
    it was called from pointing at the wrong customer.
    """
    token = set_tenant(tenant_id)
    try:
        yield _tenant.get()
    finally:
        _tenant.reset(token)


@contextmanager
def no_tenant() -> Iterator[None]:
    """Run a block with no tenant at all, whatever was set outside it.

    Its use is in tests and gates: it makes "this code path must refuse to run
    without an owner" something you can assert, rather than something you hope
    is true because nothing happened to set a tenant.
    """
    token = _tenant.set(None)
    try:
        yield
    finally:
        _tenant.reset(token)
