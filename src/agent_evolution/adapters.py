from __future__ import annotations

import json
import os
import re
import shutil
import shlex
from pathlib import Path

from .models import Profile
from .process import execute
from .util import EvolutionError, canonical


def shell_commands(command_text: str) -> list[list[str]]:
    """Recognize simple shell invocations, not text quoted by echo/printf."""
    try:
        words = shlex.split(command_text)
        if len(words) >= 3 and Path(words[0]).name in {"sh", "bash", "zsh"} and words[1] in {"-c", "-lc"}:
            command_text = words[2]
        lexer = shlex.shlex(command_text, posix=True, punctuation_chars=";&|()<>\n")
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        result, part = [], []
        for word in lexer:
            if word and all(c in ";&|()<>\n" for c in word):
                if any(c not in ";\n" for c in word):
                    return []
                if part:
                    result.append(part)
                part = []
            else:
                part.append(word)
        if part:
            result.append(part)
        # A shell's exit code proves only its last command succeeded. Accept
        # simple invocations, optionally preceded by PATH-style exports.
        if any(parts[0] != "export" or any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=[^`]*", x) for x in parts[1:])
               for parts in result[:-1]):
            return []
        if any("$(" in x or "`" in x for parts in result for x in parts):
            return []
        return result[-1:]
    except ValueError:
        return []


def probe(agent: str) -> dict:
    binary = shutil.which(agent)
    if not binary:
        return {"agent": agent, "available": False, "reason": "not_on_path"}
    r = execute([binary, "--version"], Path.cwd(), 10, 10000)
    version = re.search(r"\b\d+\.\d+\.\d+\b", r.stdout)
    return {"agent": agent, "available": r.status == "completed" and bool(version),
            "version": version[0] if version else None, "binary": binary, "status": r.status}


def codex_permissions(work: Path) -> list[str]:
    permissions = '{evolution={filesystem={":root"="deny",":minimal"="read",":workspace_roots"={"."="write",".agents"="read",".git"="read"},":tmpdir"="deny",":slash_tmp"="deny","/Library/Frameworks/Python.framework"="read"},network={enabled=false}}}'
    values = {
        "default_permissions": '"evolution"',
        "permissions": permissions,
        "approval_policy": '"never"',
        "shell_environment_policy.inherit": '"none"',
        "shell_environment_policy.set.PATH": '"/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/bin:/bin:/usr/sbin:/sbin"',
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


def command(profile: Profile, work: Path, prompt: str) -> list[str]:
    if profile.agent == "codex":
        args = ["codex", "exec", "--json", "--ephemeral", "--ignore-user-config", "--ignore-rules",
                "--skip-git-repo-check", "--strict-config", "-m", profile.model,
                "-c", "model_reasoning_effort=" + json.dumps(profile.effort)]
        if profile.backend == "codex-native":
            args += codex_permissions(work)
        else:
            args += ["--sandbox", "workspace-write", "-c", 'approval_policy="never"']
        return args + [prompt]
    if profile.agent == "claude":
        return ["claude", "-p", "--output-format", "stream-json", "--verbose", "--model", profile.model,
                "--effort", profile.effort, "--setting-sources", "project", "--strict-mcp-config",
                "--mcp-config", '{"mcpServers":{}}', "--allowedTools", "Read,Write,Edit,Bash", prompt]
    if profile.agent == "opencode":
        return ["opencode", "run", "--format", "json", "--model", profile.model, "--variant", profile.effort, prompt]
    return ["pi", "--mode", "json", "-p", "--no-session", "--no-extensions", "--no-prompt-templates",
            "--no-context-files", "--model", profile.model, "--thinking", profile.effort, prompt]


def parse_events(agent: str, output: str, skill_path: str) -> dict:
    events, commands, reads, text, terminal = [], [], [], [], False
    usage, cost = None, None
    pending_tools = {}
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
            if kind == "item.completed" and item.get("type") == "command_execution" and item.get("exit_code") == 0:
                cmd = item.get("command", "")
                commands.append(cmd)
                if any(Path(parts[0]).name == "cat" and any(x == skill_path or x.endswith("/" + skill_path) for x in parts[1:]) for parts in shell_commands(cmd)):
                    reads.append(skill_path)
        elif agent == "claude":
            if kind == "result":
                terminal = not e.get("is_error", False) and e.get("subtype") == "success"
                text.append(e.get("result", ""))
                usage, cost = e.get("usage"), e.get("total_cost_usd")
            for part in e.get("message", {}).get("content", []):
                if part.get("type") == "tool_use":
                    pending_tools[part["id"]] = (part.get("name"), part.get("input", {}))
                if part.get("type") == "tool_result" and not part.get("is_error", False):
                    name, inp = pending_tools.get(part.get("tool_use_id"), (None, {}))
                    if name == "Read" and inp.get("file_path", "").endswith(skill_path):
                        reads.append(skill_path)
                    if name == "Bash":
                        commands.append(inp.get("command", ""))
        elif agent == "opencode":
            part = e.get("part", {})
            if kind == "finish" or (kind == "step_finish" and part.get("reason") == "stop"):
                terminal = True
                usage, cost = part.get("tokens"), part.get("cost")
            if kind == "text":
                text.append(part.get("text", ""))
            if kind == "tool_use" and part.get("state", {}).get("status") == "completed":
                inp = part["state"].get("input", {})
                if part.get("tool") == "read" and inp.get("filePath", "").endswith(skill_path):
                    reads.append(skill_path)
                if part.get("tool") == "bash":
                    commands.append(inp.get("command", ""))
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
                pending_tools[e.get("toolCallId")] = (e.get("toolName"), e.get("args", {}))
            if kind == "tool_execution_end" and not e.get("isError", False):
                name, inp = pending_tools.get(e.get("toolCallId"), (None, {}))
                if name == "read" and inp.get("path", "").endswith(skill_path):
                    reads.append(skill_path)
                if name == "bash":
                    commands.append(inp.get("command", ""))
    if not terminal:
        raise EvolutionError("Missing successful terminal event")
    if any(Path(parts[0]).name == "cat" and any(x == skill_path or x.endswith("/" + skill_path) for x in parts[1:])
           for cmd in commands for parts in shell_commands(cmd)):
        reads.append(skill_path)
    return {"events": events, "commands": commands, "skill_read": bool(reads),
            "text": "\n".join(text), "usage": usage, "cost": cost,
            "cost_status": "reported" if cost is not None else "unknown"}
