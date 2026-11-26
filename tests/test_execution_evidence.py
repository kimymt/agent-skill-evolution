"""Synthetic execution receipts; no agent or model is launched by these tests."""
import json
from pathlib import Path
import shlex

import pytest

from agent_evolution import runner as runner_module
from agent_evolution.adapters import parse_events, shell_commands
from agent_evolution.evaluators import evaluate
from agent_evolution.models import Case, Check
from agent_evolution.process import ProcessResult
from agent_evolution.util import EvolutionError


SKILL = "/workspace/.agents/skills/fixture/SKILL.md"
CONTENT = "---\nname: fixture\ndescription: Public test skill.\n---\n\nRead the whole procedure.\n"
CAT = "/bin/cat " + SKILL


def stream(*events):
    return "\n".join(map(json.dumps, events))


def codex(command=CAT, output=CONTENT, **metadata):
    return stream({"type": "item.completed", "item": {"type": "command_execution",
        "command": command, "aggregated_output": output, "exit_code": 0, "status": "completed", **metadata}},
        {"type": "turn.completed"})


def trace(agent, output):
    return parse_events(agent, output, SKILL, skill_text=CONTENT)


@pytest.mark.parametrize("command", [
    CAT,
    "/usr/bin/cat " + SKILL,
    "/bin/sh -c " + shlex.quote(CAT),
    "/bin/bash -c " + shlex.quote(CAT),
    "/bin/zsh -c " + shlex.quote(CAT),
])
def test_literal_absolute_cat_and_trusted_nonlogin_shell_can_prove_full_read(command):
    assert trace("codex", codex(command))["skill_read"]


@pytest.mark.parametrize("command", [
    "/bin/sh -c " + shlex.quote(CAT) + " || true",
    "/bin/sh -c " + shlex.quote(CAT) + "; true",
    "/bin/sh -c " + shlex.quote(CAT) + " > /dev/null",
    "/bin/sh -c " + shlex.quote(CAT) + " ignored-argument",
    "/bin/zsh -lc " + shlex.quote(CAT),
    "./zsh -c " + shlex.quote(CAT),
    "/workspace/zsh -c " + shlex.quote(CAT),
    "sh -c " + shlex.quote(CAT),
    "export PATH=.; " + CAT,
    "PATH=. " + CAT,
    CAT + "\ntrue",
    CAT + " # ignored comment",
    "true && " + CAT,
    "false || " + CAT,
    CAT + " | /bin/cat",
    "/bin/cat $SKILL",
    "/bin/cat `printf ignored`",
    "/bin/cat $(printf ignored)",
])
def test_ambiguous_shell_forms_are_not_recognized_even_with_copied_skill_output(command):
    assert shell_commands(command) == []
    assert not trace("codex", codex(command))["skill_read"]


@pytest.mark.parametrize("command", [
    "cat " + SKILL,
    "./cat " + SKILL,
    "/workspace/bin/cat " + SKILL,
    "/bin/cat /different-root" + SKILL,
    "/bin/cat .agents/skills/fixture/SKILL.md",
    "/bin/cat -- " + SKILL,
    "/bin/cat --help " + SKILL,
    CAT + " /another/file",
])
def test_cat_binary_and_only_file_argument_must_match_exactly(command):
    assert not trace("codex", codex(command))["skill_read"]


@pytest.mark.parametrize("output", ["", CONTENT[:20], CONTENT.rstrip(), CONTENT + "extra text"])
def test_successful_cat_without_exact_full_output_is_not_read_evidence(output):
    assert not trace("codex", codex(output=output))["skill_read"]


def test_no_expected_skill_text_means_no_read_evidence():
    assert not parse_events("codex", codex(), SKILL)["skill_read"]


@pytest.mark.parametrize("metadata", [
    {"status": "failed"}, {"status": "in_progress"}, {"status": "declined"}, {"status": None},
    {"exit_code": False}, {"exit_code": "0"}, {"exit_code": 0.0}, {"exit_code": None}, {"exit_code": 1},
])
def test_codex_requires_explicit_completed_status_and_integer_zero_exit(metadata):
    result = trace("codex", codex(**metadata))
    assert not result["skill_read"] and result["commands"] == []


