import importlib.util
import json
import os
from pathlib import Path
import shutil
import stat
import sys

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/observer_bundle.py"
spec = importlib.util.spec_from_file_location("observer_bundle", SCRIPT)
bundle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bundle)


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    for name in bundle.FILES:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic runtime file: " + name + "\n")
    (root / "observer-runtime.json").write_text(bundle.canonical({
        "version": 1,
        "python": str(Path(sys._base_executable).resolve()),
        "skill": "/canonical/agent-skills/agent-evolution/SKILL.md",
    }))
    return root


def test_release_has_fixed_files_private_modes_and_stable_content(source, tmp_path):
    first = bundle.build(source, tmp_path / "builds")
    manifest = bundle.validate(first)
    assert first.name == manifest["digest"]
    assert tuple(manifest["files"]) == bundle.FILES
    assert {str(path.relative_to(first)) for path in first.rglob("*") if path.is_file()} == {*bundle.FILES, "manifest.json"}
    for path in (first, *first.rglob("*")):
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    for name in bundle.FILES:
        assert (first / name).read_bytes() == (source / name).read_bytes()
    assert bundle.build(source, tmp_path / "builds") == first
    assert list((tmp_path / "builds").iterdir()) == [first]


def test_published_snapshot_is_independent_and_idempotent(source, tmp_path):
    candidate = bundle.build(source, tmp_path / "builds")
    final = bundle.publish(candidate, tmp_path / "releases")
    before = bundle.validate(final)
    assert bundle.publish(candidate, tmp_path / "releases") == final
    assert final != candidate
    (source / "scripts/observe.py").write_text("changed mutable development source")
    shutil.rmtree(source)
    shutil.rmtree(candidate)
    assert bundle.validate(final) == before
    assert not list(final.rglob("__pycache__"))


@pytest.mark.parametrize("change", ["changed", "missing", "extra_file", "extra_directory", "wrong_mode", "wrong_digest", "wrong_name", "manifest_extra"])
def test_validate_rejects_changed_or_incomplete_releases(source, tmp_path, change):
    release = bundle.build(source, tmp_path / "builds")
    target = release / "scripts/observe.py"
    if change == "changed":
        target.write_text("changed")
    elif change == "missing":
        target.unlink()
    elif change == "extra_file":
        (release / "injected.py").write_text("injected")
    elif change == "extra_directory":
        (release / "extra").mkdir(mode=0o700)
    elif change == "wrong_mode":
        target.chmod(0o644)
    elif change == "wrong_name":
        moved = release.with_name("0" * 64)
        release.rename(moved)
        release = moved
    else:
        manifest_path = release / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        if change == "wrong_digest":
            manifest["digest"] = "0" * 64
        else:
            manifest["unapproved"] = True
        manifest_path.write_text(bundle.canonical(manifest))
    with pytest.raises((bundle.BundleError, OSError)):
        bundle.validate(release)


@pytest.mark.parametrize("position", ["file", "directory", "root", "parent"])
def test_symlinks_are_rejected_including_parent_directories(source, tmp_path, position):
    release = bundle.build(source, tmp_path / "builds")
    if position == "file":
        path = release / "scripts/observe.py"
        path.unlink()
        path.symlink_to(source / "scripts/observe.py")
    elif position == "directory":
        shutil.rmtree(release / "scripts")
        (release / "scripts").symlink_to(source / "scripts", target_is_directory=True)
    elif position == "root":
        link = tmp_path / "linked-release"
        link.symlink_to(release, target_is_directory=True)
        release = link
    else:
        link = tmp_path / "linked-parent"
        link.symlink_to(release.parent, target_is_directory=True)
        release = link / release.name
    with pytest.raises((bundle.BundleError, OSError)):
        bundle.validate(release)


def test_nonregular_source_or_bundle_is_rejected_without_reading_fifo(source, tmp_path):
    path = source / "scripts/observe.py"
    path.unlink()
    os.mkfifo(path, 0o600)
    with pytest.raises(bundle.BundleError, match="regular"):
        bundle.build(source, tmp_path / "builds")
    assert not (tmp_path / "builds").exists()


