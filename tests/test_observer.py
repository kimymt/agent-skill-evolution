import importlib.util
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from agent_evolution.observer import Observer, PROJECT, MARKER, result_status
from agent_evolution.util import EvolutionError


@pytest.fixture
def watch(tmp_path):
    observer = Observer(tmp_path / "state")
    base = {"cwd": str(tmp_path / "work"), "session_id": "session", "turn_id": "turn-1"}
    observer.hook("codex", {**base, "hook_event_name": "UserPromptSubmit", "prompt": "Fix the bug"})
    return observer, base


def tool(observer, base, code, command="pytest", agent="codex"):
    return observer.hook(agent, {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                                "tool_input": {"command": command}, "tool_response": {"exit_code": code}})


def outcome(observer, base, status):
    return observer.outcome("codex", base["cwd"], base["session_id"], status, "保存処理のテストを確認", "investigate-bug")


def stop(observer, base):
    return observer.hook("codex", {**base, "hook_event_name": "Stop"})


def test_tool_failure_requires_final_assessment_before_user_prompt(watch):
    o, b = watch
    tool(o, b, 1)
    result = stop(o, b)
    assert result["decision"] == "block" and "notice" not in result
    assert not list((o.root / "notices").glob("*.json"))
    assert stop(o, b) == {}  # bounded continuation if the runtime cannot assess


@pytest.mark.parametrize("status", ["passed", "unverified", "cancelled"])
def test_only_final_failure_prompts(watch, status):
    o, b = watch
    tool(o, b, 1)
    outcome(o, b, status)
    assert stop(o, b) == {}
    assert not list((o.root / "notices").glob("*.json"))


def test_failure_then_recovery_does_not_prompt(watch):
    o, b = watch
    tool(o, b, 1)
    tool(o, b, 0)
    assert stop(o, b) == {}


def test_semantic_failure_with_successful_tools_prompts(watch):
    o, b = watch
    tool(o, b, 0)
    outcome(o, b, "failed")
    result = stop(o, b)
    assert result["notice"] and "保存処理" in result["question"]
    assert stop(o, b) == {}


def test_later_work_invalidates_old_final_assessment(watch):
    o, b = watch
    outcome(o, b, "failed")
    tool(o, b, 0)
    assert stop(o, b) == {}


def test_assessment_command_does_not_invalidate_its_own_result(watch):
    o, b = watch
    outcome(o, b, "failed")
    tool(o, b, 0, f"python {PROJECT}/scripts/observe.py outcome --status failed")
    assert stop(o, b)["notice"]


def test_stop_hook_continuation_preserves_assessment(watch):
    o, b = watch
    tool(o, b, 1)
    result = stop(o, b)
    o.hook("codex", {**b, "hook_event_name": "UserPromptSubmit", "prompt": result["reason"]})
    outcome(o, b, "failed")
    assert stop(o, b)["notice"]


@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_decision_never_starts_or_authorizes_evaluation(watch, decision, monkeypatch):
    o, b = watch
    outcome(o, b, "failed")
    ident = stop(o, b)["notice"]
    def forbidden(*args, **kwargs):
        raise AssertionError("Observer must not create a process")
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    result = o.decide(ident, decision)
    assert result["model_called"] is False
    assert result["evaluation_authorized"] is False
    assert result["promotion_authorized"] is False
    assert ("instruction" in result) == (decision == "approve")
    with pytest.raises(EvolutionError):
        o.decide(ident, decision)
    assert stop(o, b) == {}


def test_no_answer_stays_pending(watch):
    o, b = watch
    outcome(o, b, "failed")
    ident = stop(o, b)["notice"]
    assert json.loads(o.notice_path(ident).read_text())["status"] == "pending"
    assert stop(o, b) == {}


def test_expired_notice_cannot_be_approved(watch, monkeypatch):
    o, b = watch
    outcome(o, b, "failed")
    ident = stop(o, b)["notice"]
    import time
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now + 8 * 86400)
    with pytest.raises(EvolutionError, match="E_NOTICE_EXPIRED"):
        o.decide(ident, "approve")


def test_interrupt_suppresses_proposal(watch):
    o, b = watch
    outcome(o, b, "failed")
    o.hook("codex", {**b, "hook_event_name": "Interrupt"})
    assert stop(o, b) == {}


