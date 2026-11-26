from __future__ import annotations

import json
import sys
import os
import re
import shutil
import shlex
from pathlib import Path

from .models import AGENTS, Profile
from .process import execute
from .util import EvolutionError, canonical


def shell_commands(command_text: str) -> list[list[str]]:
    """Accept one literal command, optionally inside an exact system-shell -c.

    This is a deliberately restricted evidence format, not a shell interpreter.
    Even quoted expansions, operators and glob characters are unsupported.
    """
    if not isinstance(command_text, str) or any(c in command_text for c in
            ";&|()<>\n\r\x00$`\\#*?[]{}!~"):
        return []
    try:
        words = shlex.split(command_text)
        shells = {"sh", "bash", "zsh"}
        if words and Path(words[0]).name in shells:
            if len(words) != 3 or words[0] not in {"/bin/sh", "/bin/bash", "/bin/zsh"} or words[1] != "-c":
                return []
            words = shlex.split(words[2])
        if not words or "=" in words[0] or words[0].startswith("-") or any(w.startswith("=") for w in words):
            return []
        if Path(words[0]).name in shells or words[0] in {
                "export", "unset", "env", "cd", "exec", "command", "builtin", "source", ".",
                "if", "then", "else", "fi", "for", "while", "until", "do", "done", "case", "esac",
                "time", "coproc", "noglob", "nocorrect", "function", "select", "repeat"}:
            return []
        return [words]
    except ValueError:
        return []


def probe(agent: str) -> dict:
    if agent not in AGENTS:
        raise EvolutionError("Unknown agent adapter")
    binary = shutil.which(agent)
    if not binary:
        return {"agent": agent, "available": False, "reason": "not_on_path"}
    r = execute([binary, "--version"], Path.cwd(), 10, 10000)
    version = re.search(r"\b\d+\.\d+\.\d+\b", r.stdout)
    return {"agent": agent, "available": r.status == "completed" and bool(version),
            "version": version[0] if version else None, "binary": binary, "status": r.status}


def evaluation_python_bindir() -> Path:
    return Path(sys.executable).resolve().parent


def evaluation_python_read_root() -> Path:
    """Filesystem root Codex may read so the evaluating Python can start."""
    parts = Path(sys.executable).resolve().parts
    if "Python.framework" in parts:
        return Path(*parts[: parts.index("Python.framework") + 1])
    return Path(sys.executable).resolve().parent


def codex_permissions(work: Path) -> list[str]:
    permissions = (
        '{evolution={filesystem={":root"="deny",":minimal"="read",":workspace_roots"='
        '{"."="write",".agents"="read",".git"="read"},":tmpdir"="deny",":slash_tmp"="deny",'
        + json.dumps(str(evaluation_python_read_root()))
        + '="read"},network={enabled=false}}}'
    )
    values = {
        "default_permissions": '"evolution"',
        "permissions": permissions,
        "approval_policy": '"never"',
        "shell_environment_policy.inherit": '"none"',
        "shell_environment_policy.set.PATH": json.dumps(
            str(evaluation_python_bindir()) + ":/usr/bin:/bin:/usr/sbin:/sbin"
        ),
        "web_search": '"disabled"',
        "features.hooks": "false", "features.memories": "false",
        "features.multi_agent": "false", "features.plugins": "false",
        "features.remote_plugin": "false", "features.apps": "false",
        "features.js_repl": "false", "features.code_mode": "false",
        "features.unified_exec": "true",
        "project_doc_max_bytes": "0",
    }
    disabled = []
    for location in (Path.home() / ".agents/skills", Path.home() / ".codex/skills"):
        if location.exists():
            for entry in sorted(location.iterdir()):
                if (entry / "SKILL.md").is_file():
                    disabled.append('{path=' + json.dumps(str(entry)) + ',enabled=false}')
    values["skills.config"] = "[" + ",".join(disabled) + "]"
    return [arg for k, v in values.items() for arg in ("-c", f"{k}={v}")]


def _model_args(flag: str, profile: Profile) -> list[str]:
    if profile.model is None:
        return []
    return [flag, profile.model]