@pytest.mark.parametrize("value", [
    {"version": True, "python": "/python", "skill": "/skill"},
    {"version": 1, "python": "python3", "skill": "/skill"},
    {"version": 1, "python": "/python", "skill": "relative/SKILL.md"},
    {"version": 1, "python": "/python", "skill": "/skill/../SKILL.md"},
    {"version": 1, "python": "/python", "skill": "/skill\x00"},
    {"version": 1, "python": "/python", "skill": "/skill", "extra": True},
])
def test_invalid_runtime_config_is_rejected_before_output(source, tmp_path, value):
    (source / "observer-runtime.json").write_text(json.dumps(value))
    with pytest.raises((bundle.BundleError, OSError)):
        bundle.build(source, tmp_path / "builds")
    assert not (tmp_path / "builds").exists()


def test_runtime_python_must_be_an_executable_real_path(source, tmp_path):
    python_link = tmp_path / "python"
    python_link.symlink_to(Path(sys._base_executable).resolve())
    path = source / "observer-runtime.json"
    config = json.loads(path.read_text())
    config["python"] = str(python_link)
    path.write_text(bundle.canonical(config))
    with pytest.raises(bundle.BundleError, match="real path"):
        bundle.build(source, tmp_path / "builds")


def test_duplicate_config_keys_are_rejected(source, tmp_path):
    path = source / "observer-runtime.json"
    path.write_text('{"version":1,"version":1,"python":"/python","skill":"/skill"}')
    with pytest.raises(bundle.BundleError, match="JSON"):
        bundle.build(source, tmp_path / "builds")


def test_existing_damaged_release_is_never_overwritten(source, tmp_path):
    candidate = bundle.build(source, tmp_path / "builds")
    final = bundle.publish(candidate, tmp_path / "releases")
    target = final / "scripts/observe.py"
    target.write_text("existing user change")
    with pytest.raises(bundle.BundleError):
        bundle.publish(candidate, tmp_path / "releases")
    assert target.read_text() == "existing user change"
    assert list(final.parent.iterdir()) == [final]


@pytest.mark.parametrize("name", ["scripts/observe.py", "manifest.json", "observer-runtime.json"])
def test_existing_release_with_writable_hardlink_alias_is_rejected(source, tmp_path, name):
    candidate = bundle.build(source, tmp_path / "builds")
    final = bundle.publish(candidate, tmp_path / "releases")
    alias = tmp_path / "workspace-alias"
    os.link(final / name, alias)
    assert alias.stat().st_ino == (final / name).stat().st_ino
    with pytest.raises(bundle.BundleError, match="hardlink"):
        bundle.validate(final)
    with pytest.raises(bundle.BundleError, match="hardlink"):
        bundle.publish(candidate, final.parent)


def test_existing_empty_destination_is_never_replaced(source, tmp_path):
    candidate = bundle.build(source, tmp_path / "builds")
    final = tmp_path / "releases" / candidate.name
    final.mkdir(parents=True, mode=0o700)
    with pytest.raises(bundle.BundleError):
        bundle.publish(candidate, final.parent)
    assert final.is_dir() and list(final.iterdir()) == []


def test_concurrent_destination_creation_is_not_overwritten(source, tmp_path, monkeypatch):
    candidate = bundle.build(source, tmp_path / "builds")
    original = bundle._rename_exclusive

    def collision(parent, source_name, destination):
        os.mkdir(destination, 0o700, dir_fd=parent)
        return original(parent, source_name, destination)

    monkeypatch.setattr(bundle, "_rename_exclusive", collision)
    with pytest.raises(bundle.BundleError):
        bundle.publish(candidate, tmp_path / "releases")
    entries = list((tmp_path / "releases").iterdir())
    assert len(entries) == 1 and entries[0].name == candidate.name
    assert list(entries[0].iterdir()) == []


