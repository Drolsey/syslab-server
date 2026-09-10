"""Shared test setup.

Every test runs as one tenant against throwaway storage roots.

Before Step 1 there was a single global data folder and each test module
patched config.DATA_DIR at it. Now there is a root per install and a folder per
tenant, so the fixture points both roots at tmp_path and enters a context.

A test that forgets the fixture does not quietly touch the real data folder,
which is what would have happened before. It raises NoTenantError. That is the
no-default rule earning its keep inside the test suite as well as in the app.
"""

from __future__ import annotations

import pytest

from app import config, context, llm, tenancy

TEST_TENANT = "testtenant"
OTHER_TENANT = "othertenant"


# Captured HERE, at import, which happens before any test runs and before
# anything can monkeypatch it. Reading config.DATA_ROOT inside the fixture
# looked equivalent and was not: fixture teardown order between two independent
# function-scoped fixtures is not specified, so the guard sometimes ran while
# the roots were still patched and dutifully compared a tmp folder with itself.
REAL_ROOTS = (config.DATA_ROOT, config.INDEX_ROOT, config.CONTROL_DIR, config.DERIVED_ROOT)


def _listing() -> dict:
    """What is in this install's own folders right now, whatever is patched."""
    seen = {}
    for root in REAL_ROOTS:
        try:
            seen[root] = sorted(p.name for p in root.iterdir())
        except OSError:
            seen[root] = []
    return seen


@pytest.fixture(autouse=True)
def the_real_folders_are_untouched():
    """The whole suite must leave this install's own folders exactly as it found them.

    Not a belt-and-braces nicety. Redirecting DATA_ROOT and INDEX_ROOT was not
    enough on its own, and neither was adding the control plane afterwards: both
    times a test wrote into the developer's real folders and NOTHING SAID SO. The
    evidence was a stray data/testtenant/ and index/testtenant.sqlite3 noticed by
    eye afterwards.

    A rule enforced by remembering is not enforced. This one runs around every
    test and fails the test that did it, whichever redirect was forgotten.
    """
    before = _listing()
    yield
    after = _listing()
    for root, names in after.items():
        appeared = sorted(set(names) - set(before.get(root, [])))
        assert not appeared, (
            f"THIS TEST created {appeared} in {root}, which is this install's real "
            "folder. It is not using the tmp_path fixtures. Per-test rather than "
            "per-session on purpose: a session-scoped check reports against "
            "whichever test happened to run last, which names the wrong one."
        )


@pytest.fixture(autouse=True)
def never_the_real_control_plane(tmp_path, monkeypatch):
    """No test may open control/control.sqlite3.

    Autouse and unconditional, because the way this goes wrong is silent. Once
    require_auth started resolving tokens through the control plane, every API
    test in the suite opened the developer's real one without anything failing
    to say so. Redirecting only the data and index roots was not enough: the
    rule is that a test touches nothing outside tmp_path, and that has to be
    enforced for every test rather than for the ones that remembered.
    """
    control = tmp_path / "control"
    monkeypatch.setattr(config, "CONTROL_DIR", control)
    monkeypatch.setattr(config, "CONTROL_PATH", control / "control.sqlite3")
    monkeypatch.setattr(tenancy, "CONTROL_PATH", control / "control.sqlite3")
    monkeypatch.setattr(tenancy, "ensure_control_dir",
                        lambda: control.mkdir(parents=True, exist_ok=True))
    yield control


@pytest.fixture(autouse=True)
def a_private_network_unless_a_test_says_otherwise(monkeypatch):
    """The network-shape settings are pinned off, whatever the developer's .env says.

    Found the way these things are found. PUBLIC_MODE arrived in Step 3.3 and
    changes what GET / returns; the operator set PUBLIC_MODE=true in .env to
    test the public surface on the real server, and two long-standing tests in
    test_api.py started failing, because config reads .env at import and the
    suite had quietly inherited it.

    The bug was never those two tests. It is that a test result depended on a
    file that is not in the repository, so the suite meant something different
    on each machine. Pinned here rather than in the tests that noticed, for the
    same reason the folder guards above are autouse: a test that has to
    remember to pin an ambient setting is a test that will forget.

    A test that is ABOUT public mode overrides this with its own monkeypatch,
    which wins for the duration of that test. See tests/test_public_mode.py.

    TRUST_CLIENT_IP_HEADER (Step 3.5) is pinned here for the same reason and
    not a different one: it decides whether a request header can name the
    client, so a suite that inherited it from .env would test a different
    throttle on the server than on a laptop.
    """
    monkeypatch.setattr(config, "PUBLIC_MODE", False)
    monkeypatch.setattr(config, "TRUST_CLIENT_IP_HEADER", False)


@pytest.fixture(autouse=True)
def no_context_window_unless_a_test_says_otherwise(monkeypatch):
    """llm.model_window answers None, instead of asking a server that may exist.

    History trimming (app/agent.py) asks the model server how big its window is
    before every model call. Left alone in the suite that is a real HTTP request
    to LLM_BASE_URL, so the tests would mean one thing on a laptop with vLLM
    running and another on a laptop without it -- the same defect the
    PUBLIC_MODE fixture above exists to stop, arriving through a socket rather
    than through .env.

    None is the honest pin: it is what the function returns when the server is
    unreachable, and app/llm.py's documented contract for None is to change
    nothing. So every test that is not about trimming sees exactly the
    behaviour it saw before trimming existed.

    A test that IS about trimming overrides this with its own monkeypatch and
    sets the window it wants. See tests/test_agent.py.
    """
    monkeypatch.setattr(llm, "model_window", lambda model: None)


@pytest.fixture()
def tenant_storage(tmp_path, monkeypatch):
    """Throwaway DATA_ROOT and INDEX_ROOT, and a tenant to be. Yields the
    tenant's own data folder, which is what tests used to get as DATA_DIR."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(config, "INDEX_ROOT", tmp_path / "index")
    # Step 2's derived artifacts. Redirected here rather than in the ingest
    # tests for the reason the guard above exists: a root that only the tests
    # which remembered redirect is a root the rest of the suite writes into.
    monkeypatch.setattr(config, "DERIVED_ROOT", tmp_path / "derived")
    # The web app decides the tenant per request in middleware and overrides
    # whatever the caller had set, which is correct in production and would
    # otherwise send every TestClient request to the bootstrap tenant while the
    # test looked in its own folder.
    monkeypatch.setattr(config, "BOOTSTRAP_TENANT", TEST_TENANT)
    with context.use_tenant(TEST_TENANT):
        yield config.ensure_data_dir()
