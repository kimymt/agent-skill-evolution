import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from agent_evolution import environment, runner as runner_module
from agent_evolution.adapters import command, parse_events, probe
from agent_evolution.cli import app
from agent_evolution.isolation import ProbeUnverified, isolation_evidence, preflight
from agent_evolution.models import Experiment, Profile
from agent_evolution.optimizer import ReflectionModel
from agent_evolution.process import ProcessResult
from agent_evolution.runner import Runner, create_run
from agent_evolution.util import EvolutionError, digest


def native(version=None):
    return Profile(id="native", agent="codex", model="specified-model", effort="high",
                   version=version, backend="codex-native")


@pytest.fixture
def native_capabilities(monkeypatch):
    calls = []
    identity = {"agent": "codex", "backend": "codex-native", "version": "99.7.2",
                "binary": "/test/codex", "resolved": "/test/codex", "sha256": "binary",
                "scanner": {"binary": "/test/gitleaks", "resolved": "/test/gitleaks"}}
    monkeypatch.setattr(environment, "runtime_identity", lambda p: dict(identity))
    features = "\n".join(name + " stable false" for name in
                         ("hooks", "memories", "multi_agent", "plugins", "remote_plugin", "apps", "js_repl", "code_mode"))
    features += "\nunified_exec stable true\n"

    def execute(argv, *args, **kwargs):
        calls.append(argv)
        if "--help" in argv:
            text = " ".join(environment.REQUIRED_FLAGS["codex"])
        elif argv[-2:] == ["features", "list"]:
            text = features
        elif argv[0] == "/test/gitleaks":
            text = ""
        else:
            pytest.fail("A capability check attempted an unexpected or model command")
        return ProcessResult("completed", 0, text, "", 0)

    monkeypatch.setattr(environment, "execute", execute)
    monkeypatch.setattr(environment, "scan_directory", lambda work, scanner: calls.append([scanner, "fixed-policy-scan", str(work)]))
    monkeypatch.setattr("agent_evolution.isolation.check_native", lambda *args: {
        "workspace_write": True, "outside_read": True, "outside_write": True,
        "skill_write": True, "network_denied": True})
    return identity, calls


def test_future_version_accepted_by_capabilities(native_capabilities):
    identity, calls = native_capabilities
    report = environment.inspect_profile(native())
    assert report["ready"]
    assert report["identity"]["version"] == "99.7.2"
    assert report["model_called"] is False
    assert preflight(native("auto")) == identity
    assert all("--help" in args or args[-2:] == ["features", "list"] or args[0] == "/test/gitleaks" for args in calls)
    assert next(c for c in report["checks"] if c["check"] == "live_auth_and_events") == {
        "check": "live_auth_and_events", "status": "unverified", "required": False,
        "detail": "Authentication, model connection and real event format require an authorized model execution"}


def test_explicit_version_mismatch_stops_before_probes(native_capabilities):
    _, calls = native_capabilities
    report = environment.inspect_profile(native("0.160.0"))
    assert not report["ready"]
    assert report["checks"][-1]["check"] == "version"
    assert calls == []


def test_isolation_unverified_is_not_ready(native_capabilities, monkeypatch):
    def unavailable(*args):
        raise ProbeUnverified("Outer sandbox prevented evidence")
    monkeypatch.setattr("agent_evolution.isolation.check_native", unavailable)
    report = environment.inspect_profile(native())
    assert not report["ready"]
    assert next(c for c in report["checks"] if c["check"] == "isolation")["status"] == "unverified"
    with pytest.raises(EvolutionError, match="isolation:unverified"):
        environment.require_ready(report)


def test_missing_invocation_capability_blocks(native_capabilities, monkeypatch):
    original = environment.execute
    def missing(argv, *args, **kwargs):
        if "--help" in argv:
            return ProcessResult("completed", 0, "--model --json", "", 0)
        return original(argv, *args, **kwargs)
    monkeypatch.setattr(environment, "execute", missing)
    assert not environment.inspect_profile(native())["ready"]


def test_secret_scan_failure_blocks_preflight_without_echoing_details(native_capabilities, monkeypatch):
    def failed(*args):
        raise EvolutionError("synthetic-sensitive-detail")
    monkeypatch.setattr(environment, "scan_directory", failed)
    report = environment.inspect_profile(native())
    assert not report["ready"]
    assert next(check for check in report["checks"] if check["check"] == "secret_scan")["status"] == "failed"
    assert "synthetic-sensitive-detail" not in json.dumps(report)