def test_output_parent_symlink_is_rejected_without_touching_destination(source, tmp_path):
    destination = tmp_path / "destination"
    destination.mkdir()
    link = tmp_path / "output"
    link.symlink_to(destination, target_is_directory=True)
    with pytest.raises(OSError):
        bundle.build(source, link)
    assert list(destination.iterdir()) == []


def test_build_rejects_source_symlink_and_ignores_unrelated_source_files(source, tmp_path):
    (source / "unrelated.txt").write_text("not part of runtime")
    release = bundle.build(source, tmp_path / "builds")
    assert "unrelated.txt" not in bundle.validate(release)["files"]
    path = source / "src/agent_evolution/util.py"
    path.unlink()
    path.symlink_to(source / "unrelated.txt")
    with pytest.raises(bundle.BundleError):
        bundle.build(source, tmp_path / "other-builds")


def test_cli_build_and_verify(source, tmp_path, capsys):
    assert bundle.main(["build", "--source", str(source), "--output", str(tmp_path / "builds")]) == 0
    built = json.loads(capsys.readouterr().out)
    assert bundle.main(["verify", "--bundle", built["bundle"]]) == 0
    assert json.loads(capsys.readouterr().out) == built["manifest"]


def legacy_release(source, tmp_path, monkeypatch):
    # Produce exactly the previous eight-file format without importing or
    # executing the old observer. New public build calls always use nine files.
    with monkeypatch.context() as previous:
        previous.setattr(bundle, "FILES", bundle.LEGACY_FILES)
        return bundle.build(source, tmp_path / "legacy-releases")


def test_previous_release_validates_for_registration_migration_but_cannot_be_published(source, tmp_path, monkeypatch):
    old = legacy_release(source, tmp_path, monkeypatch)
    assert tuple(bundle.validate(old)["files"]) == bundle.LEGACY_FILES
    assert not (old / "src/agent_evolution/state.py").exists()
    with pytest.raises(bundle.BundleError, match="current runtime files"):
        bundle.publish(old, tmp_path / "new-releases")
    assert not (tmp_path / "new-releases").exists()


@pytest.mark.parametrize("change", ["changed", "extra_helper", "missing"])
def test_legacy_compatibility_does_not_relax_manifest_integrity(source, tmp_path, monkeypatch, change):
    old = legacy_release(source, tmp_path, monkeypatch)
    if change == "changed":
        (old / "src/agent_evolution/observer.py").write_text("changed runtime")
    elif change == "extra_helper":
        helper = old / "src/agent_evolution/state.py"
        helper.write_text("unlisted runtime file")
        helper.chmod(0o600)
    else:
        (old / "src/agent_evolution/util.py").unlink()
    with pytest.raises(bundle.BundleError):
        bundle.validate(old)


def test_installer_recognizes_old_registration_and_plans_current_helper_release(source, tmp_path, monkeypatch):
    old = legacy_release(source, tmp_path, monkeypatch)
    candidate = bundle.build(source, tmp_path / "builds")
    spec = importlib.util.spec_from_file_location("observer_migration_installer", SCRIPT.parent / "install_observer.py")
    installer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(installer)
    home = tmp_path / "home"
    registration = home / ".codex/hooks.json"
    registration.parent.mkdir(parents=True)
    old_command = installer.command_for(old, "codex")
    registration.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": old_command, "timeout": 10},
        {"type": "command", "command": "unrelated-hook", "timeout": 1},
    ]}]}}))
    before = registration.read_bytes()
    changes = installer.plan(home, ["codex"], candidate, release_root=old.parent)
    assert len(changes) == 1
    after = json.loads(changes[0][2])
    commands = [handler["command"] for group in after["hooks"]["Stop"] for handler in group["hooks"]]
    assert "unrelated-hook" in commands
    assert old_command not in commands
    assert any(str(old.parent / candidate.name / "scripts/observe.py") in command for command in commands)
    assert registration.read_bytes() == before
    assert not (old.parent / candidate.name).exists()
