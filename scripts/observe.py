#!/usr/bin/env python3
"""Stable entry point for command hooks, without loading CLI/model dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_evolution.observer import main

if __name__ == "__main__":
    raise SystemExit(main())