def test_new_task_does_not_inherit_old_failures(watch):
    o, b = watch
    tool(o, b, 1)
    o.hook("codex", {**b, "turn_id": "turn-2", "hook_event_name": "UserPromptSubmit", "prompt": "New task"})
    assert stop(o, b) == {}


def test_sessions_are_separate(watch):
    o, b = watch
    tool(o, b, 1)
    other = {**b, "session_id": "different"}
    o.hook("codex", {**other, "hook_event_name": "UserPromptSubmit", "prompt": "Other task"})
    assert stop(o, other) == {}
    assert "reason" in stop(o, b)


def test_repeated_concurrent_stop_offers_once(watch):
    o, b = watch
    outcome(o, b, "failed")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: stop(Observer(o.root), b), range(10)))
    assert sum("notice" in r for r in results) == 1


def test_no_raw_prompt_command_or_tool_output_saved(watch):
    o, b = watch
    secret = "sk-proj-" + "x" * 40
    tool(o, b, 1, "curl -H 'Authorization: Bearer " + secret + "' example.invalid")
    o.outcome("codex", b["cwd"], b["session_id"], "failed", secret)
    stop(o, b)
    persisted = "".join(p.read_text() for p in o.root.rglob("*.json"))
    assert secret not in persisted and "curl" not in persisted and "Fix the bug" not in persisted
    assert "[REDACTED]" in persisted


def test_evaluation_does_not_recursively_offer(watch, monkeypatch):
    o, b = watch
    outcome(o, b, "failed")
    monkeypatch.setenv("AGENT_EVOLUTION_EVALUATION", "1")
    assert stop(o, b) == {}


def test_approved_preparation_cannot_recursively_prompt(watch):
    o, b = watch
    outcome(o, b, "failed")
    ident = stop(o, b)["notice"]
    instruction = o.decide(ident, "approve")["instruction"]
    o.hook("codex", {**b, "turn_id": "preparation", "hook_event_name": "UserPromptSubmit", "prompt": instruction})
    tool(o, b, 1)
    marker = '<!-- agent-evolution-outcome {"status":"failed","summary":"preparation failure"} -->'
    assert o.hook("codex", {**b, "hook_event_name": "Stop", "last_assistant_message": marker}) == {}


@pytest.mark.parametrize("response,expected", [
    ({"exit_code": 0}, ("passed", 0)),
    ({"exit_code": 2}, ("failed", 2)),
    ({"isError": True}, ("failed", None)),
    ("Chunk ID: x\nProcess exited with code 1\nFinal output: xyz", ("failed", 1)),
    ("The text mentions error or failed tests", ("unknown", None)),
    ({"exit_code": None, "session_id": 12}, ("unknown", None)),
])
def test_status_parsing_uses_runtime_evidence(response, expected):
    assert result_status({"tool_response": response}) == expected


def test_claude_failure_event():
    assert result_status({"hook_event_name": "PostToolUseFailure", "error": "Exit code 1\noutput"}) == ("failed", 1)
    assert result_status({"hook_event_name": "PostToolUseFailure", "is_interrupt": True}) == ("cancelled", None)


