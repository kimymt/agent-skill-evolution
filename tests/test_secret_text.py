"""Offline text-boundary tests with real Gitleaks and never-issued Stripe fixtures."""
import hashlib
import json
import shlex
from pathlib import Path
import shutil
import stat

import pytest

from agent_evolution import environment, optimizer, runner as runner_module, secret_scan
from agent_evolution.environment import executable_identity
from agent_evolution.optimizer import ReflectionModel
from agent_evolution.process import ProcessResult
from agent_evolution.runner import create_run
from agent_evolution.util import EvolutionError, canonical, digest, reject_secrets


TOKEN = "sk_live_" + hashlib.sha256(b"never-issued-agent-evolution-text-regression").hexdigest()[:32]


@pytest.fixture
def scanner():
    binary = shutil.which("gitleaks")
    if not binary:
        pytest.skip("real Gitleaks is required for offline text acceptance tests")
    return str(Path(binary).resolve())


def no_token_persisted(store):
    for path in store.root.rglob("*"):
        if path.is_file():
            if TOKEN.encode() in path.read_bytes():
                raise AssertionError("synthetic token was persisted")


def live_runner(experiment, scanner, monkeypatch):
    store, runner = experiment
    profile = runner.config.targets[0].model_copy(update={"backend": "codex-native"})
    identity = {"scanner": executable_identity("gitleaks")}
    assert identity["scanner"]["resolved"] == scanner
    runner.config.targets = [profile]
    runner.data["config"] = runner.config.model_dump()
    runner.data["config_hash"] = digest(runner.config.model_dump_json())
    runner.data["environments"][digest(profile.model_dump_json())] = identity
    monkeypatch.setattr(runner, "check_environment", lambda profile: identity)
    return store, runner, profile


