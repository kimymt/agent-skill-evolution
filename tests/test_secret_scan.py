"""Offline regression tests; token-shaped values are deterministic, never-issued fixtures."""
import hashlib
import json
import shlex
import os
from pathlib import Path
import shutil
import stat

import pytest

from agent_evolution import runner as runner_module, secret_scan
from agent_evolution.optimizer import ReflectionModel
from agent_evolution.process import ProcessResult
from agent_evolution.util import EvolutionError


TOKEN = "ghp_" + hashlib.sha256(b"agent-evolution-never-issued-scanner-fixture").hexdigest()[:36]
WEAK_CONFIG = '[[rules]]\nid = "nonmatching-fixture"\nregex = "NEVER_MATCHES_SYNTHETIC_INPUT_12345"\n'


@pytest.fixture
def scanner():
    found = shutil.which("gitleaks")
    if not found:
        pytest.skip("real Gitleaks is required for these offline acceptance tests")
    return str(Path(found).resolve())


@pytest.fixture
def work(tmp_path):
    result = tmp_path / "work"
    result.mkdir()
    return result


@pytest.mark.parametrize("override", ["none", "workspace_config", "env_config", "env_toml", "inline_allow"])
def test_real_scanner_rejects_secret_despite_suppression(work, tmp_path, scanner, monkeypatch, override):
    text = f"access_token = {TOKEN}"
    if override == "inline_allow":
        text += " # gitleaks:allow"
    (work / "credentials.txt").write_text(text + "\n")
    if override == "workspace_config":
        (work / ".gitleaks.toml").write_text(WEAK_CONFIG)
    elif override == "env_config":
        config = tmp_path / "external.toml"
        config.write_text(WEAK_CONFIG)
        monkeypatch.setenv("GITLEAKS_CONFIG", str(config))
    elif override == "env_toml":
        monkeypatch.setenv("GITLEAKS_CONFIG_TOML", WEAK_CONFIG)
    with pytest.raises(EvolutionError, match="details withheld") as error:
        secret_scan.scan_directory(work, scanner)
    assert TOKEN not in str(error.value)


@pytest.mark.parametrize("kind", ["file", "directory", "dangling_symlink", "fifo"])
def test_root_ignore_any_type_fails_before_scanner(work, scanner, monkeypatch, kind):
    path = work / ".gitleaksignore"
    if kind == "file":
        path.write_text("credentials.txt:github-pat:1\n")
    elif kind == "directory":
        path.mkdir()
    elif kind == "dangling_symlink":
        path.symlink_to(work / "absent")
    else:
        os.mkfifo(path)
    monkeypatch.setattr(secret_scan, "execute", lambda *args, **kwargs: pytest.fail("scanner must not run"))
    with pytest.raises(EvolutionError, match=".gitleaksignore is not permitted"):
        secret_scan.scan_directory(work, scanner)


@pytest.mark.parametrize("kind", ["file_symlink", "directory_symlink", "fifo"])
def test_special_paths_fail_before_scanner(work, tmp_path, scanner, monkeypatch, kind):
    if kind == "fifo":
        os.mkfifo(work / "special")
    else:
        outside = tmp_path / "outside"
        outside.mkdir() if kind == "directory_symlink" else outside.write_text("outside data")
        (work / "special").symlink_to(outside)
    monkeypatch.setattr(secret_scan, "execute", lambda *args, **kwargs: pytest.fail("scanner must not run"))
    with pytest.raises(EvolutionError, match="regular files and directories"):
        secret_scan.scan_directory(work, scanner)


def test_snapshot_policy_and_environment_are_private_and_fixed(work, scanner, monkeypatch):
    (work / "nested").mkdir()
    (work / "nested" / "clean.txt").write_text("public fixture data\n")
    monkeypatch.setenv("GITLEAKS_CONFIG", "/task-controlled/config")
    monkeypatch.setenv("gitleaks_extra", "task-controlled")
    real_execute = secret_scan.execute
    observed = []

    def inspect(argv, cwd, timeout, limit, **kwargs):
        target = Path(argv[2])
        observed.append(cwd)
        assert Path(argv[0]).is_absolute() and argv[0] == scanner
        assert not target.is_relative_to(work)
        assert (target / "nested" / "clean.txt").read_bytes() == (work / "nested" / "clean.txt").read_bytes()
        assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
        assert stat.S_IMODE((target / "nested" / "clean.txt").stat().st_mode) == 0o600
        assert Path(argv[argv.index("--config") + 1]).read_text() == secret_scan.POLICY
        assert Path(argv[argv.index("--gitleaks-ignore-path") + 1]).read_bytes() == b""
        assert "--ignore-gitleaks-allow" in argv
        assert not any(key.upper().startswith("GITLEAKS_") for key in kwargs["env"])
        assert 0 < timeout <= 30
        return real_execute(argv, cwd, timeout, limit, **kwargs)

    monkeypatch.setattr(secret_scan, "execute", inspect)
    secret_scan.scan_directory(work, scanner)
    assert len(observed) == 1 and not observed[0].exists()


