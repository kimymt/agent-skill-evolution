import errno
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile

import pytest

import agent_evolution.state as state_module
from agent_evolution.state import PrivateState, StateError, inspect_path, resolve_input_path, validate_fd


@pytest.fixture
def base():
    # Use a physical path: macOS /tmp itself is a symlink and must be rejected.
    folder = "/private/tmp" if sys.platform == "darwin" else "/tmp"
    with tempfile.TemporaryDirectory(prefix="ae-private-state-test-", dir=folder) as name:
        yield Path(name)


def test_normal_private_state_and_atomic_replacement(base):
    state = PrivateState(base / "state")
    state.write_bytes("runs/a/evidence.json", b"before")
    path = state.root / "runs/a/evidence.json"
    old_inode = path.stat().st_ino
    state.write_bytes("runs/a/evidence.json", "after")
    assert state.read_bytes("runs/a/evidence.json") == b"after"
    assert path.stat().st_ino != old_inode
    assert state.file_info("runs/a/evidence.json").st_nlink == 1
    for item in [state.root, *state.root.rglob("*")]:
        assert stat.S_IMODE(item.stat().st_mode) == (0o700 if item.is_dir() else 0o600)
    assert not list(path.parent.glob(".state-*"))


def test_existing_safe_file_not_truncated(base):
    state = PrivateState(base / "state")
    state.write_bytes("db", b"preserve")
    before = state.file_info("db")
    assert state.ensure_file("db") == state.root / "db"
    assert state.file_info("db").st_ino == before.st_ino
    assert state.read_bytes("db") == b"preserve"


def test_absence_and_probe_do_not_create_state(base):
    missing = base / "missing" / "deeper"
    with pytest.raises(FileNotFoundError):
        PrivateState(missing, create=False)
    assert inspect_path(missing) == base
    assert not (base / "missing").exists()
    assert list(base.iterdir()) == []
    state = PrivateState(base / "state")
    assert state.read_bytes("absent/file", missing_ok=True) is None
    assert state.file_info("absent/file", missing_ok=True) is None
    with pytest.raises(FileNotFoundError):
        state.read_bytes("absent")


@pytest.mark.parametrize("mode", [0o755, 0o777, 0o770, 0o1700])
def test_existing_root_permissions_refused_without_repair(base, mode):
    root = base / "state"
    root.mkdir()
    root.chmod(mode)
    with pytest.raises(StateError, match="permissions"):
        PrivateState(root)
    assert stat.S_IMODE(root.stat().st_mode) == mode
    assert list(root.iterdir()) == []
    with pytest.raises(StateError):
        inspect_path(root)


@pytest.mark.parametrize("where", ["root", "ancestor", "nested", "leaf"])
def test_symlink_redirect_refused_and_target_unchanged(base, where):
    outside = base / "outside"
    outside.mkdir(mode=0o700)
    (outside / "value").write_bytes(b"preserve")
    (outside / "value").chmod(0o600)
    if where == "root":
        (base / "state").symlink_to(outside, target_is_directory=True)
        action = lambda: PrivateState(base / "state")
    elif where == "ancestor":
        (base / "alias").symlink_to(outside, target_is_directory=True)
        action = lambda: PrivateState(base / "alias" / "state")
    else:
        state = PrivateState(base / "state")
        if where == "nested":
            (state.root / "nested").symlink_to(outside, target_is_directory=True)
            action = lambda: state.write_bytes("nested/value", b"bad")
        else:
            (state.root / "value").symlink_to(outside / "value")
            action = lambda: state.write_bytes("value", b"bad")
    with pytest.raises(StateError):
        action()
    assert (outside / "value").read_bytes() == b"preserve"
    assert not (outside / "state").exists()


@pytest.mark.parametrize("operation", ["read", "write", "info", "ensure", "lock"])
def test_file_hardlink_refused_for_every_access(base, operation):
    state = PrivateState(base / "state")
    state.write_bytes("value", b"preserve")
    outside = base / "alias"
    os.link(state.root / "value", outside)
    with pytest.raises(StateError):
        if operation == "read":
            state.read_bytes("value")
        elif operation == "write":
            state.write_bytes("value", b"bad")
        elif operation == "info":
            state.file_info("value")
        elif operation == "ensure":
            state.ensure_file("value")
        else:
            with state.lock("value"):
                pytest.fail("hardlinked lock accepted")
    assert outside.read_bytes() == b"preserve"


@pytest.mark.parametrize("kind", ["fifo", "directory", "permissive_file", "dangling_symlink"])
def test_nonprivate_leaf_refused_without_blocking(base, kind):
    state = PrivateState(base / "state")
    path = state.root / "value"
    if kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "directory":
        path.mkdir(mode=0o700)
    elif kind == "dangling_symlink":
        path.symlink_to(base / "missing")
    else:
        path.write_bytes(b"preserve")
        path.chmod(0o644)
    with pytest.raises(StateError):
        state.read_bytes("value", missing_ok=True)
    with pytest.raises(StateError):
        state.write_bytes("value", b"bad")


