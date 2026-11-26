import json
from pathlib import Path

import pytest

from agent_evolution.adapters import command
from agent_evolution.models import Experiment, Profile
from agent_evolution.util import EvolutionError, digest


EXPLICIT_DUMP = (
    '{"id":"test","agent":"codex","model":"chosen-model","effort":"high",'
    '"version":"test","backend":"simulation","image":null,"network":"none","auth_env":[]}'
)
EXPLICIT_DIGEST = "9e69b7044601fca2ed7bfa7938f17d3fc2e3d6a6c5d0fe453f8382b72fa02db7"


def _profile(agent, model="chosen-model"):
    return Profile(id="test", agent=agent, model=model, effort="high", version="test", backend="simulation")


def test_example_config_loads_without_model():
    data = json.loads(Path("examples/codex-smoke.json").read_text())
    assert "model" not in data["targets"][0]
    experiment = Experiment.model_validate(data)
    assert experiment.targets[0].model is None


def test_profile_accepts_omitted_or_null_model_and_rejects_empty():
    assert Profile(id="test", agent="codex", effort="high", backend="simulation").model is None
    assert Profile(id="test", agent="codex", model=None, effort="high", backend="simulation").model is None
    with pytest.raises(Exception):
        Profile(id="test", agent="codex", model="", effort="high", backend="simulation")


def test_explicit_model_profile_fingerprint_is_unchanged():
    profile = _profile("codex")
    assert profile.model_dump_json() == EXPLICIT_DUMP
    assert digest(profile.model_dump_json()) == EXPLICIT_DIGEST


@pytest.mark.parametrize("agent,flag", [
    ("codex", "-m"),
    ("claude", "--model"),
    ("opencode", "--model"),
    ("pi", "--model"),
])
def test_omitted_model_is_not_passed_to_the_adapter(agent, flag, tmp_path):
    argv = command(_profile(agent, model=None), tmp_path, "prompt")
    assert flag not in argv
    assert "chosen-model" not in argv
    assert argv[-1] == "prompt"


@pytest.mark.parametrize("agent,flag", [
    ("codex", "-m"),
    ("claude", "--model"),
    ("opencode", "--model"),
    ("pi", "--model"),
])
def test_explicit_model_is_passed_as_before(agent, flag, tmp_path):
    argv = command(_profile(agent), tmp_path, "prompt")
    assert flag in argv
    assert argv[argv.index(flag) + 1] == "chosen-model"
    assert argv[-1] == "prompt"


def test_schema_marks_model_optional():
    schema = Experiment.model_json_schema()
    required = schema["$defs"]["Profile"]["required"]
    assert "model" not in required
    assert {"id", "agent", "effort", "backend"} <= set(required)
