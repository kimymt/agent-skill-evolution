import json

import pytest

from agent_evolution.adapters import parse_events
from agent_evolution.models import Case, Check, Experiment, Profile, Suite
from agent_evolution.reports import decide, report
from agent_evolution.runner import Runner, create_run
from agent_evolution.store import Store
from agent_evolution.util import atomic_write

SKILL = "/work/SKILL.md"


def stream(*records):
    return "\n".join(json.dumps(r) for r in records)


def parse(agent, *records):
    return parse_events(agent, stream(*records), SKILL)


CLAUDE_DONE = {"type": "result", "subtype": "success", "is_error": False, "result": "ok"}
OPENCODE_DONE = {"type": "step_finish", "part": {"reason": "stop", "tokens": {}, "cost": 0}}


def assistant(model, **extra):
    return {"type": "assistant", "message": {"model": model, "content": []}, **extra}


def pi_message(role="assistant", **fields):
    return {"role": role, "content": [], "stopReason": "stop", **fields}


# -- extraction -----------------------------------------------------------

def test_codex_events_carry_no_model():
    done = {"type": "turn.completed", "usage": {}, "model": "model-a"}
    assert parse("codex", {"type": "thread.started"}, done)["resolved_model"] is None


def test_claude_reads_assistant_message_model():
    assert parse("claude", assistant("model-a"), CLAUDE_DONE)["resolved_model"] == "model-a"


def test_claude_init_is_only_a_fallback():
    init = {"type": "system", "subtype": "init", "model": "model-init"}
    assert parse("claude", init, CLAUDE_DONE)["resolved_model"] == "model-init"
    assert parse("claude", init, assistant("model-a"), CLAUDE_DONE)["resolved_model"] == "model-a"


def test_claude_ignores_subagent_and_synthetic_turns():
    records = [assistant("model-sub", parent_tool_use_id="toolu_1"), assistant("<synthetic>"),
               assistant("model-a", parent_tool_use_id=None), CLAUDE_DONE]
    assert parse("claude", *records)["resolved_model"] == "model-a"


def test_opencode_reads_event_model_id_when_present():
    stamped = {**OPENCODE_DONE, "providerID": "provider-a", "modelID": "model-a"}
    assert parse("opencode", stamped)["resolved_model"] == "model-a"
    assert parse("opencode", OPENCODE_DONE)["resolved_model"] is None


def test_pi_prefers_response_model_over_requested_model():
    end = {"type": "message_end", "message": pi_message(provider="provider-a", model="model-a", responseModel="model-r")}
    assert parse("pi", end, {"type": "agent_end", "messages": []})["resolved_model"] == "model-r"
    end = {"type": "message_end", "message": pi_message(provider="provider-a", model="model-a")}
    assert parse("pi", end, {"type": "agent_end", "messages": []})["resolved_model"] == "model-a"


def test_pi_reads_agent_end_messages_and_ignores_user_messages():
    messages = [pi_message("user", model="model-user"), pi_message(model="model-a")]
    assert parse("pi", {"type": "agent_end", "messages": messages})["resolved_model"] == "model-a"


def test_several_models_stay_visible_and_compare_unequal():
    got = parse("claude", assistant("model-b"), assistant("model-a"), CLAUDE_DONE)["resolved_model"]
    assert got == "model-a,model-b"


@pytest.mark.parametrize("bad", ["", " ", "model a", "model\nb", "x" * 129, 7, None, {"a": 1}])
def test_unusable_model_values_are_ignored(bad):
    assert parse("claude", assistant(bad), CLAUDE_DONE)["resolved_model"] is None


# -- recording ------------------------------------------------------------

def test_trial_records_requested_and_resolved_separately(experiment):
    store, r = experiment
    row = r.trial(r.data["baseline"], r.cases("train")[0], r.config.targets[0])
    assert row["requested_model"] == "fake"
    assert row["resolved_model"] is None  # simulation never reports a model
    assert store.evaluations(r.id)[0]["requested_model"] == "fake"


