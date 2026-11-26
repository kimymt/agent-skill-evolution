"""Storage boundary integration tests; no model processes or existing state."""
import os
import json
import stat

import pytest

from agent_evolution import cli
from agent_evolution.artifacts import parse_skill
from agent_evolution.demo import run_demo
from agent_evolution.optimizer import Adapter
from agent_evolution.reports import report
from agent_evolution.promotion import approve, promote, rollback
from agent_evolution.runner import Runner, create_run
from agent_evolution.state import PrivateState, StateError
from agent_evolution.store import Store
from agent_evolution.util import EvolutionError, canonical, digest


def plant_file(path, kind, tmp_path):
    """Keep an independent target to prove that rejected I/O did not alter it."""
    target = tmp_path / "external-target"
    target.write_bytes(b"unchanged external contents")
    target.chmod(0o600)
    if path.exists():
        path.unlink()
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.write_bytes(b"unprotected existing contents")
        path.chmod(0o644)
    return target


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_trial_rejects_cancel_file_before_reserving_usage(experiment, tmp_path, kind):
    store, runner = experiment
    target = plant_file(runner.cancel, kind, tmp_path)
    with pytest.raises(StateError):
        runner.trial(runner.data["baseline"], runner.cases("train")[0], runner.config.targets[0])
    assert store.used(runner.id) == {}
    assert target.read_bytes() == b"unchanged external contents"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_trial_rejects_unsafe_evidence_destination(experiment, tmp_path, kind):
    store, runner = experiment
    case, profile, sha = runner.cases("train")[0], runner.config.targets[0], runner.data["baseline"]
    key = digest(canonical([sha, case.model_dump(), profile.model_dump(), 0, runner.data["engine_hash"]]))
    target = plant_file(runner.path / f"{key}.trace.json", kind, tmp_path)
    result = runner.trial(sha, case, profile)
    assert result["status"] == "infrastructure_error"
    assert not result["passed"] and not result["skill_read"]
    assert not (runner.path / f"{key}.outputs.json").exists()
    assert target.read_bytes() == b"unchanged external contents"
    assert store.evaluations(runner.id) == [result]


def test_runner_rejects_changed_run_directory_permissions(experiment):
    store, runner = experiment
    runner.path.chmod(0o755)
    with pytest.raises(StateError):
        Runner(store, runner.id)


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_report_rejects_unsafe_destination(experiment, tmp_path, kind):
    store, runner = experiment
    folder = store.files.ensure_dir(f"reports/{runner.id}")
    target = plant_file(folder / "report.json", kind, tmp_path)
    with pytest.raises(StateError):
        report(store, runner.id)
    assert target.read_bytes() == b"unchanged external contents"


def test_report_rejects_redirected_parent(experiment, tmp_path):
    store, runner = experiment
    outside = tmp_path / "outside-reports"
    outside.mkdir(mode=0o700)
    (store.root / "reports").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StateError):
        report(store, runner.id)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_optimizer_rejects_unsafe_saved_evidence(experiment, tmp_path, kind):
    _, runner = experiment
    row = runner.evaluate_split(runner.data["baseline"], "train")[0]
    target = plant_file(runner.path / f"{row['key']}.trace.json", kind, tmp_path)
    _, _, body = parse_skill(runner.data["artifacts"][runner.data["baseline"]]["files"]["SKILL.md"]["text"])
    with pytest.raises(StateError):
        Adapter(runner).evaluate(runner.cases("train"), {"skill_body": body}, capture_traces=True)
    assert target.read_bytes() == b"unchanged external contents"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_cli_rejects_unsafe_run_lock(experiment, tmp_path, monkeypatch, kind):
    store, runner = experiment
    target = plant_file(store.root / f"{runner.id}.lock", kind, tmp_path)
    monkeypatch.setattr(cli, "store", lambda: store)
    with pytest.raises(StateError):
        cli.baseline(run=runner.id)
    assert store.used(runner.id) == {}
    assert target.read_bytes() == b"unchanged external contents"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_cli_rejects_unsafe_cancel_destination(experiment, tmp_path, monkeypatch, kind):
    store, runner = experiment
    target = plant_file(runner.cancel, kind, tmp_path)
    monkeypatch.setattr(cli, "store", lambda: store)
    with pytest.raises(StateError):
        cli.cancel(run=runner.id)
    assert target.read_bytes() == b"unchanged external contents"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_cli_import_checks_state_internal_body(experiment, tmp_path, monkeypatch, kind):
    store, runner = experiment
    body = store.root / "candidate.txt"
    target = plant_file(body, kind, tmp_path)
    monkeypatch.setattr(cli, "store", lambda: store)
    with pytest.raises(StateError):
        cli.import_candidate(run=runner.id, body=body)
    assert len(store.get(runner.id)["artifacts"]) == 1
    assert target.read_bytes() == b"unchanged external contents"


