from __future__ import annotations

import json
import shutil
import signal
import sys
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .adapters import probe
from .models import AGENTS, Suite, Experiment, load
from .runner import Runner, create_run, read_input_text
from .state import PrivateState, resolve_input_path
from .store import Store
from .util import EvolutionError, canonical, atomic_write

app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
dataset_app = typer.Typer(no_args_is_help=True)
app.add_typer(dataset_app, name="dataset")
state_root = Path(".evolution")


@app.callback()
def options(state: Path = typer.Option(Path(".evolution"), help="Local state directory")):
    global state_root
    state_root = state


def store():
    return Store(state_root)


def _private_path(path: Path, *, create=False):
    root = state_root.absolute()
    try:
        files = PrivateState(root, create=False)
    except FileNotFoundError:
        files = None
    path = resolve_input_path(root, path)
    if path.is_relative_to(root):
        if files is None:
            files = PrivateState(root, create=create)
        return files, path.relative_to(root).as_posix()
    return None


def _load_input(model, path: Path):
    location = _private_path(path)
    if location is None:
        return load(model, path)
    files, relative = location
    try:
        return model.model_validate_json(files.read_bytes(relative))
    except (ValueError, OSError) as error:
        raise EvolutionError(f"Invalid {model.__name__} file: {path.name} ({type(error).__name__})") from None


def show(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))



def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _confirm_adoption(*, action: str, run: str, candidate_sha: str, report_sha: str, target: str, yes: bool,
                      promotion: str | None = None):
    typer.echo(f"action: {action}")
    if promotion is not None:
        typer.echo(f"promotion: {promotion}")
    typer.echo(f"run: {run}")
    typer.echo(f"candidate: {candidate_sha}")
    typer.echo(f"report: {report_sha}")
    typer.echo(f"target: {target}")
    if yes:
        return
    if not _is_interactive():
        typer.echo("Non-interactive confirmation requires --yes", err=True)
        raise typer.Exit(1)
    typer.echo("Continue? [y/N] ", nl=False)
    answer = sys.stdin.readline()
    if answer.strip().lower() not in {"y", "yes"}:
        typer.echo("Confirmation declined", err=True)
        raise typer.Exit(1)


@app.command()
def doctor(agents: str = "claude,codex,opencode,pi", config: Optional[Path] = typer.Option(None)):
    if config is not None:
        from .environment import inspect_profile, inspect_state_path
        selected = _load_input(Experiment, config)
        profiles = [*selected.targets, *([selected.generator] if selected.generator else [])]
        reports = [inspect_profile(profile) for profile in profiles]
        storage = inspect_state_path(state_root)
        ready = all(report["ready"] for report in reports) and storage["status"] == "passed"
        show({"version": __version__, "profiles": reports, "state": storage,
              "ready": ready, "model_called": False, "run_created": False})
        if not ready:
            raise typer.Exit(2)
        return
    names = agents.split(",")
    if any(a not in AGENTS for a in names):
        raise EvolutionError("Unknown agent name")
    show({"version": __version__, "agents": [probe(a) for a in names],
          "docker": bool(shutil.which("docker")), "gitleaks": bool(shutil.which("gitleaks")),
          "model_called": False, "ready": None, "scope": "inventory_only; use doctor --config for evaluation checks"})


@dataset_app.command("validate")
def validate(suite: Path = typer.Option(...)):
    data = _load_input(Suite, suite)
    show({"valid": True, "reviewed": data.reviewed, "cases": len(data.cases),
          "splits": {s: sum(c.split == s for c in data.cases) for s in ("train", "validation", "holdout")}})


@app.command()
def schema(output: Path = typer.Option(...)):
    content = json.dumps(Experiment.model_json_schema(), indent=2)
    location = _private_path(output, create=True)
    if location is None:
        atomic_write(output, content)
    else:
        files, relative = location
        files.ensure_ignore()
        if relative == ".gitignore":
            raise EvolutionError("Schema output cannot replace the state exclusion file")
        files.write_bytes(relative, content)
    show({"schema": str(output)})


@app.command()
def start(config: Path = typer.Option(...)):
    s = store()
    ident = create_run(s, config)
    show({"run": ident, "model_called": False})


