#!/usr/bin/env python3
"""Register full-folder skill links; never overwrite an existing entry."""
from pathlib import Path

project = Path(__file__).resolve().parents[1]
skill = project.parent / "agent-skills/agent-evolution"
if not (skill / "SKILL.md").is_file():
    raise SystemExit("Canonical skill is missing")
targets = [Path.home() / ".agents/skills/agent-evolution", Path.home() / ".claude/skills/agent-evolution"]
for target in targets:
    if target.exists() or target.is_symlink():
        if target.is_symlink() and target.resolve() == skill.resolve():
            continue
        raise SystemExit(f"Existing entry preserved: {target}")
for target in targets:
    if not target.is_symlink():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.symlink_to(skill, target_is_directory=True)
    print(f"{target} -> {skill}")


cli = Path.home() / ".local/bin/agent-evolution"
entry = project / ".venv/bin/agent-evolution"
if cli.exists() or cli.is_symlink():
    if not (cli.is_symlink() and cli.resolve() == entry.resolve()):
        raise SystemExit(f"Existing CLI entry preserved: {cli}")
else:
    cli.parent.mkdir(parents=True, exist_ok=True)
    cli.symlink_to(entry)
print(f"{cli} -> {entry}")
