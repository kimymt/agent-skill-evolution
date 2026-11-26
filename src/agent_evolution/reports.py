from __future__ import annotations

import difflib
import random
import statistics

from .models import Experiment, Suite
from .util import canonical, digest


def _known(rows):
    return {r["resolved_model"] for r in rows if r.get("resolved_model")}


def _model_notes(profile, baseline, final, rows, subset, warnings, reasons):
    """Same-model comparability of baseline and candidate on one profile.

    An omitted model with no reported name only warns, and recommends specifying
    model: some harnesses never report one, and failing would block every run on
    them. A requested model with an unreported name is not a warning, because the
    comparison is already pinned to that request. Two different known names are
    different test conditions, so inside the holdout they also make the run
    inconclusive.
    """
    live = [r for r in rows if r["profile"] == profile and r["candidate"] in {baseline, final}
            and r.get("evidence") == "live"]
    unconfirmed = [r for r in live if not r.get("requested_model") and not r.get("resolved_model")]
    if unconfirmed:
        warnings.append(f"{profile}: resolved model unknown in {len(unconfirmed)} of {len(live)} "
                        "live evaluations that omitted model; comparison is less reliable. "
                        "Specify model when comparing")
    names = {a: sorted(_known([r for r in live if r["candidate"] == c])) for a, c in (("baseline", baseline), ("candidate", final))}
    if len(_known(live)) > 1:
        warnings.append(f"{profile}: resolved models differ (baseline: {', '.join(names['baseline']) or 'unknown'}; "
                        f"candidate: {', '.join(names['candidate']) or 'unknown'})")
    if len(_known([r for r in subset if r.get("evidence") == "live"])) > 1:
        reasons.append(f"{profile}: resolved model differs between baseline and candidate in the holdout")


def decide(data, rows):
    cfg = Experiment.model_validate(data["config"])
    suite = Suite.model_validate(data["suite"])
    final = data["final_candidate"]
    if not final:
        return {"status": "inconclusive", "reasons": ["Final candidate not frozen"], "warnings": [], "profiles": {}}
    reasons, warnings, profiles, gains = [], [], {}, []
    hard_failure = False
    search_cases = {c.id for c in suite.cases if c.split in {"train", "validation"}}
    for p in cfg.targets:
        if p.backend == "simulation":
            reasons.append(f"{p.id}: simulation profile cannot establish performance")
        for artifact in (data["baseline"], final):
            search = [r for r in rows if r["profile"] == p.id and r["candidate"] == artifact and r["split"] in {"train", "validation"}]
            if not search_cases or {r["case"] for r in search} != search_cases or any(r["status"] != "completed" for r in search):
                reasons.append(f"{p.id}: search evaluation incomplete")
            if any(not r["hard_pass"] for r in search):
                reasons.append(f"{p.id}: mandatory search checks failed")
                hard_failure = True
    expected_cases = {c.id: c for c in suite.cases if c.split == "holdout"}
    for p in cfg.targets:
        subset = [r for r in rows if r["profile"] == p.id and r["split"] == "holdout" and r["candidate"] in {data["baseline"], final}]
        expected = {(c, i, a) for c in expected_cases for i in range(cfg.gates.repeats) for a in (data["baseline"], final)}
        actual = {(r["case"], r["repeat"], r["candidate"]) for r in subset}
        if actual != expected or not expected or len(subset) != len(expected):
            reasons.append(f"{p.id}: incomplete holdout")
            continue
        if any(r["status"] != "completed" or not r["skill_read"] for r in subset):
            reasons.append(f"{p.id}: invalid execution evidence")
            continue
        if any(r["evidence"] != "live" for r in subset):
            reasons.append(f"{p.id}: simulation is not performance evidence")
        _model_notes(p.id, data["baseline"], final, rows, subset, warnings, reasons)
        before = [r for r in subset if r["candidate"] == data["baseline"]]
        after = [r for r in subset if r["candidate"] == final]
        base = statistics.mean(float(r["passed"]) for r in before)
        new = statistics.mean(float(r["passed"]) for r in after)
        gain = new - base
        gains.append(gain)
        if any(not r["hard_pass"] for r in after) or gain < 0:
            hard_failure = True
            reasons.append(f"{p.id}: hard check failure or regression")
        groups = {}
        for case_id, case in expected_cases.items():
            old = statistics.mean(float(r["passed"]) for r in before if r["case"] == case_id)
            cur = statistics.mean(float(r["passed"]) for r in after if r["case"] == case_id)
            groups.setdefault(case.group, []).append(cur - old)
        deltas = [statistics.mean(v) for v in groups.values()]
        rng = random.Random(0)
        boot = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(2000))
        low, high = boot[49], boot[1949]
        old_time = statistics.mean(r["seconds"] for r in before)
        new_time = statistics.mean(r["seconds"] for r in after)
        if len(groups) < cfg.gates.min_holdout_groups or cfg.gates.repeats < 3 or low < 0:
            reasons.append(f"{p.id}: insufficient evidence for non-regression")
        if new_time > old_time * (1 + cfg.gates.max_time_growth):
            reasons.append(f"{p.id}: time growth requires review")
        known_costs = all(r.get("cost") is not None for r in subset)
        if known_costs:
            old_cost = sum(r["cost"] for r in before)
            new_cost = sum(r["cost"] for r in after)
            if new_cost > old_cost * 1.2:
                reasons.append(f"{p.id}: cost growth requires review")
        profiles[p.id] = {"baseline_success_rate": base, "candidate_success_rate": new,
                          "gain": gain, "group_bootstrap_95": [low, high], "groups": len(groups),
                          "baseline_seconds": old_time, "candidate_seconds": new_time,
                          "cost_comparison": "reported" if known_costs else "unknown"}
    if not suite.reviewed:
        reasons.append("Dataset has not been marked reviewed")
    if not gains or max(gains) < cfg.gates.min_gain:
        reasons.append("No profile meets the minimum improvement")
    status = "rejected" if hard_failure else ("inconclusive" if reasons else "eligible")
    return {"status": status, "reasons": reasons, "warnings": warnings, "profiles": profiles}


