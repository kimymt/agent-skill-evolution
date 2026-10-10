"""Deterministic offline demonstration. These results can never be promoted."""
from pathlib import Path

from .models import Suite, Case, Check, Experiment, Profile, Gates
from .runner import create_run, Runner
from .reports import report
from .util import atomic_write


def run_demo(store):
    root = store.root / "demo-input"
    skill = root / "demo-skill"
    atomic_write(skill / "SKILL.md", "---\nname: demo-skill\ndescription: Demonstrate the evaluation pipeline.\n---\n\nInvestigate the input.\n")
    suite = Suite(id="offline-demo", reviewed=True, cases=[
        Case(id=f"case-{split}", group=split, split=split, request="Write report.json with result=42.",
             files={"input.txt": "42"}, checks=[Check(kind="json_equals", key="result", expected=42),
                                                Check(kind="unchanged", path="input.txt", mandatory=True)])
        for split in ("train", "validation", "holdout")])
    atomic_write(root / "suite.json", suite.model_dump_json(indent=2))
    cfg = Experiment(skill=str(skill), suite=str(root / "suite.json"), targets=[
        Profile(id="demo", agent="codex", model="simulation", effort="none", version="test", backend="simulation")])
    atomic_write(root / "config.json", cfg.model_dump_json(indent=2))
    ident = create_run(store, root / "config.json")
    r = Runner(store, ident)
    sha = r.add_candidate("\nInvestigate the input. SIMULATION_PASS\n")
    r.select(sha)
    decision = r.finalize()
    report_sha, folder = report(store, ident)
    return {"run": ident, "candidate": sha, "decision": decision, "report": str(folder / "report.md"),
            "report_sha": report_sha, "model_called": False}