def prepare_internal_inputs(store, runner):
    config = runner.config.model_copy(update={"skill": "skill", "suite": "suite.json"})
    store.files.write_bytes("inputs/config.json", config.model_dump_json())
    store.files.write_bytes("inputs/suite.json", runner.suite.model_dump_json())
    for name, item in runner.data["artifacts"][runner.data["baseline"]]["files"].items():
        store.files.write_bytes("inputs/skill/" + name, item["text"])
    return store.root / "inputs/config.json"


@pytest.mark.parametrize("relative", ["config.json", "suite.json", "skill/SKILL.md", "skill/references/info.md"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_create_run_checks_every_state_internal_input(experiment, tmp_path, relative, kind):
    store, runner = experiment
    config = prepare_internal_inputs(store, runner)
    target = plant_file(store.root / "inputs" / relative, kind, tmp_path)
    with pytest.raises(StateError):
        create_run(store, config)
    assert target.read_bytes() == b"unchanged external contents"


def test_create_run_keeps_external_shared_link_but_checks_state_target(experiment, tmp_path):
    store, runner = experiment
    config = prepare_internal_inputs(store, runner)
    alias = tmp_path / "config-alias.json"
    alias.symlink_to(config)
    ident = create_run(store, alias)
    assert store.get(ident)["baseline"] == runner.data["baseline"]
    config.chmod(0o644)
    with pytest.raises(StateError):
        create_run(store, alias)


def test_demo_creates_private_state_and_remains_unpromotable(tmp_path):
    store = Store(tmp_path / "state")
    result = run_demo(store)
    assert result["model_called"] is False
    assert result["decision"]["status"] == "inconclusive"
    for path in [store.root, *store.root.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)


def test_demo_does_not_replace_unsafe_existing_input(tmp_path):
    store = Store(tmp_path / "state")
    folder = store.files.ensure_dir("demo-input/demo-skill")
    target = plant_file(folder / "SKILL.md", "symlink", tmp_path)
    with pytest.raises(StateError):
        run_demo(store)
    assert target.read_bytes() == b"unchanged external contents"


@pytest.mark.parametrize("command", ["schema", "validate", "doctor"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_cli_checks_explicit_state_paths_without_opening_database(tmp_path, monkeypatch, command, kind):
    files = PrivateState(tmp_path / "state")
    path = files.root / "input.json"
    target = plant_file(path, kind, tmp_path)
    monkeypatch.setattr(cli, "state_root", files.root)
    with pytest.raises(StateError):
        if command == "schema":
            cli.schema(output=path)
        elif command == "validate":
            cli.validate(suite=path)
        else:
            cli.doctor(config=path)
    assert not (files.root / "state.sqlite").exists()
    assert target.read_bytes() == b"unchanged external contents"


def test_cli_can_validate_private_suite_without_creating_database(experiment, tmp_path, monkeypatch, capsys):
    _, runner = experiment
    files = PrivateState(tmp_path / "inspection-only")
    files.write_bytes("suite.json", runner.suite.model_dump_json())
    monkeypatch.setattr(cli, "state_root", files.root)
    cli.validate(suite=files.root / "suite.json")
    assert '"valid": true' in capsys.readouterr().out
    assert not (files.root / "state.sqlite").exists()


def test_cli_can_write_private_schema_without_creating_database(tmp_path, monkeypatch):
    state = tmp_path / "new-state"
    monkeypatch.setattr(cli, "state_root", state)
    cli.schema(output=state / "schema.json")
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE((state / "schema.json").stat().st_mode) == 0o600
    assert (state / ".gitignore").read_bytes() == b"*\n"
    assert stat.S_IMODE((state / ".gitignore").stat().st_mode) == 0o600
    assert not (state / "state.sqlite").exists()


def test_cli_schema_preserves_existing_different_ignore(tmp_path, monkeypatch):
    files = PrivateState(tmp_path / "state")
    files.write_bytes(".gitignore", "custom.txt\n")
    monkeypatch.setattr(cli, "state_root", files.root)
    with pytest.raises(StateError):
        cli.schema(output=files.root / "schema.json")
    assert files.read_bytes(".gitignore") == b"custom.txt\n"
    assert not (files.root / "schema.json").exists()
    assert not (files.root / "state.sqlite").exists()


def test_cli_schema_cannot_overwrite_state_ignore(tmp_path, monkeypatch):
    files = PrivateState(tmp_path / "state")
    files.ensure_ignore()
    monkeypatch.setattr(cli, "state_root", files.root)
    with pytest.raises(EvolutionError, match="exclusion file"):
        cli.schema(output=files.root / ".gitignore")
    assert files.read_bytes(".gitignore") == b"*\n"
    assert not (files.root / "state.sqlite").exists()


def test_create_run_rejects_external_alias_through_internal_file_link(experiment, tmp_path):
    store, _ = experiment
    internal = store.root / "config-link.json"
    internal.symlink_to(tmp_path / "config.json")
    alias = tmp_path / "config-alias.json"
    alias.symlink_to(internal)
    with pytest.raises(StateError):
        create_run(store, alias)


def test_create_run_rejects_external_skill_alias_through_state_link(experiment, tmp_path):
    store, runner = experiment
    internal = store.root / "skill-link"
    internal.symlink_to(tmp_path / "skill", target_is_directory=True)
    alias = tmp_path / "skill-alias"
    alias.symlink_to(internal, target_is_directory=True)
    config = runner.config.model_copy(update={"skill": str(alias)})
    config_path = tmp_path / "aliased-config.json"
    config_path.write_text(config.model_dump_json())
    with pytest.raises(StateError):
        create_run(store, config_path)


def test_create_run_keeps_external_canonical_skill_alias(experiment, tmp_path):
    store, runner = experiment
    alias = tmp_path / "shared-skill-alias"
    alias.symlink_to(tmp_path / "skill", target_is_directory=True)
    config = runner.config.model_copy(update={"skill": str(alias)})
    config_path = tmp_path / "aliased-config.json"
    config_path.write_text(config.model_dump_json())
    ident = create_run(store, config_path)
    assert store.get(ident)["baseline"] == runner.data["baseline"]
    assert store.get(ident)["config"]["skill"] == str(tmp_path / "skill")


@pytest.mark.parametrize("command", ["schema", "validate", "doctor"])
def test_cli_rejects_alias_through_state_directory_link(experiment, tmp_path, monkeypatch, command):
    store, runner = experiment
    outside = tmp_path / "external-inputs"
    outside.mkdir()
    (outside / "suite.json").write_text(runner.suite.model_dump_json())
    (outside / "config.json").write_text(runner.config.model_dump_json())
    (outside / "schema.json").write_text("preserve outside schema")
    internal = store.root / "directory-link"
    internal.symlink_to(outside, target_is_directory=True)
    alias = tmp_path / "external-alias"
    alias.symlink_to(internal, target_is_directory=True)
    monkeypatch.setattr(cli, "state_root", store.root)
    with pytest.raises(StateError):
        if command == "schema":
            cli.schema(output=alias / "schema.json")
        elif command == "validate":
            cli.validate(suite=alias / "suite.json")
        else:
            cli.doctor(config=alias / "config.json")
    assert (outside / "schema.json").read_text() == "preserve outside schema"


def test_cli_keeps_external_alias_to_safe_state_files(experiment, tmp_path, monkeypatch, capsys):
    _, runner = experiment
    files = PrivateState(tmp_path / "separate-state")
    directory = files.ensure_dir("inputs")
    files.write_bytes("inputs/suite.json", runner.suite.model_dump_json())
    alias = tmp_path / "safe-state-alias"
    alias.symlink_to(directory, target_is_directory=True)
    monkeypatch.setattr(cli, "state_root", files.root)
    cli.validate(suite=alias / "suite.json")
    assert '"valid": true' in capsys.readouterr().out
    cli.schema(output=alias / "schema.json")
    assert files.read_bytes("inputs/schema.json")
    assert files.read_bytes(".gitignore") == b"*\n"
    assert not (files.root / "state.sqlite").exists()


@pytest.mark.parametrize("kind", ["mode", "symlink"])
def test_cli_external_suite_still_rejects_unsafe_selected_state(experiment, tmp_path, monkeypatch, kind):
    store, _ = experiment
    root = store.root
    if kind == "mode":
        root.chmod(0o755)
    else:
        root = tmp_path / "state-alias"
        root.symlink_to(store.root, target_is_directory=True)
    monkeypatch.setattr(cli, "state_root", root)
    with pytest.raises(StateError):
        cli.validate(suite=tmp_path / "suite.json")


@pytest.mark.parametrize("exists", [False, True])
def test_cli_external_suite_works_with_missing_or_safe_state(experiment, tmp_path, monkeypatch, capsys, exists):
    state = tmp_path / "inspection-state"
    if exists:
        PrivateState(state)
    monkeypatch.setattr(cli, "state_root", state)
    cli.validate(suite=tmp_path / "suite.json")
    assert '"valid": true' in capsys.readouterr().out
    assert state.exists() is exists
    assert not (state / "state.sqlite").exists()


def prepare_internal_promotion(experiment):
    store, original = experiment
    config = prepare_internal_inputs(store, original)
    runner = Runner(store, create_run(store, config))
    sha = runner.add_candidate("\nInvestigate. SIMULATION_PASS\n")
    runner.select(sha)
    runner.finalize()
    # Synthetic eligible receipts exercise storage only; no actual model call,
    # and no change to the production eligibility or approval conditions.
    runner.data["config"]["targets"][0].update(backend="codex-native", version="0.160.0")
    store.put(runner.data)
    for row in store.evaluations(runner.id):
        row["evidence"] = "live"
        store.save_eval(runner.id, row["key"], row)
    report_sha, _ = report(store, runner.id)
    approve(store, runner.id, sha, report_sha)
    return store, runner, sha, report_sha


def test_state_internal_skill_promotes_and_rolls_back_with_private_lock(experiment):
    store, runner, sha, report_sha = prepare_internal_promotion(experiment)
    target = store.root / "inputs/skill/SKILL.md"
    before = target.read_bytes()
    ident = promote(store, runner.id, sha, report_sha)
    assert b"SIMULATION_PASS" in target.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    lock = store.root / "inputs/.skill.evolution.lock"
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600
    rollback(store, ident)
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(lock.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["promote", "rollback"])
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "mode"])
def test_state_internal_skill_rejects_unsafe_promotion_lock(experiment, tmp_path, operation, kind):
    store, runner, sha, report_sha = prepare_internal_promotion(experiment)
    ident = promote(store, runner.id, sha, report_sha) if operation == "rollback" else None
    target = store.root / "inputs/skill/SKILL.md"
    before = target.read_bytes()
    sentinel = plant_file(store.root / "inputs/.skill.evolution.lock", kind, tmp_path)
    with pytest.raises(StateError):
        if operation == "promote":
            promote(store, runner.id, sha, report_sha)
        else:
            rollback(store, ident)
    assert target.read_bytes() == before
    assert sentinel.read_bytes() == b"unchanged external contents"


def test_state_internal_rollback_rejects_nonprivate_saved_mode(experiment):
    store, runner, sha, report_sha = prepare_internal_promotion(experiment)
    ident = promote(store, runner.id, sha, report_sha)
    # Represent an older stored artifact that cannot be restored under the
    # approved policy; never silently change its mode and report success.
    entry = json.loads(store.db.execute("SELECT data FROM promotions WHERE id=?", (ident,)).fetchone()[0])
    entry["before"]["files"]["SKILL.md"]["mode"] = 0o644
    entry["before"]["hash"] = digest(canonical(entry["before"]["files"]))
    with store.db:
        store.db.execute("UPDATE promotions SET data=? WHERE id=?", (canonical(entry), ident))
    target = store.root / "inputs/skill/SKILL.md"
    before = target.read_bytes()
    with pytest.raises(EvolutionError, match="private state permissions"):
        rollback(store, ident)
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    saved = json.loads(store.db.execute("SELECT data FROM promotions WHERE id=?", (ident,)).fetchone()[0])
    assert saved["status"] == "promoted"
