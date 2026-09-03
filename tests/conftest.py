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

from app import config, context, tenancy

TEST_TENANT = "testtenant"
OTHER_TENANT = "othertenant"


# Captured HERE, at import, which happens before any test runs and before
# anything can monkeypatch it. Reading config.DATA_ROOT inside the fixture
# looked equivalent and was not: fixture teardown order between two independent
# function-scoped fixtures is not specified, so the guard sometimes ran while
# the roots were still patched and dutifully compared a tmp folder with itself.
REAL_ROOTS = (config.DATA_ROOT, config.INDEX_ROOT, config.CONTROL_DIR)


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


@pytest.fixture()
def tenant_storage(tmp_path, monkeypatch):
    """Throwaway DATA_ROOT and INDEX_ROOT, and a tenant to be. Yields the
    tenant's own data folder, which is what tests used to get as DATA_DIR."""
    monkeypatch.setattr(config, "DATA_ROOT", tmp_path / "data")
    monkeypatch.setattr(config, "INDEX_ROOT", tmp_path / "index")
    # The web app decides the tenant per request in middleware and overrides
    # whatever the caller had set, which is correct in production and would
    # otherwise send every TestClient request to the bootstrap tenant while the
    # test looked in its own folder.
    monkeypatch.setattr(config, "BOOTSTRAP_TENANT", TEST_TENANT)
    with context.use_tenant(TEST_TENANT):
        yield config.ensure_data_dir()
