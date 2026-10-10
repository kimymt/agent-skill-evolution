#!/usr/bin/env python3
"""Use the common CLI without duplicating its implementation in the skill."""
import os
import sys
from pathlib import Path

project = Path(__file__).resolve().parents[3] / "agent-evolution"
entry = project / ".venv/bin/agent-evolution"
if not entry.is_file():
    sys.exit("agent-evolution is not installed; read the project README.md")
arguments = sys.argv[1:]
if "--state" not in arguments and not any(a.startswith("--state=") for a in arguments):
    arguments = ["--state", str(project / ".evolution"), *arguments]
os.execv(entry, [str(entry), *arguments])