def test_changed_runtime_during_probes_blocks(native_capabilities, monkeypatch):
    identity, _ = native_capabilities
    calls = []
    def changed(profile):
        calls.append(profile)
        return {**identity, "sha256": str(len(calls))}
    monkeypatch.setattr(environment, "runtime_identity", changed)
    report = environment.inspect_profile(native())
    assert not report["ready"]
    assert next(c for c in report["checks"] if c["check"] == "stable_runtime")["status"] == "failed"


@pytest.mark.parametrize("evidence", ["{}", '{"workspace_write": true}', 'not-json', '[]'])
def test_invalid_isolation_evidence_is_unverified(evidence):
    with pytest.raises(ProbeUnverified):
        isolation_evidence(ProcessResult("failed", 71, evidence, "", 0), "Test")


def test_actual_isolation_denial_failure_is_failed():
    values = dict.fromkeys(("workspace_write", "outside_read", "outside_write", "skill_write", "network_denied"), True)
    values["skill_write"] = False
    with pytest.raises(EvolutionError, match="contract failed") as error:
        isolation_evidence(ProcessResult("failed", 1, json.dumps(values), "", 0), "Test")
    assert not isinstance(error.value, ProbeUnverified)


def test_create_run_failure_creates_no_run_or_budget(experiment, monkeypatch):
    store, r = experiment
    before = store.db.execute("select count(*) from runs").fetchone()[0]
    def blocked(profile):
        raise EvolutionError("capability missing")
    monkeypatch.setattr(runner_module, "preflight", blocked)
    with pytest.raises(EvolutionError, match="capability"):
        create_run(store, store.root.parent / "config.json")
    assert store.db.execute("select count(*) from runs").fetchone()[0] == before
    assert store.used(r.id) == {}


def test_runtime_drift_stops_before_task_budget(experiment, monkeypatch):
    store, r = experiment
    monkeypatch.setattr(runner_module, "preflight", lambda p: {"version": "changed"})
    with pytest.raises(EvolutionError, match="Runtime changed"):
        r.evaluate_split(r.data["baseline"], "train")
    assert store.used(r.id) == {}
    assert store.evaluations(r.id) == []


def test_profile_mutation_stops_before_task_budget(experiment):
    store, r = experiment
    r.config.targets[0].model = "different-model"
    with pytest.raises(EvolutionError, match="configuration changed"):
        r.evaluate_split(r.data["baseline"], "train")
    assert store.used(r.id) == {}


def test_engine_change_stops_before_task_budget(experiment, monkeypatch):
    store, r = experiment
    monkeypatch.setattr(runner_module, "engine_hash", lambda: "changed")
    with pytest.raises(EvolutionError, match="Implementation changed"):
        r.evaluate_split(r.data["baseline"], "train")
    assert store.used(r.id) == {}


def test_generator_drift_stops_before_generation_budget(experiment, monkeypatch):
    store, _ = experiment
    config_path = store.root.parent / "config.json"
    data = json.loads(config_path.read_text())
    data["generator"] = native().model_dump()
    config_path.write_text(json.dumps(data))
    scanner = {"resolved": "/test/gitleaks", "sha256": "initial"}
    monkeypatch.setattr(environment, "executable_identity", lambda name: scanner)
    monkeypatch.setattr(runner_module, "scan_values", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_module, "preflight", lambda p: {"version": "initial", "scanner": scanner})
    r = Runner(store, create_run(store, config_path))
    monkeypatch.setattr(runner_module, "preflight", lambda p: {"version": "updated"})
    with pytest.raises(EvolutionError, match="Runtime changed"):
        ReflectionModel(r)("a harmless test prompt")
    assert store.used(r.id) == {}


def test_unknown_adapter_never_falls_through_to_pi(tmp_path):
    bad = native().model_copy(update={"agent": "unknown"})
    for action in (lambda: command(bad, tmp_path, "test"), lambda: probe("unknown"),
                   lambda: parse_events("unknown", '{"type":"agent_end"}', "x")):
        with pytest.raises(EvolutionError, match="Unknown agent"):
            action()


@pytest.mark.parametrize("agent,record", [
    ("codex", {"type": "item.completed", "item": []}),
    ("claude", {"type": "assistant", "message": []}),
    ("opencode", {"type": "text", "part": []}),
    ("pi", {"type": "agent_end", "messages": [None]}),
])
def test_event_schema_change_is_sanitized_failure(agent, record):
    with pytest.raises(EvolutionError, match="Invalid agent event schema"):
        parse_events(agent, json.dumps(record), "skill")


