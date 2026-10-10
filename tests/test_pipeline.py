import json
from pathlib import Path

import pytest

from agent_evolution.runner import Runner
from agent_evolution.reports import decide, report
from agent_evolution.promotion import approve, promote, rollback
from agent_evolution.util import EvolutionError, canonical


def prepared(experiment):
    store, r = experiment
    sha = r.add_candidate("\nInvestigate. SIMULATION_PASS\n")
    r.select(sha)
    r.finalize()
    return store, r, sha


def test_offline_evolution_never_promotable(experiment):
    store, r, sha = prepared(experiment)
    decision = decide(r.data, store.evaluations(r.id))
    assert decision["status"] == "inconclusive"
    assert decision["profiles"]["sample"]["gain"] == 1
    report_sha, _ = report(store, r.id)
    with pytest.raises(EvolutionError, match="eligible"):
        approve(store, r.id, sha, report_sha)


def test_cache_and_repeat_counts(experiment):
    store, r = experiment
    first = r.evaluate_split(r.data["baseline"], "train")
    assert r.evaluate_split(r.data["baseline"], "train") == first
    assert store.used(r.id)["task"] == 1
    r.trial(r.data["baseline"], r.cases("train")[0], r.config.targets[0], 1)
    assert store.used(r.id)["task"] == 2


def test_holdout_not_available_to_search(experiment):
    store, r = experiment
    with pytest.raises(EvolutionError, match="holdout"):
        r.evaluate_split(r.data["baseline"], "holdout")
    with pytest.raises(EvolutionError, match="Holdout"):
        r.trial(r.data["baseline"], r.cases("holdout")[0], r.config.targets[0])


def test_final_candidate_cannot_change(experiment):
    _, r, _ = prepared(experiment)
    with pytest.raises(EvolutionError, match="frozen"):
        r.add_candidate("A new procedure")


def test_holdout_claim_survives_restart(experiment):
    store, r = experiment
    store.claim_holdout("dataset", r.id)
    other = type(store)(store.root)
    with pytest.raises(EvolutionError, match="exposed"):
        other.claim_holdout("dataset", "another")


def test_incomplete_or_missing_runtime_blocks(experiment):
    store, r, _ = prepared(experiment)
    rows = store.evaluations(r.id)
    assert decide(r.data, rows[:-1])["status"] != "eligible"
    data = json.loads(canonical(r.data))
    data["config"]["targets"].append({**data["config"]["targets"][0], "id": "missing"})
    assert decide(data, rows)["status"] != "eligible"


def live_test_receipts(store, r):
    """Synthetic receipts to exercise promotion logic, never external evidence."""
    r.data["config"]["targets"][0].update(backend="codex-native", version="0.160.0")
    store.put(r.data)
    for row in store.evaluations(r.id):
        row["evidence"] = "live"
        store.save_eval(r.id, row["key"], row)


def test_simulation_profile_blocks_even_relabelled_receipts(experiment):
    store, r, _ = prepared(experiment)
    rows = store.evaluations(r.id)
    for row in rows:
        row["evidence"] = "live"
    assert decide(r.data, rows)["status"] == "inconclusive"


def test_approval_promotion_and_rollback(experiment):
    store, r, sha = prepared(experiment)
    live_test_receipts(store, r)
    report_sha, _ = report(store, r.id)
    with pytest.raises(EvolutionError, match="approval"):
        promote(store, r.id, sha, report_sha)
    approve(store, r.id, sha, report_sha)
    target = Path(r.config.skill) / "SKILL.md"
    before = target.read_text()
    promotion = promote(store, r.id, sha, report_sha)
    assert "SIMULATION_PASS" in target.read_text()
    rollback(store, promotion)
    assert target.read_text() == before


def test_post_evaluation_edits_preserved(experiment):
    store, r, sha = prepared(experiment)
    live_test_receipts(store, r)
    report_sha, _ = report(store, r.id)
    approve(store, r.id, sha, report_sha)
    target = Path(r.config.skill) / "SKILL.md"
    target.write_text(target.read_text() + "\nUser change\n")
    with pytest.raises(EvolutionError, match="Canonical"):
        promote(store, r.id, sha, report_sha)
    assert "User change" in target.read_text()


def test_report_hash_required(experiment):
    store, r, sha = prepared(experiment)
    live_test_receipts(store, r)
    with pytest.raises(EvolutionError, match="hash"):
        approve(store, r.id, sha, "0" * 64)


def test_rollback_preserves_later_edits(experiment):
    store, r, sha = prepared(experiment)
    live_test_receipts(store, r)
    report_sha, _ = report(store, r.id)
    approve(store, r.id, sha, report_sha)
    ident = promote(store, r.id, sha, report_sha)
    target = Path(r.config.skill) / "SKILL.md"
    target.write_text(target.read_text() + "\nUser edit\n")
    with pytest.raises(EvolutionError, match="subsequent"):
        rollback(store, ident)
