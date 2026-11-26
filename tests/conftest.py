import json
import sys
from pathlib import Path as _Path

def _ensure_observer_runtime_for_tests():
    """Write a gitignored observer-runtime.json so source tests can import the observer."""
    project = _Path(__file__).resolve().parents[1]
    skill = project / "skills/agent-evolution/SKILL.md"
    python = _Path(sys.executable).resolve()
    if not skill.is_file() or not python.is_file():
        return
    path = project / "observer-runtime.json"
    path.write_text(json.dumps({
        "version": 1,
        "python": str(python),
        "skill": str(skill),
    }) + "\n", encoding="utf-8")

_ensure_observer_runtime_for_tests()

import pytest

from agent_evolution.models import Experiment, Profile, Suite, Case, Check, Budget
from agent_evolution.store import Store
from agent_evolution.runner import create_run, Runner
from agent_evolution.util import atomic_write


@pytest.fixture
def experiment(tmp_path):
    skill = tmp_path / "skill"
    atomic_write(skill / "SKILL.md", "---\nname: sample\ndescription: Test fixture skill.\n---\n\nInvestigate the issue and preserve evidence.\n")
    atomic_write(skill / "references/info.md", "fixed reference\n")
    cases = [Case(id=f"case-{i}", group=f"group-{i}", split=split, request="Write report.json with answer=42.",
                  files={"app.txt": "unchanged"}, checks=[Check(kind="unchanged", path="app.txt", mandatory=True),
                                                         Check(kind="json_equals", key="answer", expected=42)])
             for i, split in enumerate(["train", "validation"] + ["holdout"] * 5)]
    suite = Suite(id="tests", reviewed=True, cases=cases)
    atomic_write(tmp_path / "suite.json", suite.model_dump_json())
    cfg = Experiment(skill=str(skill), suite=str(tmp_path / "suite.json"), targets=[
        Profile(id="sample", agent="codex", model="fake", effort="none", version="test", backend="simulation")])
    atomic_write(tmp_path / "config.json", cfg.model_dump_json())
    store = Store(tmp_path / "state")
    run = create_run(store, tmp_path / "config.json")
    return store, Runner(store, run)