def read_events(agent, path=SKILL, inputs=None, content=CONTENT, result_id="read1", success=False):
    inputs = inputs or {}
    if agent == "claude":
        full_text = content if isinstance(content, str) else "".join(block["text"] for block in content)
        count = CONTENT.count("\n") + 1
        return stream({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "read1",
            "name": "Read", "input": {"file_path": path, **inputs}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": result_id,
                "is_error": success, "content": content}]},
             "tool_use_result": {"type": "text", "file": {"filePath": path, "content": full_text,
                "startLine": 1, "numLines": count, "totalLines": count, "truncatedByTokenCap": False}}},
            {"type": "result", "subtype": "success", "is_error": False})
    if agent == "opencode":
        return stream({"type": "tool_use", "part": {"id": "read1", "tool": "read", "state": {
            "status": "completed", "input": {"filePath": path, **inputs}, "output": content}}},
            {"type": "step_finish", "part": {"reason": "stop"}})
    return stream({"type": "tool_execution_start", "toolCallId": "read1", "toolName": "read",
                   "args": {"path": path, **inputs}},
                  {"type": "tool_execution_end", "toolCallId": result_id, "isError": success,
                   "result": {"content": content}},
                  {"type": "agent_end", "messages": []})


@pytest.mark.parametrize("agent", ["claude", "opencode", "pi"])
def test_read_tool_with_exact_path_and_complete_content_is_evidence(agent):
    # Pi tool results conventionally use text blocks; other adapters accept strings.
    content = [{"type": "text", "text": CONTENT}] if agent == "pi" else CONTENT
    assert trace(agent, read_events(agent, content=content))["skill_read"]


@pytest.mark.parametrize("agent", ["claude", "opencode", "pi"])
@pytest.mark.parametrize("bad_read", ["suffix", "relative", "offset", "limit", "pages", "empty", "partial"])
def test_read_tool_rejects_wrong_path_partial_request_or_incomplete_content(agent, bad_read):
    path, inputs, content = SKILL, {}, CONTENT
    if bad_read == "suffix":
        path = "/different-root" + SKILL
    elif bad_read == "relative":
        path = SKILL.removeprefix("/workspace/")
    elif bad_read in {"offset", "limit", "pages"}:
        inputs[bad_read] = 1
    elif bad_read == "empty":
        content = ""
    else:
        content = CONTENT[:20]
    if agent == "pi":
        content = [{"type": "text", "text": content}]
    assert not trace(agent, read_events(agent, path, inputs, content))["skill_read"]


@pytest.mark.parametrize("agent,bad_result", [
    (agent, bad_result) for agent in ("claude", "pi")
    for bad_result in ("unknown_id", "error", "non_boolean_success", "missing_success")
    if not (agent == "claude" and bad_result == "missing_success")
])
def test_correlated_read_result_rejects_invalid_success_flag_or_id(agent, bad_result):
    content = [{"type": "text", "text": CONTENT}]
    output = read_events(agent, content=content, result_id="unmatched" if bad_result == "unknown_id" else "read1",
                         success=True if bad_result == "error" else 0 if bad_result == "non_boolean_success" else False)
    if bad_result == "missing_success":
        records = [json.loads(line) for line in output.splitlines()]
        if agent == "claude":
            del records[1]["message"]["content"][0]["is_error"]
        else:
            del records[1]["isError"]
        output = stream(*records)
    assert not trace(agent, output)["skill_read"]


def test_claude_optional_error_flag_can_be_absent_on_successful_read():
    records = [json.loads(line) for line in read_events("claude").splitlines()]
    del records[1]["message"]["content"][0]["is_error"]
    assert trace("claude", stream(*records))["skill_read"]


def test_claude_read_uses_verified_structured_content_despite_display_line_numbers():
    records = [json.loads(line) for line in read_events("claude").splitlines()]
    records[1]["message"]["content"][0]["content"] = "\n".join(
        f"{number}\t{line}" for number, line in enumerate(CONTENT.splitlines(), 1))
    assert trace("claude", stream(*records))["skill_read"]


def test_claude_optional_truncation_flag_can_be_absent_on_full_read():
    records = [json.loads(line) for line in read_events("claude").splitlines()]
    del records[1]["tool_use_result"]["file"]["truncatedByTokenCap"]
    assert trace("claude", stream(*records))["skill_read"]


