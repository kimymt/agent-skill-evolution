import os
import sys

import pytest

from agent_evolution.process import execute
from agent_evolution.util import EvolutionError


def test_process_accepts_validated_cancel_callback(tmp_path):
    calls = []
    def cancelled():
        calls.append(True)
        return True
    result = execute([sys.executable, "-c", "import time;time.sleep(10)"], tmp_path,
                     timeout=2, cancel=cancelled)
    assert result.status == "cancelled"
    assert calls and result.seconds < 2


def test_unsafe_cancellation_check_stops_child_and_propagates(tmp_path, monkeypatch):
    import agent_evolution.process as process
    original = process.subprocess.Popen
    children = []
    def capture(*args, **kwargs):
        child = original(*args, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(process.subprocess, "Popen", capture)
    def invalid():
        raise EvolutionError("Unsafe state fixture")
    with pytest.raises(EvolutionError, match="Unsafe state"):
        execute([sys.executable, "-c", "import time;time.sleep(10)"], tmp_path,
                timeout=2, cancel=invalid)
    assert len(children) == 1 and children[0].poll() is not None
    with pytest.raises(ProcessLookupError):
        os.kill(children[0].pid, 0)