def report(store, run):
    data = store.get(run)
    rows = store.evaluations(run)
    result = {"run": run, "config": data["config"], "baseline": data["baseline"],
              "candidate": data["selected"], "engine_hash": data["engine_hash"],
              "suite_hash": data["suite_hash"], "decision": decide(data, rows),
              "evaluations": rows, "usage": store.used(run),
              "not_selected": sorted({"claude", "codex", "opencode", "pi"} - {p["agent"] for p in data["config"]["targets"]})}
    sha = digest(canonical(result))
    folder = store.root / "reports" / run
    relative = folder.relative_to(store.root).as_posix()
    store.files.write_bytes(f"{relative}/report.json", canonical(result))
    baseline = data["artifacts"][data["baseline"]]["files"]["SKILL.md"]["text"]
    diff = ""
    store.files.write_bytes(f"{relative}/baseline.SKILL.md", baseline)
    if data["selected"]:
        selected = data["artifacts"][data["selected"]]["files"]["SKILL.md"]["text"]
        store.files.write_bytes(f"{relative}/candidate.SKILL.md", selected)
        diff = "".join(difflib.unified_diff(baseline.splitlines(True), selected.splitlines(True), "baseline/SKILL.md", "candidate/SKILL.md"))
    store.files.write_bytes(f"{relative}/skill.diff", diff)
    lines = ["# Skill evaluation", "", f"Run: `{run}`", f"Report SHA-256: `{sha}`", "",
             f"Decision: **{result['decision']['status']}**", "",
             "Not evaluated: " + ", ".join(result["not_selected"]), "",
             "| Profile | Case | Split | Candidate | Status | Passed | Evidence | Resolved model |",
             "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in rows:
        lines.append(f"| {r['profile']} | {r['case']} | {r['split']} | {r['candidate'][:10]} | {r['status']} | {r['passed']} | {r['evidence']} | {r.get('resolved_model') or 'unknown'} |")
    if result["decision"]["warnings"]:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in result["decision"]["warnings"]]
    lines += ["", "## Decision details", "", "```json", json_pretty(result["decision"]), "```", "",
              "Usage counts include failed launches. Unknown costs are not zero. No inference of quality for untested runtimes or models."]
    store.files.write_bytes(f"{relative}/report.md", "\n".join(lines) + "\n")
    return sha, folder


def json_pretty(value):
    import json
    return json.dumps(value, ensure_ascii=False, indent=2)