def command(profile: Profile, work: Path, prompt: str) -> list[str]:
    if profile.agent not in AGENTS:
        raise EvolutionError("Unknown agent adapter")
    if profile.agent == "codex":
        args = ["codex", "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--strict-config", *_model_args("-m", profile),
                "-c", "model_reasoning_effort=" + json.dumps(profile.effort)]
        if profile.backend == "codex-native":
            args += codex_permissions(work)
        else:
            args += ["--sandbox", "workspace-write", "-c", 'approval_policy="never"']
        return args + [prompt]
    if profile.agent == "claude":
        return ["claude", "-p", "--output-format", "stream-json", "--verbose", *_model_args("--model", profile),
                "--effort", profile.effort, "--setting-sources", "project", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}', "--allowedTools", "Read,Write,Edit,Bash", prompt]
    if profile.agent == "opencode":
        return ["opencode", "run", "--format", "json", *_model_args("--model", profile), "--variant", profile.effort, prompt]
    if profile.agent == "pi":
        return ["pi", "--mode", "json", "-p", "--no-session", "--no-extensions", "--no-prompt-templates",
                "--no-context-files", *_model_args("--model", profile), "--thinking", profile.effort, prompt]
    raise EvolutionError("Unknown agent adapter")


def _text_content(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list) and all(isinstance(p, dict) and p.get("type") == "text"
                                        and isinstance(p.get("text"), str) for p in content):
        return "".join(p["text"] for p in content)
    return None


def _zero_exit(value) -> bool:
    # bool is an int subclass; False must never stand in for an exit code.
    return type(value) is int and value == 0


def parse_events(agent: str, output: str, skill_path: str, *, skill_text: str | None = None) -> dict:
    try:
        return _parse_events(agent, output, skill_path, skill_text)
    except (TypeError, AttributeError, KeyError, ValueError):
        raise EvolutionError("Invalid agent event schema; evaluation evidence rejected") from None


