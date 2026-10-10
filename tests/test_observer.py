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
    with pytest.raises(EvolutionError, match="expired"):
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
    result = module.install(tmp_path, ["codex", "claude", "pi", "opencode"])
    assert len(result) == 4
    assert json.loads(path.read_text())["hooks"]["Stop"][0] == original["hooks"]["Stop"][0]
    assert json.loads(Path(result[0]["backup"]).read_text()) == original
    assert module.install(tmp_path, ["codex", "claude", "pi", "opencode"]) == []
    assert not (tmp_path / ".codex/config.toml").exists()


def test_install_collision_prevents_all_writes(tmp_path):
    p = tmp_path / ".pi/agent/extensions/agent-evolution.js"
    p.parent.mkdir(parents=True)
    p.write_text("existing-user-extension")
    with pytest.raises(ValueError, match="preserved"):
        installer().install(tmp_path, ["codex", "pi"])
    assert not (tmp_path / ".codex/hooks.json").exists()
    assert p.read_text() == "existing-user-extension"
