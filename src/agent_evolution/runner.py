from __future__ import annotations

import json
import os
import shlex
import stat
import tempfile
import time
from pathlib import Path

from .adapters import parse_events
from .artifacts import snapshot, materialize, candidate, verify_snapshot, parse_skill
from .evaluators import evaluate
from .isolation import preflight, run_agent
from .models import Experiment, Suite, Case, Profile
from .secret_scan import scan_directory, scan_values, scan_streams
from .state import resolve_input_path
from .store import Store
from .util import EvolutionError, canonical, digest, atomic_write, within, reject_secrets, redact


def engine_hash() -> str:
    root = Path(__file__).parent
    return digest("".join(p.relative_to(root).as_posix() + digest(p.read_bytes()) for p in sorted(root.rglob("*.py"))))


def _live_scanner(config: Experiment, environments: dict) -> str | None:
    profiles = [*config.targets, *([config.generator] if config.generator else [])]
    live = [p for p in profiles if p.backend != "simulation"]
    if not live:
        return None
    from .environment import executable_identity
    actual = executable_identity("gitleaks")
    for profile in live:
        expected = environments.get(digest(profile.model_dump_json()), {}).get("scanner")
        if expected != actual:
            raise EvolutionError("Secret scanner changed; start a new run")
    return actual["resolved"]


def input_path(store: Store, path: Path) -> Path:
    return resolve_input_path(store.root, path)


def read_input_text(store: Store, path: Path) -> str:
    path = input_path(store, path)
    if not path.is_relative_to(store.root):
        return path.read_text()
    content = store.files.read_bytes(path.relative_to(store.root).as_posix())
    return content.decode().replace("\r\n", "\n").replace("\r", "\n")


def _load_input(store: Store, model, path: Path):
    try:
        return model.model_validate_json(read_input_text(store, path))
    except (ValueError, OSError) as error:
        raise EvolutionError(f"Invalid {model.__name__} file: {path.name} ({type(error).__name__})") from None


def _snapshot_input(store: Store, path: Path) -> dict:
    path = input_path(store, path)
    if not path.is_relative_to(store.root):
        return snapshot(path)
    files = {}

    def collect(folder: Path):
        relative = folder.relative_to(store.root).as_posix()
        with store.files.directory(relative) as fd:
            for name in sorted(os.listdir(fd)):
                child = folder / name
                if stat.S_ISDIR(os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode):
                    collect(child)
                    continue
                child_relative = child.relative_to(store.root).as_posix()
                info = store.files.file_info(child_relative)
                if info.st_size > 1_000_000:
                    raise EvolutionError("Unsupported artifact file")
                if len(files) >= 200:
                    raise EvolutionError("Too many artifact files")
                try:
                    text = store.files.read_bytes(child_relative, max_bytes=1_000_000).decode()
                except UnicodeError:
                    raise EvolutionError("Initial release supports text skill bundles only") from None
                text = text.replace("\r\n", "\n").replace("\r", "\n")
                reject_secrets(text)
                files[child.relative_to(path).as_posix()] = {"text": text, "mode": stat.S_IMODE(info.st_mode)}

    collect(path)
    if "SKILL.md" not in files:
        raise EvolutionError("SKILL.md missing")
    meta, _, _ = parse_skill(files["SKILL.md"]["text"])
    return {"name": meta["name"], "files": files, "hash": digest(canonical(files))}


