import importlib.util
import json
from pathlib import Path
import shlex

import pytest


PROJECT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    spec = importlib.util.spec_from_file_location("observer_installer_tests", PROJECT / "scripts/install_observer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    from observer_bundle import build
    runtime = build(PROJECT, tmp_path / "builds")
    home = tmp_path / "home"
    home.mkdir()
    return module, home, runtime


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def forbidden(*args, **kwargs):
    raise AssertionError("Publication or registration writes must not begin")


def test_only_exact_owned_hooks_are_replaced_and_receipt_omits_unrelated_data(setup):
    module, home, runtime = setup
    legacy = shlex.join([str(PROJECT / ".venv/bin/python"), str(PROJECT / "scripts/observe.py"),
                         "--agent", "codex", "hook"])
    owned = {"type": "command", "command": legacy, "timeout": 10}
    unrelated = [
        {"type": "command", "command": "echo " + shlex.quote(legacy)},
        {"type": "prompt", "command": legacy},
        {"type": "command", "command": "security-review --check-all"},
    ]
    original = {"env": {"TEST_UNRELATED_PRIVATE_VALUE": "test-only-sentinel"}, "hooks": {"Stop": [
        {"hooks": [owned]},
        {"matcher": "Bash", "custom": "keep-group-metadata", "hooks": unrelated}, {"hooks": []},
    ]}}
    path = home / ".codex/hooks.json"
    write(path, json.dumps(original))
    result = module.install(home, ["codex"], runtime)
    after = json.loads(path.read_text())
    assert after["env"] == original["env"]
    assert after["hooks"]["Stop"][0] == {"matcher": "Bash", "custom": "keep-group-metadata", "hooks": unrelated}
    assert after["hooks"]["Stop"][1] == {"hooks": []}
    assert len(after["hooks"]["Stop"]) == 3
    receipt_text = Path(result[0]["receipt"]).read_text()
    receipt = json.loads(receipt_text)
    previous = receipt["changes"][0]["previous_managed"]
    assert len(previous) == 1 and previous[0]["handler"] == owned
    assert previous[0]["group"] == {}
    assert "test-only-sentinel" not in receipt_text
    assert "security-review --check-all" not in receipt_text
    assert not list(home.rglob("*.before-agent-evolution-*"))


@pytest.mark.parametrize("drift", ["matcher", "async", "timeout", "extraargv", "missingtimeout"])
def test_modified_owned_definition_aborts_all_writes_and_publication(setup, monkeypatch, drift):
    module, home, runtime = setup
    legacy = shlex.join([str(PROJECT / ".venv/bin/python"), str(PROJECT / "scripts/observe.py"),
                         "--agent", "claude", "hook"])
    handler = {"type": "command", "command": legacy, "timeout": 10}
    group = {"hooks": [handler]}
    if drift == "matcher":
        group["matcher"] = "Bash"
    elif drift == "async":
        handler["async"] = True
    elif drift == "timeout":
        handler["timeout"] = 20
    elif drift == "extraargv":
        handler["command"] += " --different-operation"
    else:
        del handler["timeout"]
    codex = home / ".codex/hooks.json"
    claude = home / ".claude/settings.json"
    codex_before = '{"hooks":{},"preserve":"original"}'
    claude_before = json.dumps({"hooks": {"Stop": [group]}})
    write(codex, codex_before)
    write(claude, claude_before)
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    with pytest.raises(ValueError, match="Modified observer"):
        module.install(home, ["codex", "claude"], runtime)
    assert codex.read_text() == codex_before
    assert claude.read_text() == claude_before
    assert not (home / ".local").exists()


@pytest.mark.parametrize("collision", ["pi", "claude"])
def test_every_collision_is_checked_before_publication_or_registration_writes(setup, monkeypatch, collision):
    module, home, runtime = setup
    codex = home / ".codex/hooks.json"
    before = '{"hooks":{},"preserve":"original"}'
    write(codex, before)
    if collision == "pi":
        conflicting = home / ".pi/agent/extensions/agent-evolution.js"
        write(conflicting, "export default function userExtension() {}\n")
    else:
        conflicting = home / ".claude/settings.json"
        write(conflicting, "not valid json")
    original = conflicting.read_bytes()
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    with pytest.raises(ValueError):
        module.install(home, ["codex", collision], runtime)
    assert codex.read_text() == before
    assert conflicting.read_bytes() == original
    assert not (home / ".local").exists()


@pytest.mark.parametrize("position", ["home", "registration_parent", "registration_file", "runtime", "release_root"])
def test_symlinks_are_refused_before_publication(setup, tmp_path, monkeypatch, position):
    module, home, runtime = setup
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "hooks.json"
    sentinel.write_text('{"hooks":{}}')
    release_root = None
    if position == "home":
        linked = tmp_path / "linked-home"
        linked.symlink_to(home, target_is_directory=True)
        home = linked
    elif position == "registration_parent":
        (home / ".codex").symlink_to(external, target_is_directory=True)
    elif position == "registration_file":
        (home / ".codex").mkdir()
        (home / ".codex/hooks.json").symlink_to(sentinel)
    elif position == "runtime":
        linked = tmp_path / "linked-runtime"
        linked.symlink_to(runtime, target_is_directory=True)
        runtime = linked
    else:
        release_root = tmp_path / "linked-releases"
        release_root.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    with pytest.raises(ValueError, match="Symlink"):
        module.install(home, ["codex"], runtime, release_root=release_root)
    assert sentinel.read_text() == '{"hooks":{}}'
    assert list(external.iterdir()) == [sentinel]


def test_dry_run_does_not_publish_or_create_registrations(setup, monkeypatch):
    module, home, runtime = setup
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    result = module.install(home, ["codex", "claude", "pi", "opencode"], runtime, dry_run=True)
    assert len(result) == 4
    assert all(change["dry_run"] and "receipt" not in change for change in result)
    assert list(home.iterdir()) == []


def test_release_under_documents_is_rejected_without_creating_it(setup, monkeypatch):
    module, home, runtime = setup
    destination = PROJECT.parent / ".observer-test-forbidden-release-root"
    assert not destination.exists()
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    with pytest.raises(ValueError, match="outside the Documents"):
        module.install(home, ["codex"], runtime, release_root=destination)
    assert not destination.exists()
    assert list(home.iterdir()) == []


def test_partial_apply_retains_exact_progress_receipt_without_automatic_rollback(setup, monkeypatch):
    module, home, runtime = setup
    codex = home / ".codex/hooks.json"
    claude = home / ".claude/settings.json"
    codex_before = '{"hooks":{},"preserve":"codex-original"}'
    claude_before = '{"hooks":{},"preserve":"claude-original"}'
    write(codex, codex_before)
    write(claude, claude_before)
    actual_write = module.atomic_write

    def fail_second_registration(path, data, mode=0o600):
        if path == claude:
            raise OSError("simulated apply failure")
        return actual_write(path, data, mode=mode)

    monkeypatch.setattr(module, "atomic_write", fail_second_registration)
    with pytest.raises(OSError, match="simulated apply failure"):
        module.install(home, ["codex", "claude"], runtime)
    assert codex.read_text() != codex_before
    assert json.loads(codex.read_text())["preserve"] == "codex-original"
    assert claude.read_text() == claude_before
    receipts = list((home / ".local/share/agent-evolution/observer/changes").glob("*.json"))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt["status"].startswith("partial;")
    assert [entry["applied"] for entry in receipt["changes"]] == [True, False]
    assert [entry["path"] for entry in receipt["changes"]] == [str(codex), str(claude)]


def test_install_never_changes_existing_codex_trust_records(setup):
    module, home, runtime = setup
    path = home / ".codex/config.toml"
    trust = b'[hooks.state."existing-hook"]\ntrusted_hash = "sha256:unchanged-test-marker"\n'
    path.parent.mkdir()
    path.write_bytes(trust)
    result = module.install(home, ["codex", "claude", "pi", "opencode"], runtime)
    assert path.read_bytes() == trust
    assert len(result) == 4
    assert str(path) not in {entry["path"] for entry in result}


def test_plan_rejects_runtime_config_changed_after_manifest_verification(setup, monkeypatch):
    module, home, runtime = setup
    actual_read = module.read_registration
    config = runtime / "observer-runtime.json"
    original = config.read_bytes()
    changed = original.replace(b"SKILL.md", b"SKILL.py")
    assert changed != original and len(changed) == len(original)

    def changed_config(path):
        return changed if path == config else actual_read(path)

    monkeypatch.setattr(module, "read_registration", changed_config)
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    with pytest.raises(ValueError, match="configuration changed after verification"):
        module.plan(home, ["codex", "claude"], runtime)
    assert config.read_bytes() == original
    assert list(home.iterdir()) == []


def test_concurrent_change_to_last_registration_aborts_before_first_write_or_publish(setup, monkeypatch):
    module, home, runtime = setup
    codex = home / ".codex/hooks.json"
    claude = home / ".claude/settings.json"
    before = '{"hooks":{},"preserve":"original"}'
    concurrent = '{"hooks":{},"preserve":"concurrent-update"}'
    write(codex, before)
    write(claude, before)
    actual_read = module.read_registration
    claude_reads = 0

    def read_with_concurrent_update(path):
        nonlocal claude_reads
        if path == claude:
            claude_reads += 1
            if claude_reads == 2:
                claude.write_text(concurrent)
        return actual_read(path)

    monkeypatch.setattr(module, "read_registration", read_with_concurrent_update)
    monkeypatch.setattr(module, "publish", forbidden)
    monkeypatch.setattr(module, "atomic_write", forbidden)
    with pytest.raises(ValueError, match="Configuration changed while installing"):
        module.install(home, ["codex", "claude"], runtime)
    assert claude_reads == 2
    assert codex.read_text() == before
    assert claude.read_text() == concurrent
    assert not (home / ".local").exists()


def test_codex_work_home_is_registered_when_present(setup):
    module, home, runtime = setup
    (home / ".codex-work").mkdir()
    result = module.install(home, ["codex"], runtime)
    paths = {entry["path"] for entry in result}
    assert paths == {str(home / ".codex/hooks.json"), str(home / ".codex-work/hooks.json")}
    for path in paths:
        data = json.loads(Path(path).read_text())
        events = set(data["hooks"])
        assert events == {"UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"}
        for event, groups in data["hooks"].items():
            command = groups[-1]["hooks"][0]["command"]
            assert command.endswith("--agent codex hook")


def test_grok_registration_writes_dedicated_hook_file(setup):
    module, home, runtime = setup
    result = module.install(home, ["grok"], runtime)
    path = home / ".grok/hooks/agent-evolution.json"
    assert result[0]["path"] == str(path)
    data = json.loads(path.read_text())
    assert set(data["hooks"]) == {"UserPromptSubmit", "PostToolUse", "Stop", "PostToolUseFailure"}
    for groups in data["hooks"].values():
        command = groups[-1]["hooks"][0]["command"]
        assert command.endswith("--agent grok hook")
        assert groups[-1]["hooks"][0]["timeout"] == 10


def test_install_default_skips_missing_codex_work(setup):
    module, home, runtime = setup
    result = module.install(home, ["codex"], runtime)
    assert [entry["path"] for entry in result] == [str(home / ".codex/hooks.json")]
    assert not (home / ".codex-work").exists()
