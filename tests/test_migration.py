"""The one sub-step that moves real files.

Everything else in Step 1 can be reverted with git. This cannot, so the count
check, the refusals and the undo path are tested on a fake tree before they are
pointed at anybody's documents.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from app import context

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "migrate_to_tenants.py"


@pytest.fixture()
def migrate(tmp_path, monkeypatch):
    """The migration script, pointed at a throwaway tree."""
    spec = importlib.util.spec_from_file_location("migrate_to_tenants", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["migrate_to_tenants"] = module
    spec.loader.exec_module(module)

    data = tmp_path / "data"
    (data / "sub").mkdir(parents=True)
    for name in ("one.pdf", "two.xlsx"):
        (data / name).write_bytes(b"x" * 32)
    (data / "sub" / "three.pdf").write_bytes(b"y" * 32)
    (data / ".gitkeep").write_text("")

    monkeypatch.setattr(module, "DATA_ROOT", data)
    monkeypatch.setattr(module, "INDEX_ROOT", tmp_path / "index")
    monkeypatch.setattr(module, "CONTROL_DIR", tmp_path / "control")
    monkeypatch.setattr(module, "ensure_control_dir",
                        lambda: (tmp_path / "control").mkdir(parents=True, exist_ok=True))
    monkeypatch.setattr(module, "app_is_running", lambda: False)
    # The control plane and the index are exercised in their own tests; here
    # they would only add IO to a test about moving files.
    monkeypatch.setattr(module.tenancy, "connect", lambda *a, **k: _NullConnection())
    monkeypatch.setattr(module.tenancy, "get_tenant", lambda *a, **k: {"id": "default"})
    monkeypatch.setattr(module.search, "rebuild", lambda *a, **k: {"indexed": 3})

    module._data = data
    module._tmp = tmp_path
    return module


class _NullConnection:
    def close(self):
        pass


def test_a_dry_run_changes_nothing(migrate, capsys):
    before = sorted(p.name for p in migrate._data.iterdir())
    assert migrate.main([]) == 0
    assert sorted(p.name for p in migrate._data.iterdir()) == before
    assert "Nothing has changed" in capsys.readouterr().out


def test_apply_moves_everything_including_subfolders(migrate):
    assert migrate.main(["--apply"]) == 0
    target = migrate._data / "default"
    assert (target / "one.pdf").is_file()
    assert (target / "two.xlsx").is_file()
    assert (target / "sub" / "three.pdf").is_file()
    # .gitkeep is scaffolding, not a document, and stays where it is.
    assert (migrate._data / ".gitkeep").is_file()
    assert not (migrate._data / "one.pdf").exists()


def test_the_file_count_is_the_same_afterwards(migrate, capsys):
    before = migrate.count_files(migrate._data)
    migrate.main(["--apply"])
    assert migrate.count_files(migrate._data) == before
    assert "every file is accounted for" in capsys.readouterr().out


def test_a_manifest_records_every_move(migrate):
    migrate.main(["--apply"])
    manifest = json.loads((migrate._tmp / "control" / migrate.MANIFEST).read_text())
    assert manifest["tenant"] == "default"
    assert len(manifest["moved"]) == 3  # two files and the subfolder
    for entry in manifest["moved"]:
        assert Path(entry["to"]).exists()
        assert not Path(entry["from"]).exists()


def test_undo_puts_everything_back(migrate):
    before = sorted(p.name for p in migrate._data.iterdir())
    migrate.main(["--apply"])
    assert migrate.main(["--undo"]) == 0
    # Byte for byte where it started, and the folder it emptied is gone too, so
    # a later run does not have to reason about whether an empty tenant folder
    # means "migrated" or "not migrated".
    assert sorted(p.name for p in migrate._data.iterdir()) == before
    assert (migrate._data / "one.pdf").is_file()
    assert (migrate._data / "sub" / "three.pdf").is_file()
    assert not (migrate._data / "default").exists()


def test_it_refuses_when_the_target_already_holds_files(migrate, capsys):
    (migrate._data / "default").mkdir()
    (migrate._data / "default" / "already.pdf").write_bytes(b"z")
    assert migrate.main(["--apply"]) == 1
    assert "REFUSING" in capsys.readouterr().out
    assert (migrate._data / "one.pdf").is_file(), "nothing should have moved"


def test_it_refuses_while_the_app_is_running(migrate, monkeypatch, capsys):
    monkeypatch.setattr(migrate, "app_is_running", lambda: True)
    assert migrate.main(["--apply"]) == 1
    out = capsys.readouterr().out
    assert "REFUSING" in out and "listening on port" in out
    assert (migrate._data / "one.pdf").is_file(), "nothing should have moved"


def test_running_it_twice_is_refused_rather_than_doubled(migrate, capsys):
    assert migrate.main(["--apply"]) == 0
    assert migrate.main(["--apply"]) == 1
    assert "already holds" in capsys.readouterr().out


def test_an_unusable_tenant_name_is_refused_before_anything_moves(migrate):
    with pytest.raises(context.BadTenantError):
        migrate.main(["--apply", "--tenant", "../escape"])
    assert (migrate._data / "one.pdf").is_file()


def test_undo_without_a_manifest_says_so(migrate, capsys):
    assert migrate.main(["--undo"]) == 1
    assert "nothing recorded to undo" in capsys.readouterr().out
