import json
from pathlib import Path

from typer.testing import CliRunner

from agent_evolution.cli import app
from agent_evolution.reports import report

from test_pipeline import live_test_receipts, prepared


def _invoke(store, command, run, sha, report_sha, *, yes=False, input=None):
    args = ["--state", str(store.root), command, "--run", run,
            "--candidate-sha", sha, "--report-sha", report_sha]
    if yes:
        args.append("--yes")
    return CliRunner().invoke(app, args, input=input)


def _eligible(experiment):
    store, r, sha = prepared(experiment)
    live_test_receipts(store, r)
    report_sha, _ = report(store, r.id)
    return store, r, sha, report_sha


def test_noninteractive_approve_without_yes_exits_nonzero(experiment, monkeypatch):
    store, r, sha, report_sha = _eligible(experiment)
    monkeypatch.setattr("agent_evolution.cli._is_interactive", lambda: False)
    result = _invoke(store, "approve", r.id, sha, report_sha)
    assert result.exit_code != 0
    assert "requires --yes" in result.output
    assert r.id in result.output and sha in result.output
    assert str(r.config.skill) in result.output
    data = store.get(r.id)
    assert data.get("approval") is None
    assert data["status"] != "approved"


def test_noninteractive_approve_with_yes_records_approval(experiment, monkeypatch):
    store, r, sha, report_sha = _eligible(experiment)
    monkeypatch.setattr("agent_evolution.cli._is_interactive", lambda: False)
    result = _invoke(store, "approve", r.id, sha, report_sha, yes=True)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output[result.output.index("{"):])
    assert payload["approved"] == sha
    assert store.get(r.id)["status"] == "approved"


def test_interactive_approve_yes_proceeds_without_flag(experiment, monkeypatch):
    store, r, sha, report_sha = _eligible(experiment)
    monkeypatch.setattr("agent_evolution.cli._is_interactive", lambda: True)
    result = _invoke(store, "approve", r.id, sha, report_sha, input="y\n")
    assert result.exit_code == 0, result.output
    assert store.get(r.id)["status"] == "approved"


def test_interactive_approve_default_and_no_do_not_record(experiment, monkeypatch):
    store, r, sha, report_sha = _eligible(experiment)
    monkeypatch.setattr("agent_evolution.cli._is_interactive", lambda: True)
    declined = _invoke(store, "approve", r.id, sha, report_sha, input="\n")
    assert declined.exit_code != 0
    assert store.get(r.id).get("approval") is None
    refused = _invoke(store, "approve", r.id, sha, report_sha, input="n\n")
    assert refused.exit_code != 0
    assert store.get(r.id).get("approval") is None
    assert "Confirmation declined" in declined.output + refused.output


def test_piped_yes_without_flag_is_rejected(experiment, monkeypatch):
    store, r, sha, report_sha = _eligible(experiment)
    monkeypatch.setattr("agent_evolution.cli._is_interactive", lambda: False)
    result = _invoke(store, "approve", r.id, sha, report_sha, input="y\n")
    assert result.exit_code != 0
    assert store.get(r.id).get("approval") is None


def test_promote_requires_confirmation_and_displays_target(experiment, monkeypatch):
    store, r, sha, report_sha = _eligible(experiment)
    monkeypatch.setattr("agent_evolution.cli._is_interactive", lambda: False)
    approved = _invoke(store, "approve", r.id, sha, report_sha, yes=True)
    assert approved.exit_code == 0, approved.output
    target = Path(r.config.skill) / "SKILL.md"
    before = target.read_text()
    blocked = _invoke(store, "promote", r.id, sha, report_sha)
    assert blocked.exit_code != 0
    assert "action: promote" in blocked.output
    assert str(r.config.skill) in blocked.output
    assert target.read_text() == before
    applied = _invoke(store, "promote", r.id, sha, report_sha, yes=True)
    assert applied.exit_code == 0, applied.output
    assert "SIMULATION_PASS" in target.read_text()
    payload = json.loads(applied.output[applied.output.index("{"):])
    assert "promotion" in payload


def test_approve_and_promote_help_list_yes_flag():
    runner = CliRunner()
    for command in ("approve", "promote"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output
        assert "--yes" in result.output