def test_unsafe_ancestor_and_internal_directory_refused(base):
    parent = base / "shared"
    parent.mkdir()
    parent.chmod(0o777)
    with pytest.raises(StateError):
        PrivateState(parent / "state")
    assert not (parent / "state").exists()
    parent.chmod(0o1755)  # Sticky alone does not qualify as the standard tmp exception.
    state = PrivateState(parent / "state")
    state.ensure_dir("nested").chmod(0o755)
    with pytest.raises(StateError):
        state.write_bytes("nested/value", b"bad")
    assert not (state.root / "nested/value").exists()


@pytest.mark.parametrize("value", ["../escape", "/absolute", "a/../escape", "a//b", "a\\b", "x\x00y"])
def test_invalid_relative_names(base, value):
    state = PrivateState(base / "state")
    with pytest.raises(StateError):
        state.write_bytes(value, "bad")


def test_root_parent_traversal_not_normalized_away(base):
    with pytest.raises(StateError):
        PrivateState(base / "not-there" / ".." / "state")


def test_maximum_read_is_enforced(base):
    state = PrivateState(base / "state")
    state.write_bytes("value", b"123")
    assert state.read_bytes("value", max_bytes=3) == b"123"
    with pytest.raises(StateError) as caught:
        state.read_bytes("value", max_bytes=2)
    assert caught.value.code == "E_STATE_CORRUPT"


def test_lock_content_preserved_and_parallel_lock_denied(base):
    state = PrivateState(base / "state")
    state.write_bytes("operation.lock", b"preserve")
    with state.lock("operation.lock"):
        with pytest.raises(StateError) as caught:
            with PrivateState(state.root).lock("operation.lock"):
                pytest.fail("second lock acquired")
        assert caught.value.code == "E_BUSY"
    with state.lock("operation.lock", blocking=True):
        assert state.read_bytes("operation.lock") == b"preserve"


@pytest.mark.parametrize("content", [b"*", b"*\n"])
def test_existing_exact_ignore_preserved(base, content):
    state = PrivateState(base / "state")
    state.write_bytes(".gitignore", content)
    inode = state.file_info(".gitignore").st_ino
    state.ensure_ignore()
    assert state.read_bytes(".gitignore") == content
    assert state.file_info(".gitignore").st_ino == inode


def test_ignore_created_and_wrong_content_never_overwritten(base):
    state = PrivateState(base / "state")
    state.ensure_ignore()
    assert state.read_bytes(".gitignore") == b"*\n"
    state.write_bytes(".gitignore", b"x")
    with pytest.raises(StateError) as caught:
        state.ensure_ignore()
    assert caught.value.code == "E_STATE_CORRUPT"
    assert state.read_bytes(".gitignore") == b"x"


def test_owner_mismatch_rejected_without_chown(base, monkeypatch):
    state = PrivateState(base / "state")
    state.write_bytes("value", b"data")
    fd = os.open(state.root / "value", os.O_RDONLY)
    real = os.fstat(fd)
    wrong = list(real)
    wrong[4] = real.st_uid + 1
    monkeypatch.setattr(state_module.os, "fstat", lambda _fd: os.stat_result(wrong))
    try:
        with pytest.raises(StateError) as caught:
            validate_fd(fd, directory=False)
        assert caught.value.code == "E_PERMISSION"
    finally:
        os.close(fd)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL integration")
