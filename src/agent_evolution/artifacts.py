from __future__ import annotations

import re
import stat
from pathlib import Path

import yaml

from .util import EvolutionError, canonical, digest, within, atomic_write, reject_secrets


def parse_skill(text: str):
    match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
    if not match:
        raise EvolutionError("SKILL.md requires YAML frontmatter")
    try:
        meta = yaml.safe_load(match[1])
    except yaml.YAMLError:
        raise EvolutionError("Invalid skill frontmatter") from None
    if not isinstance(meta, dict) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(meta.get("name", ""))):
        raise EvolutionError("Invalid skill name")
    if not isinstance(meta.get("description"), str) or not meta["description"].strip():
        raise EvolutionError("Skill description is required")
    body = text[match.end():]
    if not body.strip() or len(text.encode()) > 60_000:
        raise EvolutionError("Skill is empty or exceeds 60 KB")
    reject_secrets(text)
    return meta, text[:match.end()], body


def snapshot(root: Path) -> dict:
    root = root.resolve()
    files = {}
    for p in sorted(root.rglob("*")):
        if p.is_symlink():
            raise EvolutionError("Skill bundles must not contain symlinks")
        if p.is_dir():
            continue
        if not stat.S_ISREG(p.stat().st_mode) or p.stat().st_size > 1_000_000:
            raise EvolutionError("Unsupported artifact file")
        if len(files) >= 200:
            raise EvolutionError("Too many artifact files")
        try:
            text = p.read_text()
        except UnicodeError:
            raise EvolutionError("Initial release supports text skill bundles only") from None
        reject_secrets(text)
        files[p.relative_to(root).as_posix()] = {"text": text, "mode": stat.S_IMODE(p.stat().st_mode)}
    if "SKILL.md" not in files:
        raise EvolutionError("SKILL.md missing")
    meta, _, _ = parse_skill(files["SKILL.md"]["text"])
    return {"name": meta["name"], "files": files, "hash": digest(canonical(files))}


def candidate(baseline: dict, body: str) -> dict:
    _, front, old = parse_skill(baseline["files"]["SKILL.md"]["text"])
    if len(body.encode()) > max(len(old.encode()) * 1.5, 4096):
        raise EvolutionError("Candidate exceeds the growth limit")
    full = front + body
    parse_skill(full)
    files = {k: dict(v) for k, v in baseline["files"].items()}
    files["SKILL.md"]["text"] = full
    return {"name": baseline["name"], "files": files, "hash": digest(canonical(files))}


def verify_snapshot(artifact: dict):
    if digest(canonical(artifact["files"])) != artifact["hash"]:
        raise EvolutionError("Artifact hash mismatch")
    meta, _, _ = parse_skill(artifact["files"]["SKILL.md"]["text"])
    if meta["name"] != artifact["name"]:
        raise EvolutionError("Artifact name mismatch")


def materialize(artifact: dict, root: Path):
    verify_snapshot(artifact)
    for name, data in artifact["files"].items():
        atomic_write(within(root, name), data["text"], data["mode"])

