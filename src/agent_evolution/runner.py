from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .adapters import parse_events
from .artifacts import snapshot, materialize, candidate, verify_snapshot
from .evaluators import evaluate
from .isolation import preflight, run_agent
from .models import Experiment, Suite, Case, Profile, load
from .process import execute
from .store import Store
from .util import EvolutionError, canonical, digest, atomic_write, within, reject_secrets, redact


def engine_hash() -> str:
    root = Path(__file__).parent
    return digest("".join(p.relative_to(root).as_posix() + digest(p.read_bytes()) for p in sorted(root.rglob("*.py"))))


def create_run(store: Store, path: Path) -> str:
    config = load(Experiment, path)
    skill = (path.resolve().parent / config.skill).resolve()
    suite = load(Suite, (path.resolve().parent / config.suite).resolve())
    base = snapshot(skill)
    config.skill = str(skill)
    data = {"config": config.model_dump(), "suite": suite.model_dump(), "suite_hash": digest(suite.model_dump_json()),
            "engine_hash": engine_hash(), "baseline": base["hash"], "artifacts": {base["hash"]: base},
            "selected": None, "final_candidate": None, "approval": None, "decision": None}
    return store.create(data)


class Runner:
    def __init__(self, store: Store, run_id: str):
        self.store, self.id = store, run_id
        self.data = store.get(run_id)
        if self.data["engine_hash"] != engine_hash():
            raise EvolutionError("Implementation changed; start a new run to preserve comparability")
        self.config = Experiment.model_validate(self.data["config"])
        self.suite = Suite.model_validate(self.data["suite"])
        for artifact in self.data["artifacts"].values():
            verify_snapshot(artifact)
        self.deadline = self.data["created"] + self.config.budget.wall_seconds
        self.path = store.root / "runs" / run_id
        self.path.mkdir(parents=True, exist_ok=True)
        self.cancel = self.path / "cancel"

    def save(self):
        self.store.put(self.data)

    def add_candidate(self, body: str) -> str:
        if self.data["final_candidate"]:
            raise EvolutionError("Final candidate is frozen; no further search in this run")
        new = candidate(self.data["artifacts"][self.data["baseline"]], body)
        if new["hash"] not in self.data["artifacts"]:
            if len(self.data["artifacts"]) - 1 >= self.config.budget.max_candidates:
                raise EvolutionError("Candidate budget exhausted")
            self.data["artifacts"][new["hash"]] = new
            self.save()
        return new["hash"]

    def cases(self, split):
        cases = [c for c in self.suite.cases if c.split == split]
        if not cases:
            raise EvolutionError(f"No {split} cases; zero-case evaluations are invalid")
        return cases

    def trial(self, sha: str, case: Case, profile: Profile, repeat: int = 0) -> dict:
        if self.cancel.exists():
            raise EvolutionError("Run cancelled; create a new run to continue")
        if case.split == "holdout" and self.data["final_candidate"] is None:
            raise EvolutionError("Holdout is unavailable until the final candidate is frozen")
        key = digest(canonical([sha, case.model_dump(), profile.model_dump(), repeat, self.data["engine_hash"]]))
        cached = self.store.cached(self.id, key)
        if cached:
            return cached
        preflight(profile)
        self.store.reserve(self.id, "task", self.config.budget.max_task_runs, self.deadline)
        info = {"key": key, "candidate": sha, "case": case.id, "group": case.group,
                "split": case.split, "profile": profile.id, "repeat": repeat,
                "evidence": "simulation" if profile.backend == "simulation" else "live",
                "profile_hash": digest(profile.model_dump_json()), "usage": None, "cost": None,
                "cost_status": "unknown", "status": "infrastructure_error", "score": 0,
                "passed": False, "hard_pass": False, "skill_read": False, "seconds": 0}
        # Place the workspace outside the store. Only this directory becomes a writable root.
        with tempfile.TemporaryDirectory(prefix="agent-evolution-task-") as temp:
            work = Path(temp).resolve()
            for name, text in case.files.items():
                atomic_write(within(work, name), text, 0o644)
            artifact = self.data["artifacts"][sha]
            location = ".claude/skills" if profile.agent == "claude" else ".agents/skills"
            skill_folder = work / location / artifact["name"]
            materialize(artifact, skill_folder)
            skill_path = skill_folder.relative_to(work).as_posix() + "/SKILL.md"
            prompt = (f"Use the skill at {skill_path}. First execute `cat {skill_path}` to read that exact file before solving the task. "
                      "Do not change the skill or any other configuration. "
                      "Use only files in this working directory.\n\n" + case.request)
            trace = {"commands": [], "skill_read": False, "usage": None, "cost": None}
            try:
                if profile.backend == "simulation":
                    # A transport/pipeline demonstration, explicitly not model evidence.
                    body = artifact["files"]["SKILL.md"]["text"]
                    if "SIMULATION_PASS" in body:
                        out = {}
                        for check in case.checks:
                            if check.kind == "json_equals" and "." not in check.key:
                                out[check.key] = check.expected
                        atomic_write(work / "report.json", canonical(out))
                    trace = {"commands": [str(c.expected) for c in case.checks if c.kind == "command_observed"],
                             "skill_read": True, "text": "simulation", "usage": None, "cost": None}
                    info.update(status="completed", seconds=.01)
                else:
                    self.scan(work)
                    r = run_agent(profile, work, prompt, skill_folder,
                                  min(self.config.budget.task_seconds, max(.01, self.deadline - time.time())),
                                  self.config.budget.max_output_bytes, self.cancel)
                    info.update(status=r.status, seconds=r.seconds)
                    if r.status == "cancelled":
                        atomic_write(self.cancel, "cancelled\n")
                    atomic_write(self.path / f"{key}.stderr.txt", redact(r.stderr)[:20000])
                    if r.status != "completed":
                        raise EvolutionError(f"Agent process ended: {r.status}")
                    reject_secrets(r.stdout)
                    trace = parse_events(profile.agent, r.stdout, skill_path)
                    self.scan(work)
                if not trace["skill_read"]:
                    raise EvolutionError("No completed skill-read event")
                if snapshot(skill_folder)["hash"] != sha:
                    raise EvolutionError("Protected skill bundle changed during evaluation")
                # Scan every produced path; never follow symlinks planted by an agent.
                for p in work.rglob("*"):
                    if p.is_symlink():
                        raise EvolutionError("Agent created a symlink")
                scored = evaluate(case, work, trace)
                info.update(scored, skill_read=True, usage=trace.get("usage"), cost=trace.get("cost"),
                            cost_status="reported" if trace.get("cost") is not None else "unknown")
                # Preserve only selected evidence; no private reasoning or raw provider stream.
                atomic_write(self.path / f"{key}.trace.json", canonical(trace))
                outputs = {}
                for check in case.checks:
                    p = within(work, check.path)
                    if p.is_file() and p.stat().st_size <= 100000:
                        outputs[check.path] = p.read_text(errors="replace")
                atomic_write(self.path / f"{key}.outputs.json", canonical(outputs))
            except EvolutionError as e:
                info["error"] = str(e)
                if info["status"] == "completed":
                    info["status"] = "infrastructure_error"
            except (OSError, ValueError):
                info["error"] = "Artifact or event processing failed"
                info["status"] = "infrastructure_error"
        self.store.save_eval(self.id, key, info)
        self.store.event(self.id, "evaluation", {"key": key, "status": info["status"]})
        return info

    @staticmethod
    def scan(work):
        scanner = shutil.which("gitleaks")
        if not scanner:
            raise EvolutionError("Gitleaks is required for live runs")
        r = execute([scanner, "dir", str(work), "--no-banner", "--redact", "--exit-code", "1"], work, 30, 100000)
        if r.status != "completed":
            raise EvolutionError("Secret scan failed or found a secret; details withheld")

    def evaluate_split(self, sha, split):
        if split == "holdout":
            raise EvolutionError("Use finalize for holdout evaluation")
        return [self.trial(sha, case, p) for case in self.cases(split) for p in self.config.targets]

    def select(self, sha):
        if sha not in self.data["artifacts"] or sha == self.data["baseline"]:
            raise EvolutionError("Select a non-baseline candidate")
        if self.data["final_candidate"] and self.data["final_candidate"] != sha:
            raise EvolutionError("Final candidate already frozen")
        self.data["selected"] = sha
        self.save()

    def finalize(self):
        sha = self.data["selected"]
        if not sha:
            raise EvolutionError("Select a candidate first")
        for split in ("train", "validation"):
            for artifact in (self.data["baseline"], sha):
                rows = self.evaluate_split(artifact, split)
                if any(r["status"] != "completed" for r in rows):
                    raise EvolutionError("Search evaluation incomplete; cannot expose holdout")
        self.store.claim_holdout(digest(canonical([self.data["suite_hash"], self.data["baseline"]])), self.id)
        self.data["final_candidate"] = sha
        self.data["status"] = "evaluating_holdout"
        self.save()
        for i in range(self.config.gates.repeats):
            order = (self.data["baseline"], sha) if i % 2 == 0 else (sha, self.data["baseline"])
            for case in self.cases("holdout"):
                for p in self.config.targets:
                    for artifact in order:
                        self.trial(artifact, case, p, i)
        from .reports import decide
        self.data["decision"] = decide(self.data, self.store.evaluations(self.id))
        self.data["status"] = self.data["decision"]["status"]
        self.save()
        return self.data["decision"]
