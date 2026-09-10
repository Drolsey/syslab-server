"""Removing a tenant for good.

The other irreversible operation in this project, after the migration. Every
refusal is tested for the same reason the migration's were: the failure mode is
not "it did not work", it is "it worked on the wrong customer".
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from app import config, context, tenancy, tools

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "tenant.py"


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    """scripts/tenant.py pointed at throwaway roots, with two tenants."""
    spec = importlib.util.spec_from_file_location("tenant_cli", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["tenant_cli"] = module
    spec.loader.exec_module(module)

    data, index = tmp_path / "data", tmp_path / "index"
    derived = tmp_path / "derived"
    monkeypatch.setattr(module, "DATA_ROOT", data)
    monkeypatch.setattr(module, "INDEX_ROOT", index)
    monkeypatch.setattr(module, "DERIVED_ROOT", derived)
    monkeypatch.setattr(module, "BOOTSTRAP_TENANT", "default")
    monkeypatch.setattr(module, "_app_is_running", lambda: False)
    monkeypatch.setattr(config, "DATA_ROOT", data)
    monkeypatch.setattr(config, "INDEX_ROOT", index)
    # Writing a PDF now runs the ingestion pipeline, which creates this. Before
    # Step 2.1 it did not exist and this fixture did not need to know about it;
    # the suite's folder guard is what said so, by failing.
    monkeypatch.setattr(config, "DERIVED_ROOT", derived)

    connection = tenancy.connect()
    try:
        for name, ident in (("Default", "default"), ("Going", "going"), ("Staying", "staying")):
            tenancy.create_tenant(name, tenant_id=ident, connection=connection)
            tenancy.issue_token(ident, connection=connection)
    finally:
        connection.close()

    for ident, word in (("going", "Aardvark"), ("staying", "Bandicoot")):
        with context.use_tenant(ident):
            config.ensure_data_dir()
            tools.write_pdf(f"{ident}.pdf", title=ident, body=f"{word} is here.")

    module._data, module._index, module._derived = data, index, derived
    return module


def _files(folder: Path) -> set[str]:
    return {p.name for p in folder.iterdir()} if folder.exists() else set()


# --------------------------------------------------------------------------
# the control plane
# --------------------------------------------------------------------------

def test_an_active_tenant_is_refused(cli):
    with pytest.raises(tenancy.TenancyError, match="still active"):
        tenancy.delete_tenant("going")


def test_an_unknown_tenant_is_refused(cli):
    with pytest.raises(tenancy.TenancyError, match="No tenant"):
        tenancy.delete_tenant("nobody")


def test_deleting_removes_the_rows_and_the_access(cli):
    connection = tenancy.connect()
    try:
        token = tenancy.issue_token("going", connection=connection)
        tenancy.set_disabled("going", True, connection=connection)
        removed = tenancy.delete_tenant("going", connection=connection)

        assert removed["tokens"] == 2
        assert tenancy.get_tenant("going", connection=connection) is None
        assert tenancy.resolve_token(token, connection=connection) is None
        assert tenancy.list_tokens("going", connection=connection) == []
    finally:
        connection.close()


def test_deleting_one_tenant_leaves_the_other_entirely_alone(cli):
    connection = tenancy.connect()
    try:
        staying_token = [t for t in tenancy.list_tokens("staying", connection=connection)][0]
        tenancy.set_disabled("going", True, connection=connection)
        tenancy.delete_tenant("going", connection=connection)

        assert tenancy.get_tenant("staying", connection=connection) is not None
        assert len(tenancy.list_tokens("staying", connection=connection)) == 1
        assert tenancy.list_tokens("staying", connection=connection)[0]["fingerprint"] \
            == staying_token["fingerprint"]
    finally:
        connection.close()


# --------------------------------------------------------------------------
# the command, and everything it refuses
# --------------------------------------------------------------------------

def test_a_dry_run_changes_nothing(cli, capsys):
    assert cli.main(["delete", "going"]) == 0
    assert "dry run" in capsys.readouterr().out
    assert tenancy.get_tenant("going") is not None
    assert _files(cli._data / "going") == {"going.pdf"}


def test_a_confirm_that_does_not_match_is_refused(cli, capsys):
    tenancy.set_disabled("going", True)
    assert cli.main(["delete", "going", "--apply", "--confirm", "staying"]) == 1
    assert "REFUSING" in capsys.readouterr().out
    assert tenancy.get_tenant("going") is not None, "nothing should have happened"
    assert _files(cli._data / "going") == {"going.pdf"}


def test_an_active_tenant_is_refused_by_the_command_too(cli, capsys):
    assert cli.main(["delete", "going", "--apply", "--confirm", "going"]) == 1
    out = capsys.readouterr().out
    assert "REFUSING" in out and "still active" in out
    assert tenancy.get_tenant("going") is not None


def test_the_bootstrap_tenant_is_refused(cli, capsys):
    """Deleting it would empty the operator's own assistant."""
    tenancy.set_disabled("default", True)
    assert cli.main(["delete", "default", "--apply", "--confirm", "default"]) == 1
    out = capsys.readouterr().out
    assert "REFUSING" in out and "bootstrap tenant" in out
    assert tenancy.get_tenant("default") is not None


def test_it_is_refused_while_the_app_is_running(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_app_is_running", lambda: True)
    tenancy.set_disabled("going", True)
    assert cli.main(["delete", "going", "--apply", "--confirm", "going"]) == 1
    assert "listening on the app's port" in capsys.readouterr().out
    assert tenancy.get_tenant("going") is not None


# --------------------------------------------------------------------------
# what it actually does
# --------------------------------------------------------------------------

def test_the_documents_are_moved_aside_not_destroyed(cli, capsys):
    tenancy.set_disabled("going", True)
    assert cli.main(["delete", "going", "--apply", "--confirm", "going"]) == 0

    assert tenancy.get_tenant("going") is None
    assert not (cli._data / "going").exists()
    assert not (cli._index / "going.sqlite3").exists()

    kept = list((cli._data / cli.REMOVED).iterdir())
    assert len(kept) == 1 and kept[0].name.startswith("going-")
    assert _files(kept[0]) == {"going.pdf"}, "access is gone, the documents are not"


def test_the_derived_artifacts_go_even_when_the_documents_only_move(cli):
    """They hold text extracted from the customer's documents.

    Moving the documents aside is a judgement about the customer's data;
    keeping a second copy of it in a folder nothing points at any more is not
    that judgement, it is an oversight. Everything under derived/ can be made
    again from the files, so there is nothing to weigh.
    """
    assert (cli._derived / "going").exists(), "the fixture never produced anything"
    tenancy.set_disabled("going", True)
    assert cli.main(["delete", "going", "--apply", "--confirm", "going"]) == 0

    assert not (cli._derived / "going").exists()
    assert (cli._derived / "staying").exists(), "the wrong tenant's artifacts went"

    kept = list((cli._data / cli.REMOVED).iterdir())
    assert _files(kept[0]) == {"going.pdf"}, "the documents themselves still moved aside"


def test_purge_files_really_removes_them(cli):
    tenancy.set_disabled("going", True)
    assert cli.main(["delete", "going", "--apply", "--confirm", "going",
                     "--purge-files"]) == 0
    assert not (cli._data / "going").exists()
    assert not (cli._data / cli.REMOVED).exists()


def test_the_other_tenant_is_untouched_on_disk(cli):
    tenancy.set_disabled("going", True)
    cli.main(["delete", "going", "--apply", "--confirm", "going"])
    assert _files(cli._data / "staying") == {"staying.pdf"}
    assert (cli._index / "staying.sqlite3").exists()