def test_native_hook_wire_format(tmp_path):
    base = {"cwd": str(tmp_path), "session_id": "smoke", "prompt": "Task"}
    command = [sys.executable, str(PROJECT / "scripts/observe.py"), "--state", str(tmp_path / "state"), "--agent", "codex"]
    result = subprocess.run([*command, "hook"], input=json.dumps({**base, "hook_event_name": "UserPromptSubmit"}), text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    subprocess.run([*command, "--cwd", str(tmp_path), "--session", "smoke", "outcome", "--status", "failed", "--summary", "check failed"], capture_output=True, check=True)
    result = subprocess.run([*command, "hook"], input=json.dumps({**base, "hook_event_name": "Stop"}), text=True, capture_output=True, check=True)
    assert set(json.loads(result.stdout)) == {"decision", "reason"}


@pytest.mark.parametrize("status", ["passed", "failed", "unverified", "cancelled"])
def test_local_outcome_works_without_metadata_in_final_response(watch, status):
    o, b = watch
    tool(o, b, 1)
    outcome(o, b, status)
    result = o.hook("codex", {**b, "hook_event_name": "Stop", "last_assistant_message": "通常の完了報告です。"})
    assert ("notice" in result) == (status == "failed")
    assert stop(o, b) == {}


@pytest.mark.parametrize("agent", ["codex", "claude", "pi", "opencode"])
def test_guidance_uses_local_recording_without_output_markers(watch, agent):
    o, b = watch
    text = o.guidance(agent, b["cwd"], b["session_id"])
    if agent == "pi":
        assert "agent_evolution_outcome" in text
        assert "記録コマンド:" not in text
        assert "bashでobserve.pyを実行しない" in text
    else:
        assert "outcome --status" in text
    assert "<!--" not in text and "agent-evolution-outcome" not in text
    assert "回答本文に" in text and "付けない" in text


def test_legacy_comment_cannot_override_local_status(watch):
    o, b = watch
    outcome(o, b, "passed")
    message = '<!-- agent-evolution-outcome {"status":"failed","summary":"stale marker"} -->'
    assert o.hook("codex", {**b, "hook_event_name": "Stop", "last_assistant_message": message}) == {}


def installer():
    spec = importlib.util.spec_from_file_location("installer", PROJECT / "scripts/install_observer.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_install_preserves_existing_hooks_and_is_idempotent(tmp_path):
    path = tmp_path / ".codex/hooks.json"
    path.parent.mkdir()
    original = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "existing-hook"}]}]}}
    path.write_text(json.dumps(original))
    module = installer()
    from observer_bundle import build
    runtime = build(PROJECT, tmp_path / "builds")
    result = module.install(tmp_path, ["codex", "claude", "pi", "opencode"], runtime)
    assert len(result) == 4
    assert json.loads(path.read_text())["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
    receipt = json.loads(Path(result[0]["receipt"]).read_text())
    assert receipt["status"] == "applied"
    assert receipt["changes"][0]["previous_managed"] == []
    assert all(change["applied"] for change in receipt["changes"])
    assert "existing-hook" not in json.dumps(receipt)
    assert module.install(tmp_path, ["codex", "claude", "pi", "opencode"], runtime) == []
    assert not (tmp_path / ".codex/config.toml").exists()


def test_install_collision_prevents_all_writes(tmp_path):
    p = tmp_path / ".pi/agent/extensions/agent-evolution.js"
    p.parent.mkdir(parents=True)
    p.write_text("existing-user-extension")
    module = installer()
    from observer_bundle import build
    runtime = build(PROJECT, tmp_path / "builds")
    with pytest.raises(ValueError, match="preserved"):
        module.install(tmp_path, ["codex", "pi"], runtime)
    assert not (tmp_path / ".codex/hooks.json").exists()
    assert p.read_text() == "existing-user-extension"


# Exercise the executable and its filesystem boundary, rather than only mocked hooks.
def cli(agent, cwd, args, payload=None, state=None, workdir=None):
    command = [sys.executable, str(PROJECT / "scripts/observe.py"), "--agent", agent, "--cwd", str(cwd)]
    if state is not None:
        command += ["--state", str(state)]
    return subprocess.run([*command, *args], input=json.dumps(payload) if payload is not None else "",
                          text=True, capture_output=True, cwd=workdir)


@pytest.mark.parametrize("agent", ["codex", "claude", "pi", "opencode"])
@pytest.mark.parametrize("decision", ["approve", "decline"])
def test_project_storage_full_lifecycle_and_decision_next_turn(tmp_path, agent, decision):
    project = tmp_path / "project"
    elsewhere = tmp_path / "other-tool-workdir"
    project.mkdir()
    elsewhere.mkdir()
    base = {"cwd": str(project), "session_id": "session", "turn_id": "first"}
    start = cli(agent, elsewhere, ["hook"], {**base, "hook_event_name": "UserPromptSubmit", "prompt": "task"})
    assert start.returncode == 0
    state = project / ".evolution-observer"
    assert state.is_dir() and not (elsewhere / ".evolution-observer").exists()
    assert str(state) in json.loads(start.stdout)["hookSpecificOutput"]["additionalContext"] or agent == "pi"
    result = cli(agent, project, ["--session", "session", "outcome", "--status", "failed", "--summary", "check failed"],
                 state=state, workdir=elsewhere)
    assert result.returncode == 0 and json.loads(result.stdout)["recorded"] == "failed"
    stopped = cli(agent, elsewhere, ["event"], {**base, "hook_event_name": "Stop"}, state=state)
    notice = json.loads(stopped.stdout)["notice"]
    cli(agent, project, ["hook"], {**base, "turn_id": "second", "hook_event_name": "UserPromptSubmit", "prompt": decision})
    decided = cli(agent, project, ["--session", "session", "decide", "--notice", notice, "--decision", decision],
                  state=state, workdir=elsewhere)
    value = json.loads(decided.stdout)
    assert decided.returncode == 0 and value["status"] == ("approved" if decision == "approve" else "declined")
    assert value["model_called"] is False and value["evaluation_authorized"] is False
    assert ("instruction" in value) == (decision == "approve")
    again = cli(agent, project, ["--session", "session", "decide", "--notice", notice, "--decision", decision], state=state)
    assert again.returncode == 2 and "E_NOTICE_DECIDED" in again.stderr
    listing = cli(agent, elsewhere, ["list", "--cwd", str(project)])
    assert json.loads(listing.stdout)[0]["id"] == notice


def test_explicit_legacy_state_and_read_only_list_preserve_records(tmp_path):
    central = tmp_path / "central"
    project = tmp_path / "project"
    project.mkdir()
    observer = Observer(central)
    b = {"cwd": str(project), "session_id": "s", "hook_event_name": "UserPromptSubmit"}
    observer.hook("codex", b)
    observer.outcome("codex", str(project), "s", "failed", "test")
    notice = observer.hook("codex", {**b, "hook_event_name": "Stop"})["notice"]
    before = {p.relative_to(central): p.read_bytes() for p in central.rglob("*") if p.is_file()}
    result = cli("codex", project, ["list"], state=central)
    assert result.returncode == 0 and json.loads(result.stdout)[0]["id"] == notice
    assert before == {p.relative_to(central): p.read_bytes() for p in central.rglob("*") if p.is_file()}
    assert not (project / ".evolution-observer").exists()
    missing = tmp_path / "missing"
    result = cli("codex", project, ["list"], state=missing)
    assert result.returncode == 0 and json.loads(result.stdout) == [] and not missing.exists()


@pytest.mark.parametrize("suppression", [{"is_subagent": True}, {"agent_id": "child"}, {"evaluation": True}, {"evaluation_path": True}])
def test_suppressed_hook_does_not_create_state(tmp_path, suppression, monkeypatch):
    cwd = tmp_path / ("agent-evolution-task-test" if suppression.get("evaluation_path") else "project")
    cwd.mkdir()
    if suppression.get("evaluation"):
        monkeypatch.setenv("AGENT_EVOLUTION_EVALUATION", "1")
    payload = {"cwd": str(cwd), "session_id": "s", "hook_event_name": "UserPromptSubmit", **suppression}
    result = cli("codex", cwd, ["hook"], payload)
    assert result.returncode == 0 and json.loads(result.stdout) == {}
    assert not (cwd / ".evolution-observer").exists()


def test_invalid_hook_is_rejected_before_state_creation(tmp_path):
    result = cli("codex", tmp_path, ["hook"], {"cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit"})
    assert result.returncode == 0 and "E_INPUT" in result.stdout
    assert not (tmp_path / ".evolution-observer").exists()


@pytest.mark.parametrize("entry", ["root", "parent", "sessions", "notices", "observer.lock", ".gitignore", "session.json", "notice.json"])
def test_symlink_storage_is_rejected_without_touching_destination(tmp_path, entry):
    external = tmp_path / "external"
    external.mkdir()
    sentinel = external / "sentinel"
    sentinel.write_text("do not change")
    root = tmp_path / "state"
    if entry == "root":
        root.symlink_to(external, target_is_directory=True)
        action = lambda: Observer(root)
    elif entry == "parent":
        root.symlink_to(external, target_is_directory=True)
        action = lambda: Observer(root / "nested")
    else:
        observer = Observer(root)
        if entry in {"sessions", "notices"}:
            (root / entry).symlink_to(external, target_is_directory=True)
            action = lambda: observer.write("a" * 64, {}) if entry == "sessions" else observer.notices("codex")
        elif entry in {"observer.lock", ".gitignore"}:
            path = root / entry
            path.unlink(missing_ok=True)
            path.symlink_to(sentinel)
            action = (lambda: observer.outcome("codex", str(tmp_path), "s", "passed", "test")) if entry == "observer.lock" else (lambda: Observer(root))
        else:
            folder = root / ("sessions" if entry == "session.json" else "notices")
            folder.mkdir(mode=0o700)
            path = folder / (("a" * (64 if entry == "session.json" else 32)) + ".json")
            path.symlink_to(sentinel)
            action = (lambda: observer.read("a" * 64)) if entry == "session.json" else (lambda: observer.decide("a" * 32, "approve"))
    with pytest.raises(EvolutionError, match="E_UNSAFE_PATH"):
        action()
    assert sentinel.read_text() == "do not change"
    assert set(external.iterdir()) == {sentinel}


@pytest.mark.parametrize("kind", ["file_root", "directory_lock", "fifo_record"])
def test_wrong_file_types_fail_without_hanging(tmp_path, kind):
    import os
    root = tmp_path / "state"
    if kind == "file_root":
        root.write_text("unchanged")
        action = lambda: Observer(root)
    else:
        observer = Observer(root)
        if kind == "directory_lock":
            (root / "observer.lock").mkdir()
            action = lambda: observer.outcome("codex", str(tmp_path), "s", "passed", "test")
        else:
            (root / "sessions").mkdir(mode=0o700)
            os.mkfifo(observer.path("a" * 64))
            action = lambda: observer.read("a" * 64)
    with pytest.raises(EvolutionError, match="E_UNSAFE_PATH"):
        action()


def test_new_storage_is_private_and_git_ignores_it(tmp_path):
    import stat
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    root = tmp_path / ".evolution-observer"
    observer = Observer(root)
    observer.hook("codex", {"cwd": str(tmp_path), "session_id": "s", "hook_event_name": "UserPromptSubmit"})
    for path in [root, *root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    assert (root / ".gitignore").read_text() == "*\n"
    assert not (tmp_path / ".gitignore").exists()
    result = subprocess.run(["git", "-C", str(tmp_path), "status", "--porcelain", "--untracked-files=all"], text=True, capture_output=True, check=True)
    assert result.stdout == ""
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    assert subprocess.run(["git", "-C", str(tmp_path), "diff", "--cached", "--name-only"], capture_output=True, text=True, check=True).stdout == ""


def test_diagnostics_distinguish_lifecycle_and_never_echo_secrets(tmp_path):
    from agent_evolution.observer import TTL
    import time
    secret = "sk-proj-" + "x" * 40
    root = tmp_path / "state"
    args = ["--session", "s", "outcome", "--status", "passed", "--summary", secret]
    missing = cli("codex", tmp_path, args, state=root)
    assert missing.returncode == 2 and "E_STATE_MISSING" in missing.stderr and secret not in missing.stderr
    observer = Observer(root)
    base = {"cwd": str(tmp_path), "session_id": "s", "hook_event_name": "UserPromptSubmit"}
    observer.hook("codex", base)
    identity = observer.identity("codex", str(tmp_path), "s")
    state = observer.read(identity)
    state["created"] = time.time() - TTL - 1
    observer.write(identity, state)
    assert "E_TASK_EXPIRED" in cli("codex", tmp_path, args, state=root).stderr
    state["created"] = time.time()
    state["closed"] = True
    observer.write(identity, state)
    assert "E_TASK_CLOSED" in cli("codex", tmp_path, args, state=root).stderr
    observer.path(identity).write_text('{"secret": "' + secret + '"}')
    corrupt = cli("codex", tmp_path, args, state=root)
    assert "E_STATE_CORRUPT" in corrupt.stderr and secret not in corrupt.stderr
    invalid = cli("codex", tmp_path, ["outcome", "--status", secret, "--summary", secret], state=root)
    assert invalid.returncode == 2 and "E_INPUT" in invalid.stderr and secret not in invalid.stderr


def test_permission_rejection_has_no_fallback_and_local_project_succeeds(tmp_path):
    central = tmp_path / "central"
    central.mkdir(mode=0o500)
    project = tmp_path / "project"
    project.mkdir()
    base = {"cwd": str(project), "session_id": "s", "hook_event_name": "UserPromptSubmit"}
    try:
        old = cli("codex", project, ["hook"], base, state=central)
        assert old.returncode == 0 and "E_PERMISSION" in old.stdout
        assert list(central.iterdir()) == []
        assert not (project / ".evolution-observer").exists()
        new = cli("codex", project, ["hook"], base)
        assert new.returncode == 0 and "hookSpecificOutput" in json.loads(new.stdout)
        result = cli("codex", project, ["--session", "s", "outcome", "--status", "passed", "--summary", "checked"])
        assert result.returncode == 0 and json.loads(result.stdout)["recorded"] == "passed"
        assert list(central.iterdir()) == []
    finally:
        central.chmod(0o700)


def test_event_errors_are_structured_but_native_hook_contract_is_preserved(tmp_path):
    payload = {"cwd": str(tmp_path), "session_id": "s", "hook_event_name": "Unsupported", "private": "secret"}
    native = json.loads(cli("codex", tmp_path, ["hook"], payload).stdout)
    event = json.loads(cli("codex", tmp_path, ["event"], payload).stdout)
    assert set(native) == {"systemMessage"}
    assert event["error"] == {"code": "E_INPUT", "operation": "event"}
    assert "secret" not in json.dumps([native, event])


def test_pi_outcome_tool_does_not_invalidate_its_record(watch):
    observer, base = watch
    outcome(observer, base, "failed")
    observer.hook("codex", {**base, "hook_event_name": "PostToolUse", "tool_name": "agent_evolution_outcome",
                            "tool_input": {"status": "failed", "summary": "test"}, "tool_response": {"isError": False}})
    assert stop(observer, base)["notice"]


def test_decision_rejects_other_product_or_session(watch):
    observer, base = watch
    outcome(observer, base, "failed")
    notice = stop(observer, base)["notice"]
    for agent, session in [("claude", base["session_id"]), ("codex", "another-session")]:
        with pytest.raises(EvolutionError, match="E_INPUT"):
            observer.decide(notice, "approve", agent, base["cwd"], session)
    assert json.loads(observer.notice_path(notice).read_text())["status"] == "pending"


@pytest.mark.parametrize("content", ["", "*\n!*.json\n", "sessions/\n"])
def test_existing_gitignore_must_exclude_all_records(tmp_path, content):
    root = tmp_path / "state"
    root.mkdir(mode=0o700)
    ignore = root / ".gitignore"
    ignore.write_text(content)
    ignore.chmod(0o600)
    with pytest.raises(EvolutionError, match="E_STATE_CORRUPT"):
        Observer(root)
    assert ignore.read_text() == content
    assert set(root.iterdir()) == {ignore}


def test_storage_does_not_attempt_to_create_existing_ancestors(tmp_path, monkeypatch):
    import os
    original = os.mkdir
    attempted = []
    def checked(path, mode=0o777, *, dir_fd=None):
        attempted.append(str(path))
        assert str(path) == "state", "must not request writes to existing read-only ancestors"
        return original(path, mode, dir_fd=dir_fd)
    monkeypatch.setattr(os, "mkdir", checked)
    Observer(tmp_path / "state")
    assert attempted == ["state"]


@pytest.mark.parametrize("mode", [0o755, 0o777])
@pytest.mark.parametrize("operation", ["hook", "list"])
def test_existing_nonprivate_root_is_rejected_without_repair(tmp_path, mode, operation):
    import stat
    root = tmp_path / "state"
    root.mkdir(mode=mode)
    root.chmod(mode)
    sentinel = root / "keep"
    sentinel.write_text("synthetic existing state")
    payload = {"cwd": str(tmp_path), "session_id": "s", "hook_event_name": "UserPromptSubmit"}
    result = cli("codex", tmp_path, [operation], payload if operation == "hook" else None, state=root)
    assert "E_PERMISSION" in result.stdout + result.stderr
    assert result.returncode == (0 if operation == "hook" else 2)
    assert stat.S_IMODE(root.stat().st_mode) == mode
    assert sentinel.read_text() == "synthetic existing state"
    assert set(root.iterdir()) == {sentinel}


@pytest.mark.parametrize("target", ["root", "notices", "notice"])
def test_read_only_list_checks_private_permissions_without_rewriting(watch, target):
    import stat
    observer, base = watch
    outcome(observer, base, "failed")
    ident = stop(observer, base)["notice"]
    path = {"root": observer.root, "notices": observer.root / "notices", "notice": observer.notice_path(ident)}[target]
    before = observer.notice_path(ident).read_bytes()
    bad_mode = 0o755 if path.is_dir() else 0o644
    path.chmod(bad_mode)
    result = cli("codex", Path(base["cwd"]).parent, ["list"], state=observer.root)
    assert result.returncode == 2 and "E_PERMISSION" in result.stderr
    assert observer.notice_path(ident).read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == bad_mode


@pytest.mark.parametrize("entry", [".gitignore", "observer.lock", "session", "notice"])
def test_hardlinked_state_files_are_rejected_without_touching_alias(watch, entry):
    import os
    observer, base = watch
    outcome(observer, base, "failed")
    ident = stop(observer, base)["notice"]
    session = observer.identity("codex", base["cwd"], base["session_id"])
    path = (observer.path(session) if entry == "session" else observer.notice_path(ident)
            if entry == "notice" else observer.root / entry)
    before = path.read_bytes()
    alias = observer.root.parent / "external-alias"
    os.link(path, alias)
    def locked():
        with observer.locked():
            pass
    action = {".gitignore": lambda: Observer(observer.root), "observer.lock": locked,
              "session": lambda: observer.read(session),
              "notice": lambda: Observer(observer.root, create=False).notices("codex")}[entry]
    with pytest.raises(EvolutionError, match="E_UNSAFE_PATH"):
        action()
    assert alias.read_bytes() == path.read_bytes() == before
    assert path.stat().st_nlink == 2


@pytest.mark.parametrize("operation", ["hook", "list"])
def test_default_state_rejects_symlink_cwd_before_creating_records(tmp_path, operation):
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "linked-cwd"
    link.symlink_to(target, target_is_directory=True)
    payload = {"cwd": str(link), "session_id": "s", "hook_event_name": "UserPromptSubmit"}
    result = cli("codex", link, [operation], payload if operation == "hook" else None)
    assert "E_UNSAFE_PATH" in result.stdout + result.stderr
    assert not (target / ".evolution-observer").exists()


def test_oversized_stored_record_keeps_fixed_diagnostic(watch):
    from agent_evolution.observer import MAX_BYTES
    observer, base = watch
    ident = observer.identity("codex", base["cwd"], base["session_id"])
    marker = "synthetic private record"
    observer.path(ident).write_text(marker + " " * MAX_BYTES)
    result = cli("codex", base["cwd"], ["--session", base["session_id"], "outcome", "--status", "passed", "--summary", "test"], state=observer.root)
    assert result.returncode == 2 and "E_STATE_CORRUPT" in result.stderr
    assert marker not in result.stderr + result.stdout


def test_grok_camelcase_payload_starts_a_session(tmp_path):
    from agent_evolution.observer import Observer
    observer = Observer(tmp_path / "state")
    payload = {"cwd": str(tmp_path / "work"), "sessionId": "session", "hookEventName": "UserPromptSubmit",
               "prompt": "Fix the bug", "turnId": "turn-1"}
    result = observer.hook("grok", payload)
    assert "hookSpecificOutput" in result
    assert payload["hook_event_name"] == "UserPromptSubmit"
    assert payload["session_id"] == "session"
    assert list((observer.root / "sessions").glob("*.json"))


def test_grok_stopcancelled_is_treated_as_interrupt(tmp_path):
    from agent_evolution.observer import Observer
    observer = Observer(tmp_path / "state")
    work = tmp_path / "work"
    base = {"cwd": str(work), "session_id": "session", "turn_id": "turn-1"}
    observer.hook("grok", {**base, "hook_event_name": "UserPromptSubmit", "prompt": "task"})
    observer.hook("grok", {**base, "hook_event_name": "PostToolUse", "tool_name": "Bash",
                           "tool_input": {"command": "pytest"}, "tool_response": {"exit_code": 1}})
    cancelled = {"cwd": str(work), "sessionId": "session", "hookEventName": "StopCancelled"}
    assert observer.hook("grok", cancelled) == {}
    assert cancelled["hook_event_name"] == "Interrupt"
    assert observer.hook("grok", {**base, "hook_event_name": "Stop"}) == {}


def test_grok_runtime_ignores_non_grok_agents_and_records_grok(tmp_path, monkeypatch):
    """Grok injects GROK_SESSION_ID / GROK_HOOK_EVENT into hook processes.
    https://docs.x.ai/build/features/hooks
    """
    from agent_evolution.observer import Observer
    monkeypatch.setenv("GROK_SESSION_ID", "grok-session-1")
    monkeypatch.setenv("GROK_HOOK_EVENT", "UserPromptSubmit")
    observer = Observer(tmp_path / "state")
    work = tmp_path / "work"
    payload = {"cwd": str(work), "session_id": "session", "hook_event_name": "UserPromptSubmit", "prompt": "task"}
    assert observer.hook("claude", payload) == {}
    assert observer.hook("codex", payload) == {}
    assert list((observer.root / "sessions").glob("*.json")) == []
    assert "hookSpecificOutput" in observer.hook("grok", payload)
    assert list((observer.root / "sessions").glob("*.json"))


def test_cli_grok_runtime_does_not_record_claude_hooks(tmp_path, monkeypatch):
    monkeypatch.setenv("GROK_SESSION_ID", "grok-session-1")
    project = tmp_path / "project"
    project.mkdir()
    payload = {"cwd": str(project), "session_id": "session", "hookEventName": "UserPromptSubmit", "prompt": "task"}
    ignored = cli("claude", project, ["hook"], payload)
    assert ignored.returncode == 0 and json.loads(ignored.stdout) == {}
    assert not (project / ".evolution-observer").exists()
    recorded = cli("grok", project, ["hook"], payload)
    assert recorded.returncode == 0 and "hookSpecificOutput" in json.loads(recorded.stdout)
    assert (project / ".evolution-observer").is_dir()


def test_grok_official_post_tool_use_aliases_are_accepted_via_cli(tmp_path):
    """Grok sends camelCase and snake_case together; tool_input is a dict.
    The CLI path runs hook_context twice on the same payload.
    """
    project = tmp_path / "project"
    project.mkdir()
    start = {
        "cwd": str(project),
        "workspaceRoot": str(project),
        "sessionId": "session",
        "session_id": "session",
        "hookEventName": "user_prompt_submit",
        "hook_event_name": "UserPromptSubmit",
        "promptId": "turn-1",
        "prompt": "task",
    }
    recorded = cli("grok", project, ["hook"], start)
    assert recorded.returncode == 0 and "hookSpecificOutput" in json.loads(recorded.stdout)
    payload = {
        "cwd": str(project),
        "workspaceRoot": str(project),
        "sessionId": "session",
        "session_id": "session",
        "hookEventName": "post_tool_use",
        "hook_event_name": "PostToolUse",
        "toolName": "list_dir",
        "tool_name": "list_dir",
        "toolInput": {"path": str(project)},
        "tool_input": {"path": str(project)},
        "toolResult": {"type": "ListDir", "entries": []},
        "tool_response": {"type": "ListDir", "entries": []},
        "timestamp": "2026-11-26T14:38:23Z",
        "permissionMode": "default",
        "toolUseId": "tu-1",
    }
    result = cli("grok", project, ["hook"], payload)
    assert result.returncode == 0, result.stdout
    assert json.loads(result.stdout) == {}
    assert "E_INPUT" not in result.stdout


def test_grok_snake_case_event_name_and_tool_result_are_normalized(tmp_path):
    from agent_evolution.observer import Observer
    observer = Observer(tmp_path / "state")
    work = tmp_path / "work"
    payload = {"cwd": str(work), "sessionId": "session", "hookEventName": "user_prompt_submit",
               "prompt": "task", "promptId": "turn-1"}
    assert "hookSpecificOutput" in observer.hook("grok", payload)
    assert payload["hook_event_name"] == "UserPromptSubmit"
    tool = {"cwd": str(work), "sessionId": "session", "hookEventName": "post_tool_use",
            "toolName": "Bash", "toolInput": {"command": "pytest"},
            "toolResult": {"exit_code": 1}}
    assert observer.hook("grok", tool) == {}
    assert tool["hook_event_name"] == "PostToolUse"
    assert tool["tool_response"]["exit_code"] == 1
    sessions = list((observer.root / "sessions").glob("*.json"))
    assert len(sessions) == 1
    data = json.loads(sessions[0].read_text())
    assert data["failures"]