def test_real_darwin_deny_ancestor_allowed_and_private_acl_refused(base):
    parent = base / "parent"
    parent.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(parent)], check=True)
    try:
        state = PrivateState(parent / "state")
        state.write_bytes("value", b"data")
        value = state.root / "value"
        subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(value)], check=True)
        try:
            with pytest.raises(StateError):
                state.read_bytes("value")
            with pytest.raises(StateError):
                state.write_bytes("value", b"bad")
        finally:
            subprocess.run(["/bin/chmod", "-N", str(value)], check=True)
        subprocess.run(["/bin/chmod", "+a", "everyone deny delete", str(state.root)], check=True)
        try:
            with pytest.raises(StateError):
                PrivateState(state.root)
        finally:
            subprocess.run(["/bin/chmod", "-N", str(state.root)], check=True)
    finally:
        subprocess.run(["/bin/chmod", "-N", str(parent)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL integration")
def test_real_darwin_grant_on_ancestor_refused(base):
    parent = base / "parent"
    parent.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a", "everyone allow read", str(parent)], check=True)
    try:
        with pytest.raises(StateError):
            PrivateState(parent / "state")
        assert not (parent / "state").exists()
    finally:
        subprocess.run(["/bin/chmod", "-N", str(parent)], check=True)


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin ACL integration")
def test_real_darwin_grant_after_deny_is_not_missed(base):
    parent = base / "parent"
    parent.mkdir(mode=0o700)
    subprocess.run(["/bin/chmod", "+a#", "0", "everyone deny delete", str(parent)], check=True)
    try:
        subprocess.run(["/bin/chmod", "+a#", "1", "everyone allow read", str(parent)], check=True)
        with pytest.raises(StateError):
            PrivateState(parent / "state")
        assert not (parent / "state").exists()
    finally:
        subprocess.run(["/bin/chmod", "-N", str(parent)], check=True)


def test_readonly_filesystem_has_permission_diagnostic():
    with pytest.raises(StateError) as caught:
        with state_module._errors():
            raise OSError(errno.EROFS, "synthetic filesystem detail")
    assert caught.value.code == "E_PERMISSION"
    assert "synthetic" not in str(caught.value)


def test_acl_query_error_refuses_instead_of_assuming_absence(base, monkeypatch):
    state = PrivateState(base / "state")
    state.ensure_file("value")
    fd = os.open(state.root / "value", os.O_RDONLY)
    monkeypatch.setattr(state_module.sys, "platform", "linux")
    monkeypatch.setattr(state_module.os, "listxattr", lambda fd: [], raising=False)
    def failure(*args):
        raise OSError(errno.EOPNOTSUPP, "synthetic private diagnostic")
    monkeypatch.setattr(state_module.os, "getxattr", failure, raising=False)
    try:
        with pytest.raises(StateError) as caught:
            validate_fd(fd, directory=False)
        assert caught.value.code == "E_PERMISSION"
        assert "synthetic" not in str(caught.value)
    finally:
        os.close(fd)


@pytest.mark.parametrize("attribute", ["system.posix_acl_access", "system.posix_acl_default", "system.nfs4_acl"])
def test_linux_acl_representations_refused(base, monkeypatch, attribute):
    state = PrivateState(base / "state")
    state.ensure_file("value")
    fd = os.open(state.root / "value", os.O_RDONLY)
    monkeypatch.setattr(state_module.sys, "platform", "linux")
    monkeypatch.setattr(state_module.os, "listxattr", lambda fd: [attribute], raising=False)
    def query(_fd, name):
        if name == attribute:
            return b"synthetic ACL"
        raise OSError(errno.ENODATA, "absent")
    monkeypatch.setattr(state_module.os, "getxattr", query, raising=False)
    try:
        with pytest.raises(StateError):
            validate_fd(fd, directory=False)
    finally:
        os.close(fd)


@pytest.mark.parametrize("relative_target", [False, True])
def test_external_alias_to_safe_state_input_is_preserved(base, relative_target):
    state = PrivateState(base / "state")
    state.write_bytes("inputs/value", b"safe input")
    alias = base / "alias"
    alias.symlink_to("state/inputs/value" if relative_target else state.root / "inputs/value")
    resolved = resolve_input_path(state.root, alias)
    assert resolved == state.root / "inputs/value"
    assert state.read_bytes(resolved.relative_to(state.root)) == b"safe input"


@pytest.mark.parametrize("relative_target", [False, True])
def test_external_alias_cannot_hide_state_internal_link(base, relative_target):
    state = PrivateState(base / "state")
    state.ensure_dir("inputs")
    outside = base / "outside"
    outside.write_bytes(b"must not be read through the state link")
    (state.root / "inputs/link").symlink_to(outside)
    alias = base / "alias"
    alias.symlink_to("state/inputs/link" if relative_target else state.root / "inputs/link")
    resolved = resolve_input_path(state.root, alias)
    assert resolved == state.root / "inputs/link"
    with pytest.raises(StateError):
        state.read_bytes(resolved.relative_to(state.root))
    with pytest.raises(StateError):
        state.write_bytes(resolved.relative_to(state.root), b"bad")
    assert outside.read_bytes() == b"must not be read through the state link"


@pytest.mark.parametrize("via_alias", [False, True])
def test_parent_components_after_entering_state_are_not_erased(base, via_alias):
    state = PrivateState(base / "state")
    path = state.root / "inputs" / ".." / ".." / "outside"
    if via_alias:
        alias = base / "alias"
        alias.symlink_to("state/inputs/../../outside")
        path = alias
    resolved = resolve_input_path(state.root, path)
    assert resolved == state.root / "inputs" / ".." / ".." / "outside"
    with pytest.raises(StateError):
        state.read_bytes(resolved.relative_to(state.root), missing_ok=True)


def test_external_shared_skill_alias_and_external_parent_navigation(base):
    state = PrivateState(base / "state")
    external = base / "external"
    external.mkdir()
    (external / "skill").mkdir()
    (base / "alias").symlink_to("external/skill", target_is_directory=True)
    assert resolve_input_path(state.root, base / "alias") == external / "skill"
    assert resolve_input_path(state.root, external / ".." / "state" / "missing") == state.root / "missing"


def test_external_alias_loop_is_bounded(base):
    state = PrivateState(base / "state")
    (base / "first").symlink_to("second")
    (base / "second").symlink_to("first")
    with pytest.raises(StateError) as caught:
        resolve_input_path(state.root, base / "first")
    assert caught.value.code == "E_UNSAFE_PATH"


def test_relative_input_path_uses_working_directory(base, monkeypatch):
    state = PrivateState(base / "state")
    (base / "alias").symlink_to("state/missing")
    monkeypatch.chdir(base)
    assert resolve_input_path(state.root, Path("alias")) == state.root / "missing"
