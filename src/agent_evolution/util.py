from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path, PurePosixPath


class EvolutionError(Exception):
    """An actionable, sanitized error safe to show in the CLI."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def safe_relative(value: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value or "\x00" in value or str(p) != value:
        raise EvolutionError("Expected a normalized relative path")
    return value


def within(root: Path, value: str) -> Path:
    p = root / safe_relative(value)
    if not p.resolve().is_relative_to(root.resolve()):
        raise EvolutionError("Path escapes its permitted root")
    for part in (p, *p.parents):
        if part == root.parent:
            break
        if part.is_symlink():
            raise EvolutionError("Symlinks are not allowed in evaluation artifacts")
    return p


def atomic_write(path: Path, data: str | bytes, mode: int = 0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".evolution-", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as f:
            f.write(data.encode() if isinstance(data, str) else data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def lock(path: Path):
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise EvolutionError("Another operation holds this lock") from None
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


SECRET = re.compile(r"(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[A-Z0-9]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|(?i:authorization\s*:\s*bearer\s+)\S+)")


def reject_secrets(text: str):
    if SECRET.search(text):
        raise EvolutionError("Secret-like content detected; content withheld")


def redact(text: str) -> str:
    return SECRET.sub("[REDACTED]", text)