def test_doctor_only_checks_selected_profiles_and_creates_no_state(experiment, monkeypatch, tmp_path):
    store, _ = experiment
    called = []
    def inspected(profile):
        called.append(profile.agent)
        return {"ready": True, "checks": []}
    monkeypatch.setattr(environment, "inspect_profile", inspected)
    target = tmp_path / "new-state"
    result = CliRunner().invoke(app, ["--state", str(target), "doctor", "--config", str(store.root.parent / "config.json")])
    assert result.exit_code == 0, result.output
    output = json.loads(result.output)
    assert output["ready"] and not output["model_called"] and not output["run_created"]
    assert called == ["codex"]
    assert not target.exists()


def test_doctor_returns_failure_for_required_unverified(experiment, monkeypatch, tmp_path):
    store, _ = experiment
    monkeypatch.setattr(environment, "inspect_profile", lambda p: {"ready": False, "checks": [{"status": "unverified"}]})
    result = CliRunner().invoke(app, ["--state", str(tmp_path / "state"), "doctor", "--config", str(store.root.parent / "config.json")])
    assert result.exit_code == 2
    assert not json.loads(result.output)["ready"]


def test_auto_identity_is_frozen_without_rewriting_config(experiment):
    _, r = experiment
    profile = r.config.targets[0]
    assert r.data["environments"][digest(profile.model_dump_json())]["version"] == "simulation"
    assert r.data["config_hash"] == digest(r.config.model_dump_json())


def test_docker_help_alone_never_proves_effective_settings(monkeypatch):
    profile = Profile(id="claude", agent="claude", model="chosen-model", effort="high", backend="docker",
                      image="agent@sha256:" + "a" * 64)
    monkeypatch.setattr(environment, "runtime_identity", lambda p: {
        "agent": "claude", "backend": "docker", "version": "7.2.1", "scanner": {"binary": "gitleaks", "resolved": "/test/gitleaks"}})
    monkeypatch.setattr(environment, "_container_probe", lambda *args: ProcessResult(
        "completed", 0, " ".join(environment.REQUIRED_FLAGS["claude"]), "", 0))
    monkeypatch.setattr(environment, "execute", lambda *args, **kwargs: ProcessResult("completed", 0, "", "", 0))
    monkeypatch.setattr(environment, "scan_directory", lambda *args: None)
    monkeypatch.setattr("agent_evolution.isolation.check_docker", lambda *args: {})
    report = environment.inspect_profile(profile)
    assert not report["ready"]
    assert next(c for c in report["checks"] if c["check"] == "arguments")["status"] == "passed"
    settings = next(c for c in report["checks"] if c["check"] == "settings")
    assert settings["required"] and settings["status"] == "unverified"


def test_docker_rejects_noninternal_network_before_launch(monkeypatch):
    profile = Profile(id="codex", agent="codex", model="chosen-model", effort="high", backend="docker",
                      image="agent@sha256:" + "a" * 64, network="ordinary-network")
    monkeypatch.setattr(environment, "executable_identity", lambda name: {"binary": name, "version": "1.0.0"})
    calls = []
    def execute(argv, *args, **kwargs):
        calls.append(argv)
        if argv[1:3] == ["image", "inspect"]:
            return ProcessResult("completed", 0, '[{"Id":"sha256:123"}]', "", 0)
        if argv[1:3] == ["network", "inspect"]:
            return ProcessResult("completed", 0, '[{"Internal":false,"Id":"network"}]', "", 0)
        pytest.fail("Container launched before validating the network")
    monkeypatch.setattr(environment, "execute", execute)
    with pytest.raises(EvolutionError, match="internal network"):
        environment.runtime_identity(profile)
    assert len(calls) == 2


def test_container_probe_uses_same_protection_wrapper_as_launch(tmp_path):
    from agent_evolution.isolation import container_command
    profile = Profile(id="pi", agent="pi", model="m", effort="high", backend="docker",
                      image="agent@sha256:" + "a" * 64)
    skill = tmp_path / ".agents/skills/probe"
    skill.mkdir(parents=True)
    args = container_command(profile, tmp_path, skill, "probe")
    assert "--read-only" in args and "--cap-drop=ALL" in args and "--security-opt=no-new-privileges" in args
    assert args[args.index("--network") + 1] == "none"
    assert "HOME=/home/evaluator" in args
    assert any(arg.endswith("dst=/workspace/.agents/skills/probe,readonly") for arg in args)