def _parse_events(agent: str, output: str, skill_path: str, skill_text: str | None) -> dict:
    if agent not in AGENTS:
        raise EvolutionError("Unknown agent adapter")
    events, commands, reads, text, terminal = [], [], [], [], False
    usage, cost = None, None
    pending_tools, seen_tools = {}, set()

    def full_skill(content):
        return (isinstance(skill_text, str) and bool(skill_text) and Path(skill_path).is_absolute()
                and _text_content(content) == skill_text)

    def read_tool(inp, path_key, content):
        if set(inp) == {path_key} and inp[path_key] == skill_path and full_skill(content):
            reads.append(skill_path)

    def completed_command(cmd, content):
        parts = shell_commands(cmd)
        if not parts:
            return
        commands.append(cmd)
        if len(parts[0]) == 2 and parts[0][0] in {"/bin/cat", "/usr/bin/cat"} and parts[0][1] == skill_path and full_skill(content):
            reads.append(skill_path)

    def start_tool(tool_id, name, inp):
        if not isinstance(tool_id, str) or not tool_id or tool_id in seen_tools or not isinstance(inp, dict):
            raise EvolutionError("Invalid or duplicate tool identity; evaluation evidence rejected")
        seen_tools.add(tool_id)
        pending_tools[tool_id] = (name, inp)

    for line in output.splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except ValueError:
            raise EvolutionError("Invalid JSON event stream") from None
        if not isinstance(e, dict):
            raise EvolutionError("Invalid JSON event record")
        kind = e.get("type")
        # Never persist private reasoning events in the normalized trace.
        if kind not in {"thinking_delta", "reasoning"}:
            events.append(kind)
        if agent == "codex":
            if kind == "turn.completed":
                terminal = True
                usage = e.get("usage")
            if kind in {"error", "turn.failed"}:
                raise EvolutionError("Agent reported a failed turn")
            item = e.get("item", {})
            if kind == "item.completed" and item.get("type") == "agent_message":
                text.append(item.get("text", ""))
            if (kind == "item.completed" and item.get("type") == "command_execution"
                    and item.get("status") == "completed" and _zero_exit(item.get("exit_code"))):
                completed_command(item.get("command"), item.get("aggregated_output"))
        elif agent == "claude":
            if kind == "result":
                terminal = e.get("is_error", False) is False and e.get("subtype") == "success"
                text.append(e.get("result", ""))
                usage, cost = e.get("usage"), e.get("total_cost_usd")
            parts = e.get("message", {}).get("content", [])
            for part in parts:
                if kind == "assistant" and part.get("type") == "tool_use":
                    start_tool(part["id"], part.get("name"), part.get("input", {}))
                if kind == "user" and part.get("type") == "tool_result":
                    name, inp = pending_tools.pop(part.get("tool_use_id"), (None, {}))
                    # Anthropic's tool_result schema permits omission on success.
                    if part.get("is_error", False) is not False:
                        continue
                    if name == "Read" and len(parts) == 1:
                        result = e.get("tool_use_result", {})
                        file = result.get("file", {})
                        # Claude decorates tool_result content with line numbers.
                        # Its structured text result preserves the returned body.
                        line_count = skill_text.count("\n") + 1 if isinstance(skill_text, str) else None
                        if (result.get("type") == "text" and file.get("filePath") == skill_path
                                and type(file.get("startLine")) is int and file["startLine"] == 1
                                and type(file.get("numLines")) is int and file["numLines"] == line_count
                                and type(file.get("totalLines")) is int and file["totalLines"] == line_count
                                and file.get("truncatedByTokenCap", False) is False):
                            read_tool(inp, "file_path", file.get("content"))
                    if name == "Bash":
                        result = e.get("tool_use_result", {})
                        # A non-error background launch is not completed execution.
                        # Require one result so this metadata cannot bind to another tool.
                        if (len(parts) == 1 and inp.get("run_in_background", False) is False
                                and isinstance(result, dict) and result.get("interrupted") is False
                                and result.get("backgroundTaskId") is None
                                and result.get("backgroundedByUser", False) is False
                                and result.get("isImage", False) is False
                                and result.get("returnCodeInterpretation") is None
                                and isinstance(result.get("stdout"), str) and isinstance(result.get("stderr"), str)):
                            completed_command(inp.get("command"), result["stdout"])
        elif agent == "opencode":
            part = e.get("part", {})
            if kind == "finish" or (kind == "step_finish" and part.get("reason") == "stop"):
                terminal = True
                usage, cost = part.get("tokens"), part.get("cost")
            if kind == "text":
                text.append(part.get("text", ""))
            if kind == "tool_use" and part.get("state", {}).get("status") == "completed":
                state = part["state"]
                inp = state.get("input", {})
                if part.get("tool") == "read":
                    read_tool(inp, "filePath", state.get("output"))
                if part.get("tool") == "bash" and _zero_exit(state.get("metadata", {}).get("exit")):
                    completed_command(inp.get("command"), state.get("output"))
        else:
            if kind == "agent_end":
                terminal = True
                for message in e.get("messages", []):
                    if message.get("role") == "assistant":
                        if message.get("stopReason") in {"error", "aborted"}:
                            terminal = False
                        usage = message.get("usage", usage)
                        text.extend(p.get("text", "") for p in message.get("content", []) if p.get("type") == "text")
            if kind == "tool_execution_start":
                start_tool(e.get("toolCallId"), e.get("toolName"), e.get("args", {}))
            if kind == "tool_execution_end":
                name, inp = pending_tools.pop(e.get("toolCallId"), (None, {}))
                if e.get("isError") is not False:
                    continue
                result = e.get("result", {})
                if name == "read":
                    read_tool(inp, "path", result.get("content"))
                if name == "bash" and _zero_exit(result.get("structuredContent", {}).get("exit_code")):
                    completed_command(inp.get("command"), result.get("content"))
    if not terminal:
        raise EvolutionError("Missing successful terminal event")
    return {"events": events, "commands": commands, "skill_read": bool(reads),
            "text": "\n".join(text), "usage": usage, "cost": cost,
            "cost_status": "reported" if cost is not None else "unknown"}
