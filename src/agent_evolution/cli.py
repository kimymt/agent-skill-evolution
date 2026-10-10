from __future__ import annotations

import json
import shutil
import signal
from pathlib import Path
from typing import Optional

import typer

from . import __version__
from .adapters import probe
from .models import Suite, Experiment, load
from .runner import Runner, create_run
from .store import Store
from .util import EvolutionError, canonical, atomic_write, lock

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


def show(value):
    typer.echo(json.dumps(value, ensure_ascii=False, indent=2))


@app.command()
def doctor(agents: str = "claude,codex,opencode,pi"):
    names = agents.split(",")
    if any(a not in {"claude", "codex", "opencode", "pi"} for a in names):
        raise EvolutionError("Unknown agent name")
    show({"version": __version__, "agents": [probe(a) for a in names],
          "docker": bool(shutil.which("docker")), "gitleaks": bool(shutil.which("gitleaks")),
          "model_called": False})


@dataset_app.command("validate")
def validate(suite: Path = typer.Option(...)):
    data = load(Suite, suite)
    show({"valid": True, "reviewed": data.reviewed, "cases": len(data.cases),
          "splits": {s: sum(c.split == s for c in data.cases) for s in ("train", "validation", "holdout")}})


@app.command()
def schema(output: Path = typer.Option(...)):
    atomic_write(output, json.dumps(Experiment.model_json_schema(), indent=2))
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
    with lock(s.root / (run + ".lock")):
        r = Runner(s, run)
        show(r.evaluate_split(r.data["baseline"], split))


@app.command("import-candidate")
def import_candidate(run: str = typer.Option(...), body: Path = typer.Option(...)):
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        r = Runner(s, run)
        show({"candidate": r.add_candidate(body.read_text())})


@app.command()
def evaluate(run: str = typer.Option(...), candidate_sha: str = typer.Option(...), split: str = "validation"):
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        r = Runner(s, run)
        if candidate_sha not in r.data["artifacts"]:
            raise EvolutionError("Unknown candidate hash")
        show(r.evaluate_split(candidate_sha, split))


@app.command()
def select(run: str = typer.Option(...), candidate_sha: str = typer.Option(...)):
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        Runner(s, run).select(candidate_sha)
        show({"selected": candidate_sha})


@app.command()
def evolve(run: str = typer.Option(...)):
    from .optimizer import evolve as optimize
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        r = Runner(s, run)
        if r.config.generator is None:
            raise EvolutionError("Configure a generator profile before invoking GEPA")
        show({"candidate": optimize(r)})


@app.command()
def finalize(run: str = typer.Option(...)):
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        show(Runner(s, run).finalize())


@app.command()
def report(run: str = typer.Option(...)):
    from .reports import report as build
    sha, folder = build(store(), run)
    show({"report_sha": sha, "report": str(folder / "report.md")})


@app.command()
def approve(run: str = typer.Option(...), candidate_sha: str = typer.Option(...), report_sha: str = typer.Option(...)):
    from .promotion import approve as record
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        record(s, run, candidate_sha, report_sha)
        show({"approved": candidate_sha})


@app.command()
def promote(run: str = typer.Option(...), candidate_sha: str = typer.Option(...), report_sha: str = typer.Option(...)):
    from .promotion import promote as apply
    s = store()
    s.get(run)
    with lock(s.root / (run + ".lock")):
        show({"promotion": apply(s, run, candidate_sha, report_sha)})


@app.command()
def rollback(promotion: str = typer.Option(...)):
    from .promotion import rollback as undo
    s = store()
    with lock(s.root / "rollback.lock"):
        undo(s, promotion)
    show({"rolled_back": promotion})


@app.command()
def cancel(run: str = typer.Option(...)):
    s = store()
    s.get(run)
    atomic_write(s.root / "runs" / run / "cancel", "cancelled\n")
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
