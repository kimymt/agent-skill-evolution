import json
from pathlib import Path

import pytest

from agent_evolution.adapters import parse_events, command
from agent_evolution.models import Profile
from agent_evolution.util import EvolutionError

SKILL = ".agents/skills/sample/SKILL.md"


def stream(*records):
    return "\n".join(json.dumps(r) for r in records)


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode", "pi"])
def test_adapter_requires_successful_terminal_event(agent):
    with pytest.raises(EvolutionError):
        parse_events(agent, stream({"type": "some_event"}), SKILL)
    with pytest.raises(EvolutionError):
        parse_events(agent, "not-json", SKILL)


def test_codex_completed_read_and_usage():
    output = stream({"type": "item.completed", "item": {"type": "command_execution", "command": f"/bin/zsh -lc 'cat {SKILL}'", "exit_code": 0}},
                    {"type": "turn.completed", "usage": {"input_tokens": 10}})
    trace = parse_events("codex", output, SKILL)
    assert trace["skill_read"]
    assert trace["usage"]["input_tokens"] == 10
    assert trace["cost"] is None


def test_read_claim_in_echo_not_evidence():
    output = stream({"type": "item.completed", "item": {"type": "command_execution", "command": f"echo 'cat {SKILL}'", "exit_code": 0}}, {"type": "turn.completed"})
    assert not parse_events("codex", output, SKILL)["skill_read"]


def test_failed_read_not_evidence():
    output = stream({"type": "item.completed", "item": {"type": "command_execution", "command": f"cat {SKILL}", "exit_code": 1}}, {"type": "turn.completed"})
    assert not parse_events("codex", output, SKILL)["skill_read"]


@pytest.mark.parametrize("command_text", [f"true || cat {SKILL}", f"false && cat {SKILL}; true", f"cat {SKILL}; true", f"exit 0\ncat {SKILL}"])
def test_conditional_or_masked_read_not_evidence(command_text):
    output = stream({"type": "item.completed", "item": {"type": "command_execution", "command": command_text, "exit_code": 0}}, {"type": "turn.completed"})
    assert not parse_events("codex", output, SKILL)["skill_read"]


def test_claude_read_needs_tool_result():
    call = {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": SKILL}}]}}
    done = {"type": "result", "subtype": "success", "is_error": False}
    assert not parse_events("claude", stream(call, done), SKILL)["skill_read"]
    result = {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "r", "is_error": False}]}}
    assert parse_events("claude", stream(call, result, done), SKILL)["skill_read"]


def test_opencode_successful_read():
    output = stream({"type": "tool_use", "part": {"tool": "read", "state": {"status": "completed", "input": {"filePath": SKILL}}}},
                    {"type": "step_finish", "part": {"reason": "stop"}})
    assert parse_events("opencode", output, SKILL)["skill_read"]


def test_pi_successful_read():
    output = stream({"type": "tool_execution_start", "toolCallId": "1", "toolName": "read", "args": {"path": SKILL}},
                    {"type": "tool_execution_end", "toolCallId": "1", "isError": False},
                    {"type": "agent_end", "messages": []})
    assert parse_events("pi", output, SKILL)["skill_read"]


@pytest.mark.parametrize("agent", ["codex", "claude", "opencode", "pi"])
def test_fixed_model_effort_and_no_shell_injection(agent, tmp_path):
    p = Profile(id="test", agent=agent, model="chosen-model", effort="high", version="test", backend="simulation")
    prompt = "$(touch /tmp/do-not-create)"
    argv = command(p, tmp_path, prompt)
    assert "chosen-model" in argv
    assert prompt == argv[-1]
    assert not any("dangerously" in a for a in argv)



def test_newline_commands_are_independent():
    from agent_evolution.adapters import shell_commands
    parts = shell_commands("/bin/zsh -lc 'export PATH=/some/bin:$PATH\npython3 reproduce.py'")
    assert ['python3', 'reproduce.py'] in parts


def test_claude_bash_cat_read():
    call = {'type': 'assistant', 'message': {'content': [{'type': 'tool_use', 'id': 'b', 'name': 'Bash', 'input': {'command': f'cat {SKILL}'}}]}}
    result = {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'b', 'is_error': False}]}}
    done = {'type': 'result', 'subtype': 'success'}
    assert parse_events('claude', stream(call, result, done), SKILL)['skill_read']