@app.command()
def baseline(run: str = typer.Option(...), split: str = "train"):
    s = store()
    s.get(run)
    with s.files.lock(run + ".lock"):
        r = Runner(s, run)
        show(r.evaluate_split(r.data["baseline"], split))


@app.command("import-candidate")
def import_candidate(run: str = typer.Option(...), body: Path = typer.Option(...)):
    s = store()
    s.get(run)
    with s.files.lock(run + ".lock"):
        r = Runner(s, run)
        show({"candidate": r.add_candidate(read_input_text(s, body))})


@app.command()
def evaluate(run: str = typer.Option(...), candidate_sha: str = typer.Option(...), split: str = "validation"):
    s = store()
    s.get(run)
    with s.files.lock(run + ".lock"):
        r = Runner(s, run)
        if candidate_sha not in r.data["artifacts"]:
            raise EvolutionError("Unknown candidate hash")
        show(r.evaluate_split(candidate_sha, split))


@app.command()
def select(run: str = typer.Option(...), candidate_sha: str = typer.Option(...)):
    s = store()
    s.get(run)
    with s.files.lock(run + ".lock"):
        Runner(s, run).select(candidate_sha)
        show({"selected": candidate_sha})


@app.command()
def evolve(run: str = typer.Option(...)):
    from .optimizer import evolve as optimize
    s = store()
    s.get(run)
    with s.files.lock(run + ".lock"):
        r = Runner(s, run)
        if r.config.generator is None:
            raise EvolutionError("Configure a generator profile before invoking GEPA")
        show({"candidate": optimize(r)})


@app.command()
def finalize(run: str = typer.Option(...)):
    s = store()
    s.get(run)
    with s.files.lock(run + ".lock"):
        show(Runner(s, run).finalize())


@app.command()
def report(run: str = typer.Option(...)):
    from .reports import report as build
    sha, folder = build(store(), run)
    show({"report_sha": sha, "report": str(folder / "report.md")})


@app.command()
def approve(run: str = typer.Option(...), candidate_sha: str = typer.Option(...), report_sha: str = typer.Option(...),
            yes: bool = typer.Option(False, "--yes", help="Confirm without a prompt")):
    from .promotion import approve as record
    s = store()
    data = s.get(run)
    _confirm_adoption(action="approve", run=run, candidate_sha=candidate_sha, report_sha=report_sha,
                      target=str(data["config"]["skill"]), yes=yes)
    with s.files.lock(run + ".lock"):
        record(s, run, candidate_sha, report_sha)
        show({"approved": candidate_sha})


@app.command()
def promote(run: str = typer.Option(...), candidate_sha: str = typer.Option(...), report_sha: str = typer.Option(...),
            yes: bool = typer.Option(False, "--yes", help="Confirm without a prompt")):
    from .promotion import promote as apply
    s = store()
    data = s.get(run)
    _confirm_adoption(action="promote", run=run, candidate_sha=candidate_sha, report_sha=report_sha,
                      target=str(data["config"]["skill"]), yes=yes)
    with s.files.lock(run + ".lock"):
        show({"promotion": apply(s, run, candidate_sha, report_sha)})


@app.command()
def rollback(promotion: str = typer.Option(...),
             yes: bool = typer.Option(False, "--yes", help="Confirm without a prompt")):
    from .promotion import rollback as undo
    s = store()
    row = s.db.execute("SELECT data FROM promotions WHERE id=?", (promotion,)).fetchone()
    if not row:
        raise EvolutionError("Unknown promotion ID")
    entry = json.loads(row[0])
    _confirm_adoption(action="rollback", run=str(entry["run"]), candidate_sha=str(entry["after"]["hash"]),
                      report_sha=str(entry["report"]), target=str(entry["target"]), yes=yes,
                      promotion=promotion)
    with s.files.lock("rollback.lock"):
        undo(s, promotion)
    show({"rolled_back": promotion})


@app.command()
def cancel(run: str = typer.Option(...)):
    s = store()
    s.get(run)
    s.files.write_bytes(f"runs/{run}/cancel", "cancelled\n")
    show({"cancel_requested": run})


@app.command()
def demo():
    from .demo import run_demo
    show(run_demo(store()))


def main():
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    try:
        app()
    except EvolutionError as e:
        typer.echo(f"Error: {e}", err=True)
        raise SystemExit(2)
    except KeyboardInterrupt:
        typer.echo("Interrupted", err=True)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
