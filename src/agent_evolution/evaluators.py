from __future__ import annotations

import json
from pathlib import Path

from .models import Case
from .util import within, EvolutionError, reject_secrets
from .adapters import shell_commands
import shlex


def evaluate(case: Case, work: Path, trace: dict) -> dict:
    results = []
    for index, check in enumerate(case.checks):
        ok, reason = False, "mismatch"
        try:
            p = within(work, check.path)
            if check.kind == "absent":
                ok = not p.exists()
            elif check.kind == "command_observed":
                # Evidence that a command ran, not a claim in the final answer.
                expected = shlex.split(str(check.expected))
                ok = any(parts == expected for c in trace.get("commands", []) for parts in shell_commands(c))
            elif p.is_file() and p.stat().st_size <= 1_000_000:
                content = p.read_text()
                reject_secrets(content)
                if check.kind == "unchanged":
                    ok = check.path in case.files and content == case.files[check.path]
                elif check.kind == "file_contains":
                    ok = isinstance(check.expected, str) and check.expected in content
                else:
                    value = json.loads(content)
                    for key in check.key.split(".") if check.key else []:
                        value = value[key]
                    ok = type(value) is type(check.expected) and value == check.expected
            else:
                reason = "missing_or_oversized"
        except (OSError, ValueError, KeyError, TypeError, EvolutionError):
            reason = "unreadable_or_invalid"
        results.append({"id": index, "kind": check.kind, "passed": ok,
                        "mandatory": check.mandatory, "reason": "passed" if ok else reason})
    mandatory = all(c["passed"] for c in results if c["mandatory"])
    return {"checks": results, "hard_pass": mandatory,
            "score": sum(c["passed"] for c in results) / len(results) if mandatory else 0,
            "passed": all(c["passed"] for c in results)}