@pytest.mark.parametrize("bad_metadata", [
    "wrong_path", "start_after_first", "short_range", "longer_file", "boolean_start", "boolean_num_lines",
    "boolean_total_lines", "truncated", "missing_metadata", "multiple_results", "wrong_content", "trimmed_content",
])
def test_claude_read_requires_complete_correlated_structured_file_metadata(bad_metadata):
    records = [json.loads(line) for line in read_events("claude").splitlines()]
    result = records[1]
    file = result["tool_use_result"]["file"]
    if bad_metadata == "wrong_path":
        file["filePath"] = "/different-root" + SKILL
    elif bad_metadata == "start_after_first":
        file["startLine"] = 2
    elif bad_metadata == "short_range":
        file["numLines"] -= 1
    elif bad_metadata == "longer_file":
        file["totalLines"] += 1
    elif bad_metadata == "boolean_start":
        file["startLine"] = True
    elif bad_metadata == "boolean_num_lines":
        file["numLines"] = True
    elif bad_metadata == "boolean_total_lines":
        file["totalLines"] = True
    elif bad_metadata == "truncated":
        file["truncatedByTokenCap"] = True
    elif bad_metadata == "missing_metadata":
        del result["tool_use_result"]
    elif bad_metadata == "multiple_results":
        result["message"]["content"].append({"type": "tool_result", "tool_use_id": "other",
                                            "is_error": False, "content": "other output"})
    elif bad_metadata == "wrong_content":
        file["content"] = "different file content"
    else:
        file["content"] = CONTENT.rstrip()
    assert not trace("claude", stream(*records))["skill_read"]


def bash_events(agent, exit_code=0, command=CAT):
    if agent == "opencode":
        return stream({"type": "tool_use", "part": {"tool": "bash", "state": {"status": "completed",
            "input": {"command": command}, "output": CONTENT, "metadata": {"exit": exit_code}}}},
            {"type": "step_finish", "part": {"reason": "stop"}})
    return stream({"type": "tool_execution_start", "toolCallId": "bash1", "toolName": "bash", "args": {"command": command}},
                  {"type": "tool_execution_end", "toolCallId": "bash1", "isError": False,
                   "result": {"content": [{"type": "text", "text": CONTENT}],
                              "structuredContent": {"exit_code": exit_code}}},
                  {"type": "agent_end", "messages": []})


@pytest.mark.parametrize("agent", ["opencode", "pi"])
@pytest.mark.parametrize("exit_code", [1, None, False, "0"])
def test_bash_result_requires_integer_zero_exit_for_read_or_command_evidence(agent, exit_code):
    result = trace(agent, bash_events(agent, exit_code))
    assert not result["skill_read"] and result["commands"] == []


@pytest.mark.parametrize("agent", ["opencode", "pi"])
def test_bash_result_with_zero_exit_and_exact_content_is_evidence(agent):
    assert trace(agent, bash_events(agent))["skill_read"]


def claude_bash(inputs=None, result=None):
    return stream({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "bash1",
        "name": "Bash", "input": {"command": CAT, **(inputs or {})}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "bash1",
            "is_error": False, "content": CONTENT}]},
         "tool_use_result": {"stdout": CONTENT, "stderr": "", "interrupted": False, **(result or {})}},
        {"type": "result", "subtype": "success", "is_error": False})


def test_claude_foreground_bash_with_completed_metadata_can_prove_read():
    assert trace("claude", claude_bash())["skill_read"]


@pytest.mark.parametrize("inputs,result", [
    ({"run_in_background": True}, {}),
    ({}, {"backgroundTaskId": "running-task"}),
    ({}, {"backgroundedByUser": True}),
    ({}, {"interrupted": True}),
])
def test_claude_background_or_interrupted_bash_is_not_completed_evidence(inputs, result):
    parsed = trace("claude", claude_bash(inputs, result))
    assert not parsed["skill_read"] and parsed["commands"] == []


