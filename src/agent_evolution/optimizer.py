from __future__ import annotations

import importlib.metadata
import json
import tempfile
import time
from pathlib import Path

from .artifacts import parse_skill, materialize
from .adapters import parse_events
from .isolation import run_agent
from .runner import Runner
from .util import EvolutionError, canonical, reject_secrets


class ReflectionModel:
    def __init__(self, runner: Runner):
        self.runner = runner

    def __call__(self, prompt):
        r = self.runner
        profile = r.config.generator
        if profile is None or profile.backend == "simulation":
            raise EvolutionError("Configure an explicit live generator profile")
        r.store.reserve(r.id, "generation", r.config.budget.max_generations, r.deadline)
        text = prompt if isinstance(prompt, str) else canonical(prompt)
        reject_secrets(text)
        with tempfile.TemporaryDirectory(prefix="agent-evolution-reflect-") as temp:
            work = Path(temp).resolve()
            artifact = r.data["artifacts"][r.data["baseline"]]
            folder = work / ".agents/skills" / artifact["name"]
            materialize(artifact, folder)
            r.scan(work)
            result = run_agent(profile, work, text, folder,
                               min(r.config.budget.task_seconds, max(.01, r.deadline - time.time())),
                               r.config.budget.max_output_bytes, r.cancel)
            if result.status != "completed":
                raise EvolutionError("Reflection model did not complete")
            reject_secrets(result.stdout)
            output = parse_events(profile.agent, result.stdout, "unused")
            if not output["text"].strip():
                raise EvolutionError("Reflection model returned empty output")
            r.store.event(r.id, "generation", {"profile": profile.model_dump(), "usage": output.get("usage"),
                                               "cost": output.get("cost")})
            return output["text"]


class Adapter:
    propose_new_texts = None

    def __init__(self, runner):
        self.runner = runner

    def evaluate(self, batch, candidate, capture_traces=False):
        from gepa.core.adapter import EvaluationBatch
        r = self.runner
        try:
            sha = r.add_candidate(candidate["skill_body"])
        except EvolutionError as e:
            if "budget" in str(e) or "frozen" in str(e):
                raise
            outputs = [{"error": str(e)} for _ in batch]
            return EvaluationBatch(outputs=outputs, scores=[0.] * len(batch), trajectories=outputs if capture_traces else None)
        outputs, scores = [], []
        for case in batch:
            if case.split == "holdout":
                raise EvolutionError("Optimizer cannot access holdout")
            rows = [r.trial(sha, case, p) for p in r.config.targets]
            if any(row["status"] != "completed" for row in rows):
                raise EvolutionError("Evaluation infrastructure failed; optimizer stopped")
            outputs.append({"request": case.request, "results": rows})
            if capture_traces:
                evidence = []
                for row in rows:
                    item = {"profile": row["profile"]}
                    for kind in ("trace", "outputs"):
                        path = r.path / f"{row['key']}.{kind}.json"
                        if path.is_file():
                            text = path.read_text()
                            reject_secrets(text)
                            item[kind] = json.loads(text)
                    evidence.append(item)
                outputs[-1]["execution_evidence"] = evidence
            scores.append(sum(row["score"] for row in rows) / len(rows))
        return EvaluationBatch(outputs=outputs, scores=scores, trajectories=outputs if capture_traces else None)

    def make_reflective_dataset(self, candidate, eval_batch, components_to_update):
        # Only train minibatch evidence enters the teacher prompt. No suite/holdout object.
        return {key: [{"Inputs": item.get("request", ""), "Feedback": item} for item in (eval_batch.trajectories or [])]
                for key in components_to_update}


def evolve(runner, reflection=None):
    if importlib.metadata.version("gepa") != "0.1.4":
        raise EvolutionError("This integration requires gepa==0.1.4")
    from gepa import optimize
    _, _, body = parse_skill(runner.data["artifacts"][runner.data["baseline"]]["files"]["SKILL.md"]["text"])
    result = optimize(seed_candidate={"skill_body": body}, trainset=runner.cases("train"),
                      valset=runner.cases("validation"), adapter=Adapter(runner),
                      reflection_lm=reflection or ReflectionModel(runner),
                      reflection_minibatch_size=1, max_metric_calls=runner.config.budget.max_task_runs,
                      stop_callbacks=lambda state: len(runner.data["artifacts"]) - 1 >= runner.config.budget.max_candidates,
                      use_merge=False, display_progress_bar=False, raise_on_exception=True,
                      seed=0, cache_evaluation=True)
    sha = runner.add_candidate(result.best_candidate["skill_body"])
    if sha != runner.data["baseline"]:
        runner.select(sha)
    return sha
