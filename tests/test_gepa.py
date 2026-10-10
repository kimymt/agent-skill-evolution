from agent_evolution.optimizer import evolve, Adapter
from agent_evolution.artifacts import parse_skill
import pytest
from agent_evolution.util import EvolutionError


def test_reflection_receives_execution_evidence_without_holdout(experiment):
    _, r = experiment
    adapter = Adapter(r)
    _, _, body = parse_skill(r.data["artifacts"][r.data["baseline"]]["files"]["SKILL.md"]["text"])
    batch = adapter.evaluate(r.cases("train"), {"skill_body": body}, capture_traces=True)
    feedback = adapter.make_reflective_dataset({}, batch, ["skill_body"])["skill_body"][0]["Feedback"]
    assert feedback["execution_evidence"][0]["trace"]["skill_read"]
    assert feedback["execution_evidence"][0]["outputs"]["app.txt"] == "unchanged"
    with pytest.raises(EvolutionError, match="holdout"):
        adapter.evaluate(r.cases("holdout"), {"skill_body": body}, capture_traces=True)


def test_real_gepa_engine_with_offline_reflection(experiment):
    store, r = experiment
    # GEPA itself runs; only its model and task transports are deterministic fakes.
    def reflect(prompt):
        return "```\nInvestigate the issue. SIMULATION_PASS\n```"
    sha = evolve(r, reflection=reflect)
    assert sha != r.data["baseline"]
    assert "SIMULATION_PASS" in r.data["artifacts"][sha]["files"]["SKILL.md"]["text"]
    assert all(row["split"] != "holdout" for row in store.evaluations(r.id))