@pytest.mark.parametrize("command", [
    "/bin/sh -c 'python3 reproduce.py' || true",
    "/bin/sh -c 'python3 reproduce.py'; true",
    "/bin/sh -c 'python3 reproduce.py' extra",
    "export PATH=.; python3 reproduce.py",
])
def test_command_observed_cannot_be_satisfied_by_hidden_failure(command, tmp_path):
    case = Case(id="evidence", group="evidence", split="train", request="public request", files={},
                checks=[Check(kind="command_observed", expected="python3 reproduce.py", mandatory=True)])
    result = evaluate(case, tmp_path, trace("codex", codex(command=command)))
    assert not result["passed"] and not result["hard_pass"] and result["score"] == 0


@pytest.mark.parametrize("receipt", ["valid", "valid_shell", "outer_tail", "fake_cat", "wrong_path", "empty_output", "partial_output"])
def test_runner_requires_unambiguous_full_skill_read_before_recording_pass(experiment, monkeypatch, receipt):
    store, runner = experiment
    profile = runner.config.targets[0].model_copy(update={"backend": "codex-native"})
    monkeypatch.setattr(runner, "check_environment", lambda profile: {"scanner": {"resolved": "/fixture/unused-scanner"}})
    monkeypatch.setattr(runner, "scan", lambda *args: None)
    monkeypatch.setattr(runner_module, "scan_values", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_module, "scan_streams", lambda *args, **kwargs: None)
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        expected = str(skill_folder / "SKILL.md")
        command = "/bin/cat " + shlex.quote(expected)
        assert command in prompt
        output = (skill_folder / "SKILL.md").read_text()
        if receipt in {"valid_shell", "outer_tail"}:
            command = "/bin/sh -c " + shlex.quote(command)
        if receipt == "outer_tail":
            command += " || true"
        elif receipt == "fake_cat":
            command = "./cat " + shlex.quote(expected)
        elif receipt == "wrong_path":
            command = "/bin/cat " + shlex.quote("/different-root" + expected)
        elif receipt == "empty_output":
            output = ""
        elif receipt == "partial_output":
            output = output[:20]
        (work / "report.json").write_text('{"answer":42}')
        return ProcessResult("completed", 0, codex(command, output), "", 0.01)
    monkeypatch.setattr(runner_module, "run_agent", transport)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    valid = receipt in {"valid", "valid_shell"}
    assert result["passed"] is valid and result["skill_read"] is valid
    assert result["status"] == ("completed" if valid else "infrastructure_error")
    assert (runner.path / f"{result['key']}.trace.json").exists() is valid
    assert (runner.path / f"{result['key']}.outputs.json").exists() is valid
    assert len(store.evaluations(runner.id)) == 1


def test_docker_receipt_uses_container_absolute_skill_path(experiment, monkeypatch):
    _, runner = experiment
    profile = runner.config.targets[0].model_copy(update={"backend": "docker"})
    monkeypatch.setattr(runner, "check_environment", lambda profile: {"scanner": {"resolved": "/fixture/unused-scanner"}})
    monkeypatch.setattr(runner, "scan", lambda *args: None)
    monkeypatch.setattr(runner_module, "scan_values", lambda *args, **kwargs: None)
    monkeypatch.setattr(runner_module, "scan_streams", lambda *args, **kwargs: None)
    def transport(profile, work, prompt, skill_folder, *args, **kwargs):
        protected = "/workspace/" + (skill_folder / "SKILL.md").relative_to(work).as_posix()
        command = "/bin/cat " + shlex.quote(protected)
        assert command in prompt
        (work / "report.json").write_text('{"answer":42}')
        return ProcessResult("completed", 0, codex(command, (skill_folder / "SKILL.md").read_text()), "", 0.01)
    monkeypatch.setattr(runner_module, "run_agent", transport)
    result = runner.trial(runner.data["baseline"], runner.cases("train")[0], profile)
    assert result["status"] == "completed" and result["passed"] and result["skill_read"]


@pytest.mark.parametrize("command", [
    "/bin/zsh -c '/usr/bin/printf %s =cat'",
    "/bin/zsh -c '/usr/bin/printf %s \"=cat\"'",
    "time python3 reproduce.py",
    "noglob python3 reproduce.py",
])
def test_shell_expansion_and_reserved_prefixes_are_not_literal_evidence(command):
    assert shell_commands(command) == []
