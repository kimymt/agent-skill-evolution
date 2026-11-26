#!/usr/bin/env python3
"""Stable entry point for command hooks, without loading CLI/model dependencies."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_evolution.util import EvolutionError

try:
    from agent_evolution.observer import main
except EvolutionError:
    print("agent-evolution: [E_RUNTIME_CONFIG] 終了監視: 監視用ランタイムの設定がないか不正です。"
          "配布物を確認してください。自動起動は行っていません。 no model was called", file=sys.stderr)
    raise SystemExit(2) from None

if __name__ == "__main__":
    raise SystemExit(main())
