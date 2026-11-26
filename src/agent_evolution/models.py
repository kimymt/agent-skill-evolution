from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator, field_validator

from .util import EvolutionError, safe_relative, reject_secrets

AgentName = Literal["claude", "codex", "opencode", "pi"]
AGENTS = ("claude", "codex", "opencode", "pi")


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Check(Strict):
    kind: Literal["json_equals", "file_contains", "unchanged", "absent", "command_observed"]
    path: str = "report.json"
    key: str = ""
    expected: Any = None
    mandatory: bool = False

    @field_validator("path")
    @classmethod
    def valid_path(cls, v):
        return safe_relative(v)


class Case(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    group: str = Field(min_length=1)
    split: Literal["train", "validation", "holdout"]
    request: str = Field(min_length=1)
    files: dict[str, str]
    checks: list[Check] = Field(min_length=1)
    source: str = "synthetic-reviewed-in-repository"

    @model_validator(mode="after")
    def validate_files(self):
        for name, content in self.files.items():
            safe_relative(name)
            if any(p.startswith(".") for p in Path(name).parts) or Path(name).name in {"AGENTS.md", "CLAUDE.md", "SKILL.md"}:
                raise ValueError("Fixture cannot supply runtime configuration or instructions")
            if len(content.encode()) > 1_000_000:
                raise ValueError("Fixture file too large")
            reject_secrets(content)
        reject_secrets(self.request)
        return self


class Suite(Strict):
    version: Literal[1] = 1
    id: str = Field(min_length=1)
    reviewed: bool = False
    cases: list[Case] = Field(min_length=1)

    @model_validator(mode="after")
    def split_integrity(self):
        ids, groups = set(), {}
        for c in self.cases:
            if c.id in ids or (c.group in groups and groups[c.group] != c.split):
                raise ValueError("Duplicate case or group leakage across splits")
            ids.add(c.id)
            groups[c.group] = c.split
        return self


class Profile(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")
    agent: AgentName
    model: str = Field(min_length=1)
    effort: str = Field(min_length=1)
    version: str | None = Field(default=None, min_length=1)
    backend: Literal["codex-native", "docker", "simulation"]
    image: str | None = None
    network: str = "none"
    auth_env: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def compatible(self):
        if self.backend == "codex-native" and self.agent != "codex":
            raise ValueError("The native backend requires the Codex adapter")
        if self.backend == "docker" and (not self.image or not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", self.image)):
            raise ValueError("Docker image must be pinned by digest")
        for key in self.auth_env:
            if key not in {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"}:
                raise ValueError("Only explicit model API credential variables are supported")
        return self


class Budget(Strict):
    max_task_runs: int = Field(default=200, ge=1, le=10000)
    max_candidates: int = Field(default=4, ge=1, le=100)
    max_generations: int = Field(default=4, ge=0, le=100)
    wall_seconds: int = Field(default=3600, ge=1)
    task_seconds: int = Field(default=180, ge=1, le=3600)
    max_output_bytes: int = Field(default=2_000_000, ge=1024, le=20_000_000)


class Gates(Strict):
    repeats: int = Field(default=3, ge=1, le=20)
    min_holdout_groups: int = Field(default=5, ge=2)
    min_gain: float = Field(default=.05, gt=0, le=1)
    max_time_growth: float = Field(default=.20, ge=0)


class Experiment(Strict):
    skill: str
    suite: str
    targets: list[Profile] = Field(min_length=1)
    budget: Budget = Field(default_factory=Budget)
    gates: Gates = Field(default_factory=Gates)
    generator: Profile | None = None

    @model_validator(mode="after")
    def unique(self):
        if len({p.id for p in self.targets}) != len(self.targets):
            raise ValueError("Duplicate profile ID")
        return self


def load(model, path: Path):
    try:
        return model.model_validate_json(path.read_text())
    except (ValueError, OSError) as e:
        # Pydantic exceptions include raw inputs; never echo those inputs.
        raise EvolutionError(f"Invalid {model.__name__} file: {path.name} ({type(e).__name__})") from None
