"""Database entry points must reject unsafe paths before SQLite uses them."""
import os
import sqlite3
import stat

import pytest

from agent_evolution.environment import inspect_state_path
from agent_evolution.store import DATABASE_FILES, Store
from agent_evolution.util import EvolutionError


def test_private_database_and_sidecars_survive_reopen(tmp_path):
    old = os.umask(0o022)
    try:
        store = Store(tmp_path / "state")
        run = store.create({"synthetic": "fixture"})
        assert store.files.read_bytes(".gitignore") == b"*\n"
        assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
        for name in DATABASE_FILES:
            path = store.root / name
            if path.exists():
                assert stat.S_IMODE(path.stat().st_mode) == 0o600
        store.db.close()
        reopened = Store(store.root)
        try:
            assert reopened.get(run)["synthetic"] == "fixture"
        finally:
            reopened.db.close()
    finally:
        os.umask(old)


@pytest.mark.parametrize("name", DATABASE_FILES)
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "broad_mode", "fifo"])
def test_reject_unsafe_database_files_before_connect(tmp_path, monkeypatch, name, kind):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    target = tmp_path / "unrelated"
    target.write_bytes(b"unrelated synthetic bytes")
    target.chmod(0o600)
    path = root / name
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "broad_mode":
        path.write_bytes(b"synthetic")
        path.chmod(0o644)
    else:
        os.mkfifo(path, 0o600)
    def never_connect(*args, **kwargs):
        pytest.fail("SQLite must not open an unsafe path")
    monkeypatch.setattr(sqlite3, "connect", never_connect)
    with pytest.raises(EvolutionError):
        Store(root)
    assert target.read_bytes() == b"unrelated synthetic bytes"


def test_doctor_missing_path_creates_no_state_or_database(tmp_path):
    path = tmp_path / "not-created" / "state"
    assert inspect_state_path(path)["status"] == "passed"
    assert not (tmp_path / "not-created").exists()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("parent_mode", [0o700, 0o755])
def test_doctor_missing_path_does_not_inspect_parent_as_state(tmp_path, parent_mode):
    parent = tmp_path / "project"
    parent.mkdir(mode=parent_mode)
    (parent / ".gitignore").write_text("project-specific\n")
    (parent / "state.sqlite").write_bytes(b"unrelated project file")
    before = {p.name: p.read_bytes() for p in parent.iterdir()}
    assert inspect_state_path(parent / "new-state")["status"] == "passed"
    assert {p.name: p.read_bytes() for p in parent.iterdir()} == before


@pytest.mark.parametrize("kind", ["root_mode", "root_link", "db_mode", "db_link", "ignore"])
def test_doctor_rejects_same_unsafe_state_policy_without_repair(tmp_path, kind):
    root = tmp_path / "state"
    if kind == "root_link":
        target = tmp_path / "target"
        target.mkdir(mode=0o700)
        root.symlink_to(target, target_is_directory=True)
    else:
        root.mkdir(mode=0o700)
        if kind == "root_mode":
            root.chmod(0o755)
        elif kind == "db_mode":
            (root / "state.sqlite").write_bytes(b"synthetic")
            (root / "state.sqlite").chmod(0o644)
        elif kind == "db_link":
            (root / "state.sqlite").symlink_to(tmp_path / "missing")
        else:
            (root / ".gitignore").write_bytes(b"!keep\n")
            (root / ".gitignore").chmod(0o600)
    assert inspect_state_path(root)["status"] == "failed"
    if kind == "root_mode":
        assert stat.S_IMODE(root.stat().st_mode) == 0o755
    elif kind == "root_link":
        assert root.is_symlink()
    elif kind == "db_mode":
        assert (root / "state.sqlite").read_bytes() == b"synthetic"


def test_doctor_safe_database_is_read_only(tmp_path):
    store = Store(tmp_path / "state")
    store.create({"synthetic": "record"})
    store.db.close()
    before = {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}
    assert inspect_state_path(store.root)["status"] == "passed"
    after = {p.name: p.read_bytes() for p in store.root.iterdir() if p.is_file()}
    assert before == after
