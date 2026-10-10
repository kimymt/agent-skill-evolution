#!/usr/bin/env python3
"""Merge observer hooks and install JS entry points, preserving existing configuration."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from agent_evolution.util import atomic_write


def plan(home, agents):
    changes = []
    for agent in agents:
        if agent in {"codex", "claude"}:
            path = home / (".codex/hooks.json" if agent == "codex" else ".claude/settings.json")
            before = path.read_bytes() if path.exists() else None
            config = json.loads(before) if before else {}
            command = shlex.join([str(PROJECT / ".venv/bin/python"), str(PROJECT / "scripts/observe.py"), "--agent", agent, "hook"])
            events = ["UserPromptSubmit", "PostToolUse", "Stop"]
            events += ["Interrupt"] if agent == "codex" else ["PostToolUseFailure"]
            hooks = config.setdefault("hooks", {})
            for event in events:
                groups = hooks.setdefault(event, [])
                # Only replace entries owned by this installer, not verify-changes.
                retained = []
                for group in groups:
                    handlers = [h for h in group["hooks"] if str(PROJECT / "scripts/observe.py") not in h.get("command", "")]
                    if handlers:
                        retained.append({**group, "hooks": handlers})
                retained.append({"hooks": [{"type": "command", "command": command,
                                             "timeout": 3 if event == "Interrupt" else 10}]})
                hooks[event] = retained
            after = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
        else:
            path = home / (".pi/agent/extensions/agent-evolution.js" if agent == "pi" else ".config/opencode/plugins/agent-evolution.js")
            module = PROJECT / ("integrations/pi/index.js" if agent == "pi" else "integrations/opencode.js")
            before = path.read_bytes() if path.exists() else None
            marker = "// Managed by agent-evolution observer installer\n"
            if before and not before.decode().startswith(marker):
                raise ValueError(f"Existing extension preserved: {path}")
            after = (marker + f"export {{ default }} from {json.dumps(module.as_uri())};\n").encode()
        if before != after:
            changes.append((path, before, after))
    return changes


def install(home, agents, dry_run=False):
    changes = plan(home, agents)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    result = []
    # Check every collision/JSON document before the first write.
    for path, before, after in changes:
        backup = None
        if not dry_run:
            if (path.read_bytes() if path.exists() else None) != before:
                raise ValueError(f"Configuration changed while installing: {path}")
            if before is not None:
                backup = path.with_name(path.name + ".before-agent-evolution-" + timestamp)
                atomic_write(backup, before)
            atomic_write(path, after)
        result.append({"path": str(path), "backup": str(backup) if backup else None, "dry_run": dry_run})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--agents", default="codex,claude,pi,opencode")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    agents = args.agents.split(",")
    if any(a not in {"codex", "claude", "pi", "opencode"} for a in agents):
        parser.error("Unknown agent")
    print(json.dumps({"changes": install(args.home, agents, args.dry_run),
                      "codex_trust": "Review the new definitions with /hooks; trust records were not changed"}, indent=2))


if __name__ == "__main__":
    main()
