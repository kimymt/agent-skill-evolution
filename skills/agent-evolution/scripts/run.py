#!/usr/bin/env python3
"""Use the common CLI without duplicating its implementation in the skill."""
import os
import sys
from pathlib import Path


def resolve_project() -> Path:
    """Prefer the in-repo layout; keep the sibling agent-skills layout working."""
    parent = Path(__file__).resolve().parents[2]
    root = Path(__file__).resolve().parents[3]
    if parent.name == "skills" and (
        (root / "pyproject.toml").is_file() or (root / ".venv/bin/agent-evolution").is_file()
    ):
        return root
    sibling = root / "agent-evolution"
    if (sibling / "pyproject.toml").is_file() or (sibling / ".venv/bin/agent-evolution").is_file():
        return sibling
    sys.exit("agent-evolution is not installed; read the project README.md")


project = resolve_project()
entry = project / ".venv/bin/agent-evolution"
if not entry.is_file():
    sys.exit("agent-evolution is not installed; read the project README.md")
arguments = sys.argv[1:]
if "--state" not in arguments and not any(a.startswith("--state=") for a in arguments):
    arguments = ["--state", str(project / ".evolution"), *arguments]
os.execv(entry, [str(entry), *arguments])
