from __future__ import annotations

import json
import uuid
from pathlib import Path

from .artifacts import snapshot, candidate, parse_skill, verify_snapshot
from .reports import report, decide
from .runner import Runner
from .util import EvolutionError, atomic_write, canonical, digest, lock


def approve(store, run, candidate_sha, report_sha):
    runner = Runner(store, run)
    data = runner.data
    if data["final_candidate"] != candidate_sha or data["selected"] != candidate_sha:
        raise EvolutionError("Approval candidate does not match the frozen candidate")
    actual, _ = report(store, run)
    if actual != report_sha:
        raise EvolutionError("Report changed or report hash is incorrect")
    if decide(data, store.evaluations(run))["status"] != "eligible":
        raise EvolutionError("Candidate is not eligible for promotion")
    data["approval"] = {"candidate": candidate_sha, "report": report_sha, "target": data["config"]["skill"]}
    data["status"] = "approved"
    runner.save()


def promote(store, run, candidate_sha, report_sha):
    runner = Runner(store, run)
    data = runner.data
    target = Path(data["config"]["skill"])
    approval = {"candidate": candidate_sha, "report": report_sha, "target": str(target)}
    if data["approval"] != approval:
        raise EvolutionError("Explicit approval of this candidate, report and target is required")
    actual, _ = report(store, run)
    if actual != report_sha or decide(data, store.evaluations(run))["status"] != "eligible":
        raise EvolutionError("Evidence changed after approval")
    base, new = data["artifacts"][data["baseline"]], data["artifacts"][candidate_sha]
    verify_snapshot(base)
    verify_snapshot(new)
    _, _, body = parse_skill(new["files"]["SKILL.md"]["text"])
    if candidate(base, body)["hash"] != new["hash"]:
        raise EvolutionError("Protected files or frontmatter changed")
    with lock(target.parent / f".{target.name}.evolution.lock"):
        if snapshot(target)["hash"] != base["hash"]:
            raise EvolutionError("Canonical skill changed since evaluation")
        ident = uuid.uuid4().hex
        entry = {"id": ident, "run": run, "target": str(target), "before": base, "after": new,
                 "report": report_sha, "status": "prepared"}
        with store.db:
            store.db.execute("INSERT INTO promotions VALUES(?,?)", (ident, canonical(entry)))
        atomic_write(target / "SKILL.md", new["files"]["SKILL.md"]["text"], new["files"]["SKILL.md"]["mode"])
        entry["status"] = "promoted"
        with store.db:
            store.db.execute("UPDATE promotions SET data=? WHERE id=?", (canonical(entry), ident))
        data["status"] = "promoted"
        runner.save()
        return ident


def rollback(store, ident):
    row = store.db.execute("SELECT data FROM promotions WHERE id=?", (ident,)).fetchone()
    if not row:
        raise EvolutionError("Unknown promotion ID")
    entry = json.loads(row[0])
    if entry["status"] == "rolled_back":
        raise EvolutionError("Already rolled back")
    target = Path(entry["target"])
    verify_snapshot(entry["before"])
    verify_snapshot(entry["after"])
    with lock(target.parent / f".{target.name}.evolution.lock"):
        current = snapshot(target)["hash"]
        if current not in {entry["before"]["hash"], entry["after"]["hash"]}:
            raise EvolutionError("Canonical skill has subsequent edits; refusing overwrite")
        old = entry["before"]["files"]["SKILL.md"]
        atomic_write(target / "SKILL.md", old["text"], old["mode"])
        entry["status"] = "rolled_back"
        with store.db:
            store.db.execute("UPDATE promotions SET data=? WHERE id=?", (canonical(entry), ident))
        data = store.get(entry["run"])
        data["status"] = "rolled_back"
        data["approval"] = None
        store.put(data)