def test_trial_without_explicit_model_records_null_requested(tmp_path):
    skill = tmp_path / "skill"
    atomic_write(skill / "SKILL.md", "---\nname: sample\ndescription: Test fixture skill.\n---\n\nInvestigate.\n")
    cases = [Case(id=f"case-{i}", group=f"group-{i}", split=split, request="Do it.", files={"app.txt": "x"},
                  checks=[Check(kind="unchanged", path="app.txt", mandatory=True)])
             for i, split in enumerate(["train", "validation"] + ["holdout"] * 5)]
    atomic_write(tmp_path / "suite.json", Suite(id="tests", reviewed=True, cases=cases).model_dump_json())
    profile = Profile(id="sample", agent="codex", effort="none", version="test", backend="simulation")
    assert profile.model is None
    cfg = Experiment(skill=str(skill), suite=str(tmp_path / "suite.json"), targets=[profile])
    atomic_write(tmp_path / "config.json", cfg.model_dump_json())
    store = Store(tmp_path / "state")
    r = Runner(store, create_run(store, tmp_path / "config.json"))
    row = r.trial(r.data["baseline"], r.cases("train")[0], r.config.targets[0])
    assert row["requested_model"] is None and row["resolved_model"] is None


# -- comparability --------------------------------------------------------

def finished(experiment, models):
    """Frozen run whose holdout rows carry the given resolved model per candidate."""
    store, r = experiment
    sha = r.add_candidate("\nInvestigate. SIMULATION_PASS\n")
    r.select(sha)
    r.finalize()
    r.data["config"]["targets"][0].update(backend="codex-native", version="0.160.0")
    store.put(r.data)
    for row in store.evaluations(r.id):
        row["evidence"] = "live"
        row["resolved_model"] = models(row)
        store.save_eval(r.id, row["key"], row)
    return store, r, sha


def test_same_model_gives_no_warning(experiment):
    store, r, _ = finished(experiment, lambda row: "model-a")
    decision = decide(r.data, store.evaluations(r.id))
    assert decision["warnings"] == []
    assert decision["status"] == "eligible"


def test_unknown_model_only_warns(experiment):
    store, r, _ = finished(experiment, lambda row: None)
    decision = decide(r.data, store.evaluations(r.id))
    assert any("resolved model unknown" in w for w in decision["warnings"])
    assert decision["status"] == "eligible"


def test_partly_unknown_model_warns_with_counts(experiment):
    store, r, sha = finished(experiment, lambda row: None if row["case"] == "case-2" else "model-a")
    rows = store.evaluations(r.id)
    live = [x for x in rows if x["candidate"] in {r.data["baseline"], sha}]
    missing = [x for x in live if x["resolved_model"] is None]
    assert 0 < len(missing) < len(live)
    decision = decide(r.data, rows)
    assert [w for w in decision["warnings"] if f"unknown in {len(missing)} of {len(live)} " in w]
    assert decision["status"] == "eligible"


def test_baseline_candidate_mismatch_warns_and_blocks(experiment):
    store, r = experiment
    baseline = r.data["baseline"]
    store, r, sha = finished(experiment, lambda row: "model-a" if row["candidate"] == baseline else "model-b")
    decision = decide(r.data, store.evaluations(r.id))
    assert any("resolved models differ (baseline: model-a; candidate: model-b)" in w for w in decision["warnings"])
    assert any("resolved model differs between baseline and candidate" in x for x in decision["reasons"])
    assert decision["status"] == "inconclusive"


def test_report_shows_warning_and_resolved_model_column(experiment):
    store, r, _ = finished(experiment, lambda row: None)
    _, folder = report(store, r.id)
    text = (folder / "report.md").read_text()
    assert "| Resolved model |" in text and "## Warnings" in text and "unknown" in text
    saved = json.loads((folder / "report.json").read_text())
    assert saved["decision"]["warnings"] and saved["evaluations"][0]["resolved_model"] is None


def test_report_without_warnings_has_no_warning_section(experiment):
    store, r, _ = finished(experiment, lambda row: "model-a")
    _, folder = report(store, r.id)
    text = (folder / "report.md").read_text()
    assert "## Warnings" not in text and "model-a" in text


def test_explicit_model_behaviour_is_unchanged(experiment):
    # Profile still says model="fake": status logic ignores requested_model entirely.
    store, r, _ = finished(experiment, lambda row: None)
    assert r.config.targets[0].model == "fake"
    with_names = decide(r.data, store.evaluations(r.id))
    for row in store.evaluations(r.id):
        row.pop("resolved_model")
        row.pop("requested_model", None)  # rows written before this change
        store.save_eval(r.id, row["key"], row)
    legacy = decide(r.data, store.evaluations(r.id))
    assert legacy["status"] == with_names["status"] == "eligible"
    assert legacy["reasons"] == with_names["reasons"]
