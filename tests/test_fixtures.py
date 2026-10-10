import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest


CASES = json.loads((Path(__file__).parents[1] / "fixtures/bug-investigation-v1.json").read_text())["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_reviewed_fixture_observation_matches_oracle(case, tmp_path):
    # Execute only these authored, checked-in Python fixtures; no model calls.
    for name, content in case["files"].items():
        (tmp_path / name).write_text(content)
    result = subprocess.run([sys.executable, "-B", "reproduce.py"], cwd=tmp_path,
                            capture_output=True, text=True, check=True, timeout=5)
    expected = next(c["expected"] for c in case["checks"] if c["key"] == "observed")
    observed = ast.literal_eval(result.stdout.strip())
    assert type(observed) is type(expected) and observed == expected