def test_late_source_ignore_cannot_suppress_snapshot_findings(work, scanner, monkeypatch):
    (work / "credentials.txt").write_text(f"access_token = {TOKEN}\n")
    real_execute = secret_scan.execute
    codes = []

    def late_ignore(argv, *args, **kwargs):
        (work / ".gitleaksignore").write_text("credentials.txt:github-pat:1\n")
        assert Path(argv[2]) != work
        assert not (Path(argv[2]) / ".gitleaksignore").exists()
        result = real_execute(argv, *args, **kwargs)
        codes.append(result.code)
        return result

    monkeypatch.setattr(secret_scan, "execute", late_ignore)
    with pytest.raises(EvolutionError, match="details withheld"):
        secret_scan.scan_directory(work, scanner)
    assert codes == [1]


def test_copy_consumes_same_timeout_budget_as_scanner(work, scanner, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(secret_scan.time, "monotonic", lambda: now[0])
    original_copy = secret_scan._copy_tree
    def slow_copy(*args, **kwargs):
        original_copy(*args, **kwargs)
        now[0] += 7.0
    monkeypatch.setattr(secret_scan, "_copy_tree", slow_copy)
    def check_timeout(argv, cwd, timeout, *args, **kwargs):
        assert timeout == 23
        return ProcessResult("completed", 0, "", "", 0)
    monkeypatch.setattr(secret_scan, "execute", check_timeout)
    secret_scan.scan_directory(work, scanner)


def test_expired_snapshot_budget_fails_before_scanner(work, scanner, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(secret_scan.time, "monotonic", lambda: now[0])
    original_copy = secret_scan._copy_tree
    def exhausted_copy(*args, **kwargs):
        original_copy(*args, **kwargs)
        now[0] += 31.0
    monkeypatch.setattr(secret_scan, "_copy_tree", exhausted_copy)
    monkeypatch.setattr(secret_scan, "execute", lambda *args, **kwargs: pytest.fail("scanner must not run"))
    with pytest.raises(EvolutionError, match="details withheld"):
        secret_scan.scan_directory(work, scanner)


@pytest.mark.parametrize("output", ["clean", "secret_with_config", "root_ignore"])
def test_real_trial_only_records_passed_outputs_after_fixed_scan(experiment, scanner, monkeypatch, output):
    store, runner = experiment
    identity = {"scanner": {"resolved": scanner}}
    monkeypatch.setattr(runner, "check_environment", lambda profile: identity)
    profile = runner.config.targets[0].model_copy(update={"backend": "codex-native"})

    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        assert kwargs["expected_identity"] is identity
        (work / "report.json").write_text('{"answer": 42}')
        if output == "secret_with_config":
            (work / "report.json").write_text(json.dumps({"answer": 42, "token": TOKEN}))
            (work / "credentials.txt").write_text(f"access_token = {TOKEN}\n")
            (work / ".gitleaks.toml").write_text(WEAK_CONFIG)
        elif output == "root_ignore":
            (work / ".gitleaksignore").write_text("credentials.txt:github-pat:1\n")
        events = [
            {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0,
             "command": "/bin/cat " + shlex.quote(str(skill_folder / "SKILL.md")),
         "status": "completed", "aggregated_output": (skill_folder / "SKILL.md").read_text()}},
            {"type": "turn.completed"},
        ]
        return ProcessResult("completed", 0, "\n".join(map(json.dumps, events)), "", 0.01)

    monkeypatch.setattr(runner_module, "run_agent", transport)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    outputs = runner.path / f"{result['key']}.outputs.json"
    trace = runner.path / f"{result['key']}.trace.json"
    if output == "clean":
        assert result["status"] == "completed" and result["passed"]
        assert outputs.exists() and trace.exists()
    else:
        assert result["status"] == "infrastructure_error" and not result["passed"]
        assert not outputs.exists() and not trace.exists()
        assert TOKEN not in json.dumps(store.evaluations(runner.id))


def test_generator_uses_frozen_scanner_before_transport(experiment, monkeypatch):
    _, runner = experiment
    runner.config.generator = runner.config.targets[0].model_copy(update={"backend": "codex-native"})
    monkeypatch.setattr(runner, "check_environment", lambda profile: {"scanner": {"resolved": "/frozen/scanner"}})
    observed = []
    def stop_at_scan(work, scanner):
        observed.append(scanner)
        raise EvolutionError("controlled stop before model transport")
    monkeypatch.setattr(runner, "scan", stop_at_scan)
    with pytest.raises(EvolutionError, match="controlled stop"):
        ReflectionModel(runner)("public test prompt")
    assert observed == ["/frozen/scanner"]