def event_stream(skill_folder, work, text="public completed output"):
    records = [
        {"type": "item.completed", "item": {"type": "command_execution", "exit_code": 0,
         "command": "/bin/cat " + shlex.quote(str(skill_folder / "SKILL.md")),
         "status": "completed", "aggregated_output": (skill_folder / "SKILL.md").read_text()}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": text}},
        {"type": "turn.completed"},
    ]
    return "\n".join(map(json.dumps, records))


@pytest.mark.parametrize("value", [
    TOKEN,
    {"nested": [{"token": TOKEN}]},
    {TOKEN: "ordinary value"},
    "%PDF-1.7\n" + TOKEN,
    "\x00" + TOKEN,
    "public\n" * 17000 + "%PDF-1.7\n" + TOKEN,
], ids=["plain", "nested_value", "mapping_key", "pdf_header", "nul", "later_pdf_header"])
def test_values_reject_formats_not_covered_by_legacy_regex(scanner, value):
    reject_secrets(canonical(value))
    with pytest.raises(EvolutionError) as error:
        secret_scan.scan_values(value, scanner=scanner)
    assert TOKEN not in str(error.value)


def test_values_accept_clean_batch_and_empty_strings(scanner):
    secret_scan.scan_values("", "public words", {"nested": ["clean", 42, None]}, scanner=scanner)


def test_values_keep_existing_regex_protection(scanner):
    # This incomplete header is intentionally covered by the old regex, unlike
    # Gitleaks' complete-private-key pattern. Retaining it avoids a regression.
    with pytest.raises(EvolutionError):
        secret_scan.scan_values("-----BEGIN PRIVATE KEY-----", scanner=scanner)


def test_values_reject_mixed_batch_if_any_member_contains_secret(scanner):
    with pytest.raises(EvolutionError):
        secret_scan.scan_values("ordinary stdout", TOKEN, "ordinary stderr", scanner=scanner)


def test_values_use_stdin_without_plaintext_payload_files_or_arguments(scanner, monkeypatch):
    monkeypatch.setenv("GITLEAKS_CONFIG", "/untrusted/config.toml")
    monkeypatch.setenv("GITLEAKS_CONFIG_TOML", "untrusted policy")
    original = secret_scan.execute
    directories = []
    def inspected(argv, cwd, *args, **kwargs):
        directories.append(cwd)
        assert argv[:2] == [scanner, "stdin"]
        assert "--ignore-gitleaks-allow" in argv
        assert stat.S_IMODE(cwd.stat().st_mode) == 0o700
        assert not any(key.upper().startswith("GITLEAKS_") for key in kwargs["env"])
        exposed_in_argv = any(TOKEN in arg for arg in argv)
        assert not exposed_in_argv
        assert set(path.name for path in cwd.iterdir()) == {"policy.toml", "empty.ignore"}
        for path in cwd.iterdir():
            exposed_on_disk = TOKEN.encode() in path.read_bytes()
            assert not exposed_on_disk
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        contains_input = TOKEN in kwargs["stdin"]
        assert contains_input
        return original(argv, cwd, *args, **kwargs)
    monkeypatch.setattr(secret_scan, "execute", inspected)
    with pytest.raises(EvolutionError):
        secret_scan.scan_values(TOKEN, scanner=scanner)
    assert len(directories) == 1 and not directories[0].exists()


def test_streams_scan_decoded_jsonl_values_and_keys(scanner):
    encoded = "".join("\\u%04x" % ord(char) for char in TOKEN)
    stream = '{"type":"unknown","nested":{"' + encoded + '":"ordinary"}}\n'
    assert TOKEN not in stream
    with pytest.raises(EvolutionError):
        secret_scan.scan_streams(stream, "ordinary stderr", scanner=scanner)


@pytest.mark.parametrize("status", ["timeout", "infrastructure_error", "output_limit"])
def test_scanner_failure_is_closed_and_does_not_echo_streams(scanner, monkeypatch, status):
    monkeypatch.setattr(secret_scan, "execute", lambda *args, **kwargs:
                        ProcessResult(status, None, TOKEN, TOKEN, 0))
    with pytest.raises(EvolutionError) as error:
        secret_scan.scan_values("public text", scanner=scanner)
    assert TOKEN not in str(error.value)


def test_task_prompt_is_blocked_before_transport(experiment, scanner, monkeypatch):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    case = runner.cases("train")[0].model_copy(update={"request": "Use this value: " + TOKEN})
    monkeypatch.setattr(runner_module, "run_agent", lambda *args, **kwargs:
                        pytest.fail("a rejected prompt must not reach model transport"))
    result = runner.trial(runner.data["baseline"], case, profile)
    assert not result["passed"] and result["status"] == "infrastructure_error"
    assert not list(runner.path.glob("*.trace.json"))
    no_token_persisted(store)


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
@pytest.mark.parametrize("status", ["completed", "failed", "timeout", "cancelled", "output_limit"])
def test_task_streams_are_checked_before_any_evidence_write(experiment, scanner, monkeypatch, channel, status):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        (work / "report.json").write_text('{"answer":42}')
        stdout = event_stream(skill_folder, work, TOKEN if channel == "stdout" else "public")
        stderr = TOKEN if channel == "stderr" else "public diagnostic"
        return ProcessResult(status, 0 if status == "completed" else 1, stdout, stderr, 0.01)
    monkeypatch.setattr(runner_module, "run_agent", transport)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert not result["passed"]
    assert not list(runner.path.glob("*.stderr.txt"))
    assert not list(runner.path.glob("*.trace.json"))
    assert not list(runner.path.glob("*.outputs.json"))
    no_token_persisted(store)


def test_normalized_trace_is_checked_before_first_evidence_write(experiment, scanner, monkeypatch):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        (work / "report.json").write_text('{"answer":42}')
        return ProcessResult("completed", 0, event_stream(skill_folder, work), "public", 0.01)
    original = runner_module.parse_events
    def parsed(*args, **kwargs):
        result = original(*args, **kwargs)
        result["usage"] = {"provider_metadata": TOKEN}
        return result
    monkeypatch.setattr(runner_module, "run_agent", transport)
    monkeypatch.setattr(runner_module, "parse_events", parsed)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert not result["passed"]
    assert not list(runner.path.glob("*.stderr.txt"))
    assert not list(runner.path.glob("*.trace.json"))
    no_token_persisted(store)


def test_selected_output_bytes_are_checked_after_directory_scan(experiment, scanner, monkeypatch):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    original_scan = runner.scan
    scans = []
    def scan(work, scanner):
        original_scan(work, scanner)
        scans.append(work)
        if len(scans) == 2:
            (work / "report.json").write_text(json.dumps({"answer": 42, "token": TOKEN}))
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        (work / "report.json").write_text('{"answer":42}')
        return ProcessResult("completed", 0, event_stream(skill_folder, work), "public", 0.01)
    monkeypatch.setattr(runner, "scan", scan)
    monkeypatch.setattr(runner_module, "run_agent", transport)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert len(scans) == 2 and not result["passed"]
    assert not list(runner.path.glob("*.stderr.txt"))
    assert not list(runner.path.glob("*.trace.json"))
    assert not list(runner.path.glob("*.outputs.json"))
    no_token_persisted(store)


def test_clean_task_keeps_existing_result_and_evidence(experiment, scanner, monkeypatch):
    _, runner, profile = live_runner(experiment, scanner, monkeypatch)
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        (work / "report.json").write_text('{"answer":42}')
        return ProcessResult("completed", 0, event_stream(skill_folder, work), "public diagnostic", 0.01)
    monkeypatch.setattr(runner_module, "run_agent", transport)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert result["status"] == "completed" and result["passed"]
    assert len(list(runner.path.glob("*.stderr.txt"))) == 1
    assert len(list(runner.path.glob("*.trace.json"))) == 1
    assert len(list(runner.path.glob("*.outputs.json"))) == 1


def test_clean_failed_task_retains_sanitized_diagnostic(experiment, scanner, monkeypatch):
    _, runner, profile = live_runner(experiment, scanner, monkeypatch)
    monkeypatch.setattr(runner_module, "run_agent", lambda *args, **kwargs:
                        ProcessResult("failed", 1, "public partial stdout", "public failure diagnostic", 0.01))
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert result["status"] == "failed" and not result["passed"]
    assert (runner.path / f"{result['key']}.stderr.txt").read_text() == "public failure diagnostic"
    assert not list(runner.path.glob("*.trace.json"))


def test_stderr_is_checked_beyond_saved_diagnostic_limit(experiment, scanner, monkeypatch):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    monkeypatch.setattr(runner_module, "run_agent", lambda *args, **kwargs:
                        ProcessResult("failed", 1, "public partial stdout", "public\n" * 3000 + TOKEN, 0.01))
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert not result["passed"]
    assert not list(runner.path.glob("*.stderr.txt"))
    no_token_persisted(store)


@pytest.mark.parametrize("prompt", [TOKEN, [{"role": "user", "content": TOKEN}]], ids=["plain", "messages"])
def test_generator_prompt_is_blocked_before_transport(experiment, scanner, monkeypatch, prompt):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    runner.config.generator = profile
    monkeypatch.setattr(optimizer, "run_agent", lambda *args, **kwargs:
                        pytest.fail("a rejected prompt must not reach generator transport"))
    with pytest.raises(EvolutionError):
        ReflectionModel(runner)(prompt)
    no_token_persisted(store)


@pytest.mark.parametrize("channel", ["stdout", "stderr"])
@pytest.mark.parametrize("status", ["completed", "failed", "timeout"])
def test_generator_streams_are_blocked_before_feedback(experiment, scanner, monkeypatch, channel, status):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    runner.config.generator = profile
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        return ProcessResult(status, 0 if status == "completed" else 1,
                             event_stream(skill_folder, work, TOKEN if channel == "stdout" else "clean reflection"),
                             TOKEN if channel == "stderr" else "public diagnostic", 0.01)
    monkeypatch.setattr(optimizer, "run_agent", transport)
    with pytest.raises(EvolutionError):
        ReflectionModel(runner)("public prompt")
    assert store.db.execute("select count(*) from events where kind='generation'").fetchone()[0] == 0
    no_token_persisted(store)


def test_generator_parsed_text_is_checked_before_return(experiment, scanner, monkeypatch):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    runner.config.generator = profile
    monkeypatch.setattr(optimizer, "run_agent", lambda *args, **kwargs:
                        ProcessResult("completed", 0, '{"type":"turn.completed"}', "", 0.01))
    monkeypatch.setattr(optimizer, "parse_events", lambda *args: {"text": TOKEN, "usage": None, "cost": None})
    with pytest.raises(EvolutionError):
        ReflectionModel(runner)("public prompt")
    no_token_persisted(store)


def test_clean_generator_result_is_returned_and_usage_event_recorded(experiment, scanner, monkeypatch):
    store, runner, profile = live_runner(experiment, scanner, monkeypatch)
    runner.config.generator = profile
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        return ProcessResult("completed", 0, event_stream(skill_folder, work, "clean reflection result"),
                             "public diagnostic", 0.01)
    monkeypatch.setattr(optimizer, "run_agent", transport)
    assert ReflectionModel(runner)("public prompt") == "clean reflection result"
    assert store.db.execute("select count(*) from events where kind='generation'").fetchone()[0] == 1


@pytest.mark.parametrize("location", ["request", "skill", "generator_model"])
def test_live_run_input_is_checked_before_database_insert(experiment, scanner, monkeypatch, location):
    store, runner = experiment
    base = store.root.parent
    config = json.loads((base / "config.json").read_text())
    config["targets"][0]["backend"] = "codex-native"
    if location == "request":
        suite = json.loads((base / "suite.json").read_text())
        suite["cases"][0]["request"] = TOKEN
        (base / "suite.json").write_text(json.dumps(suite))
    elif location == "skill":
        skill = base / "skill/SKILL.md"
        skill.write_text(skill.read_text() + "\n" + TOKEN)
    else:
        config["generator"] = {**config["targets"][0], "id": "generator", "model": TOKEN}
    (base / "config.json").write_text(json.dumps(config))
    identity = {"scanner": executable_identity("gitleaks")}
    monkeypatch.setattr(runner_module, "preflight", lambda profile: identity)
    count = store.db.execute("select count(*) from runs").fetchone()[0]
    with pytest.raises(EvolutionError):
        create_run(store, base / "config.json")
    assert store.db.execute("select count(*) from runs").fetchone()[0] == count
    no_token_persisted(store)


def test_live_candidate_is_checked_before_database_update(experiment, scanner, monkeypatch):
    store, runner, _ = live_runner(experiment, scanner, monkeypatch)
    before = store.get(runner.id)
    with pytest.raises(EvolutionError):
        runner.add_candidate("Investigate the issue.\n" + TOKEN)
    assert store.get(runner.id) == before
    assert len(runner.data["artifacts"]) == 1
    no_token_persisted(store)


def test_manual_candidate_rejects_scanner_drift_before_scan_or_save(experiment, scanner, monkeypatch):
    store, runner, _ = live_runner(experiment, scanner, monkeypatch)
    before = store.get(runner.id)
    changed = {**executable_identity("gitleaks"), "sha256": "changed executable"}
    monkeypatch.setattr(environment, "executable_identity", lambda name: changed)
    monkeypatch.setattr(runner_module, "scan_values", lambda *args, **kwargs:
                        pytest.fail("scanner drift must fail before scanning"))
    with pytest.raises(EvolutionError, match="scanner changed"):
        runner.add_candidate("A clean manual candidate update.")
    assert store.get(runner.id) == before and len(runner.data["artifacts"]) == 1


def test_generator_only_live_run_rejects_input_before_database_insert(experiment, scanner, monkeypatch):
    store, _ = experiment
    base = store.root.parent
    config = json.loads((base / "config.json").read_text())
    config["generator"] = {**config["targets"][0], "id": "generator", "backend": "codex-native"}
    (base / "config.json").write_text(json.dumps(config))
    suite = json.loads((base / "suite.json").read_text())
    suite["cases"][0]["request"] = TOKEN
    (base / "suite.json").write_text(json.dumps(suite))
    identity = {"scanner": executable_identity("gitleaks")}
    monkeypatch.setattr(runner_module, "preflight", lambda profile:
                        {"version": "simulation"} if profile.backend == "simulation" else identity)
    count = store.db.execute("select count(*) from runs").fetchone()[0]
    with pytest.raises(EvolutionError):
        create_run(store, base / "config.json")
    assert store.db.execute("select count(*) from runs").fetchone()[0] == count
    no_token_persisted(store)


def test_simulation_pipeline_does_not_require_gitleaks(experiment, monkeypatch):
    store, runner = experiment
    monkeypatch.setattr(secret_scan, "execute", lambda *args, **kwargs:
                        pytest.fail("simulation must not invoke Gitleaks"))
    monkeypatch.setattr(secret_scan.shutil, "which", lambda *args: None)
    run = create_run(store, store.root.parent / "config.json")
    candidate = runner.add_candidate("Investigate the issue. SIMULATION_PASS\n")
    result = runner.trial(candidate, runner.cases("train")[0], runner.config.targets[0])
    assert run != runner.id and result["passed"]