def create_run(store: Store, path: Path) -> str:
    path = input_path(store, path)
    config = _load_input(store, Experiment, path)
    skill = input_path(store, path.parent / config.skill)
    suite = _load_input(store, Suite, path.parent / config.suite)
    base = _snapshot_input(store, skill)
    config.skill = str(skill)
    profiles = [*config.targets, *([config.generator] if config.generator else [])]
    environments = {digest(p.model_dump_json()): preflight(p) for p in profiles}
    scanner = _live_scanner(config, environments)
    if scanner:
        scan_values(config.model_dump(), suite.model_dump(), base["files"], scanner=scanner)
    data = {"config": config.model_dump(), "suite": suite.model_dump(), "suite_hash": digest(suite.model_dump_json()),
            "config_hash": digest(config.model_dump_json()), "environments": environments,
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
        self.relative_path = self.path.relative_to(store.root).as_posix()
        store.files.ensure_dir(self.relative_path)
        self.cancel = self.path / "cancel"

    def cancelled(self):
        return self.store.files.file_info(f"{self.relative_path}/cancel", missing_ok=True) is not None

    def save(self):
        self.store.put(self.data)

    def add_candidate(self, body: str) -> str:
        if self.data["final_candidate"]:
            raise EvolutionError("Final candidate is frozen; no further search in this run")
        new = candidate(self.data["artifacts"][self.data["baseline"]], body)
        if new["hash"] not in self.data["artifacts"]:
            if len(self.data["artifacts"]) - 1 >= self.config.budget.max_candidates:
                raise EvolutionError("Candidate budget exhausted")
            scanner = _live_scanner(self.config, self.data["environments"])
            if scanner:
                scan_values(new["files"], scanner=scanner)
            self.data["artifacts"][new["hash"]] = new
            self.save()
        return new["hash"]

    def cases(self, split):
        cases = [c for c in self.suite.cases if c.split == split]
        if not cases:
            raise EvolutionError(f"No {split} cases; zero-case evaluations are invalid")
        return cases

    def check_environment(self, profile: Profile) -> dict:
        if self.cancelled():
            raise EvolutionError("Run cancelled; create a new run to continue")
        if self.data["engine_hash"] != engine_hash():
            raise EvolutionError("Implementation changed; start a new run to preserve comparability")
        if self.data.get("config_hash") != digest(self.config.model_dump_json()):
            raise EvolutionError("Run configuration changed; start a new run")
        expected = self.data.get("environments", {}).get(digest(profile.model_dump_json()))
        if expected is None:
            raise EvolutionError("Profile has no frozen runtime identity; start a new run")
        actual = preflight(profile)
        if actual != expected:
            raise EvolutionError("Runtime changed; start a new run to preserve comparability")
        return actual

    def trial(self, sha: str, case: Case, profile: Profile, repeat: int = 0) -> dict:
        if self.cancelled():
            raise EvolutionError("Run cancelled; create a new run to continue")
        if case.split == "holdout" and self.data["final_candidate"] is None:
            raise EvolutionError("Holdout is unavailable until the final candidate is frozen")
        key = digest(canonical([sha, case.model_dump(), profile.model_dump(), repeat, self.data["engine_hash"]]))
        cached = self.store.cached(self.id, key)
        if cached:
            return cached
        identity = self.check_environment(profile)
        self.store.reserve(self.id, "task", self.config.budget.max_task_runs, self.deadline)
        info = {"key": key, "candidate": sha, "case": case.id, "group": case.group,
                "split": case.split, "profile": profile.id, "repeat": repeat,
                "evidence": "simulation" if profile.backend == "simulation" else "live",
                "profile_hash": digest(profile.model_dump_json()),
                # requested_model is what the profile asked for (null: harness default);
                # resolved_model is what the harness reported using (null: not reported).
                "requested_model": profile.model, "resolved_model": None, "usage": None, "cost": None,
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
            relative_skill = (skill_folder / "SKILL.md").relative_to(work).as_posix()
            skill_path = "/workspace/" + relative_skill if profile.backend == "docker" else str(skill_folder / "SKILL.md")
            read_instruction = (f"First use Read with only file_path={skill_path}, without offset, limit or pages. "
                                if profile.agent == "claude" else
                                f"First execute `/bin/cat {shlex.quote(skill_path)}` as a separate foreground tool call. ")
            prompt = (f"Use the skill at {skill_path}. " + read_instruction +
                      "Return the entire file without truncation. "
                      "Use a non-login shell (login=false where supported). "
                      "For execution evidence use one literal command per tool call, without exports, "
                      "shell operators, redirection or expansions. "
                      "Do not change the skill or any other configuration. "
                      "Use only files in this working directory.\n\n" + case.request)
            trace = {"commands": [], "skill_read": False, "usage": None, "cost": None}
            stderr = None
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
                    self.scan(work, identity["scanner"]["resolved"])
                    scan_values(prompt, scanner=identity["scanner"]["resolved"])
                    r = run_agent(profile, work, prompt, skill_folder,
                                  min(self.config.budget.task_seconds, max(.01, self.deadline - time.time())),
                                  self.config.budget.max_output_bytes, self.cancelled, expected_identity=identity)
                    info.update(status=r.status, seconds=r.seconds)
                    if r.status == "cancelled":
                        self.store.files.write_bytes(f"{self.relative_path}/cancel", "cancelled\n")
                    # Inspect all received text before truncation, parsing or
                    # writing any diagnostic, including failed processes.
                    scan_streams(r.stdout, r.stderr, scanner=identity["scanner"]["resolved"])
                    stderr = redact(r.stderr)[:20000]
                    if r.status != "completed":
                        self.store.files.write_bytes(f"{self.relative_path}/{key}.stderr.txt", stderr)
                        raise EvolutionError(f"Agent process ended: {r.status}")
                    reject_secrets(r.stdout)
                    trace = parse_events(profile.agent, r.stdout, skill_path,
                                         skill_text=artifact["files"]["SKILL.md"]["text"])
                    self.scan(work, identity["scanner"]["resolved"])
                if not trace["skill_read"]:
                    raise EvolutionError("No verified full skill content in completed tool result")
                if snapshot(skill_folder)["hash"] != sha:
                    raise EvolutionError("Protected skill bundle changed during evaluation")
                # Scan every produced path; never follow symlinks planted by an agent.
                for p in work.rglob("*"):
                    if p.is_symlink():
                        raise EvolutionError("Agent created a symlink")
                scored = evaluate(case, work, trace)
                # Preserve only selected evidence; no private reasoning or raw provider stream.
                outputs = {}
                for check in case.checks:
                    p = within(work, check.path)
                    if p.is_file() and p.stat().st_size <= 100000:
                        outputs[check.path] = p.read_text(errors="replace")
                if profile.backend != "simulation":
                    scan_values(trace, outputs, stderr, scanner=identity["scanner"]["resolved"])
                if stderr is not None:
                    self.store.files.write_bytes(f"{self.relative_path}/{key}.stderr.txt", stderr)
                self.store.files.write_bytes(f"{self.relative_path}/{key}.trace.json", canonical(trace))
                self.store.files.write_bytes(f"{self.relative_path}/{key}.outputs.json", canonical(outputs))
                info.update(scored, skill_read=True, resolved_model=trace.get("resolved_model"),
                            usage=trace.get("usage"), cost=trace.get("cost"),
                            cost_status="reported" if trace.get("cost") is not None else "unknown")
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
    def scan(work, scanner=None):
        return scan_directory(work, scanner)

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
