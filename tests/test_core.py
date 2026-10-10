import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_evolution.artifacts import parse_skill, snapshot, candidate, materialize
from agent_evolution.models import Suite, Case, Check, Profile
from agent_evolution.store import Store
from agent_evolution.process import execute
from agent_evolution.util import EvolutionError, safe_relative, reject_secrets, atomic_write, within


@pytest.mark.parametrize("path", ["../escape", "/abs", "a/../../b", "a\\b", "a//b", ""])
def test_path_escape(path):
    with pytest.raises(EvolutionError):
        safe_relative(path)


def test_symlink_read_refused(tmp_path):
    (tmp_path / "link").symlink_to("/etc/passwd")
    with pytest.raises(EvolutionError):
        within(tmp_path, "link")


def test_frontmatter_and_bundle_preserved(experiment):
    _, r = experiment
    base = r.data["artifacts"][r.data["baseline"]]
    new = candidate(base, "\nBetter investigation procedure.\n")
    assert parse_skill(new["files"]["SKILL.md"]["text"])[1] == parse_skill(base["files"]["SKILL.md"]["text"])[1]
    assert new["files"]["references/info.md"] == base["files"]["references/info.md"]
    assert new["hash"] != base["hash"]


@pytest.mark.parametrize("body", ["", "x" * 70000, "sk-proj-" + "X" * 40])
def test_bad_candidate_refused(experiment, body):
    _, r = experiment
    with pytest.raises(EvolutionError):
        r.add_candidate(body)


def test_secret_error_does_not_echo_value():
    secret = "sk-proj-" + "x" * 40
    with pytest.raises(EvolutionError) as e:
        reject_secrets(secret)
    assert secret not in str(e.value)


def test_group_leakage_refused():
    base = dict(group="same", request="task", files={"a": "b"}, checks=[Check(kind="absent", path="x")])
    with pytest.raises(ValidationError):
        Suite(id="bad", cases=[Case(id="one", split="train", **base), Case(id="two", split="holdout", **base)])


def test_fixture_configuration_refused():
    with pytest.raises(ValidationError):
        Case(id="bad", group="g", split="train", request="task", files={".codex/config.toml": ""}, checks=[Check(kind="absent")])


def test_budget_atomic_across_connections(tmp_path):
    root = tmp_path / "store"
    s = Store(root)
    def reserve(_):
        other = Store(root)
        try:
            other.reserve("run", "task", 3, time.time() + 30)
            return True
        except EvolutionError:
            return False
        finally:
            other.db.close()
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = list(pool.map(reserve, range(12)))
    assert sum(result) == 3
    assert s.used("run") == {"task": 3}


def test_budget_deadline(experiment):
    store, r = experiment
    with pytest.raises(EvolutionError, match="Wall time"):
        store.reserve(r.id, "task", 1, time.time() - 1)
    assert store.used(r.id) == {}


def test_process_timeout(tmp_path):
    result = execute([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path, .15)
    assert result.status == "timeout"
    assert result.seconds < 3


def test_process_output_limit(tmp_path):
    result = execute([sys.executable, "-c", "print('x'*1000000)"], tmp_path, 2, 1024)
    assert result.status == "output_limit"


def test_process_success_and_stderr(tmp_path):
    result = execute([sys.executable, "-c", "import sys; print('ok'); print('err',file=sys.stderr)"], tmp_path, 2)
    assert result.status == "completed" and result.code == 0
    assert result.stdout == "ok\n" and result.stderr == "err\n"


def test_process_cancellation(tmp_path):
    flag = tmp_path / "cancel"
    flag.write_text("stop")
    result = execute([sys.executable, "-c", "import time; time.sleep(20)"], tmp_path, 5, cancel=flag)
    assert result.status == "cancelled"


def test_unknown_native_version_refused():
    with pytest.raises(ValidationError):
        Profile(id="bad", agent="codex", model="m", effort="high", version="9.0.0", backend="codex-native")


@pytest.mark.parametrize("image", ["latest", "image@sha256:short", "image@sha256:" + "z" * 64])
def test_unpinned_docker_refused(image):
    with pytest.raises(ValidationError):
        Profile(id="bad", agent="pi", model="m", effort="high", version="1.0", backend="docker", image=image)
