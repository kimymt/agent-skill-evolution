"""Scan a private snapshot with built-in rules and no task-supplied overrides."""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

from .process import execute
from .util import EvolutionError, atomic_write, canonical, reject_secrets


POLICY = "[extend]\nuseDefault = true\n"
TIMEOUT = 30
FAILURE = "Secret scan failed or found a secret; details withheld"


def _scan(control, binary, arguments, deadline, stdin=""):
    policy, ignore = control / "policy.toml", control / "empty.ignore"
    atomic_write(policy, POLICY)
    atomic_write(ignore, "")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GITLEAKS_")}
    result = execute([binary, *arguments, "--no-banner", "--redact", "--exit-code", "1",
                      "--config", str(policy), "--gitleaks-ignore-path", str(ignore),
                      "--ignore-gitleaks-allow"], control, _remaining(deadline), 100000,
                     env=environment, **({"stdin": stdin} if stdin else {}))
    if result.status != "completed":
        raise EvolutionError(FAILURE)


def _remaining(deadline):
    seconds = deadline - time.monotonic()
    if seconds <= 0:
        raise EvolutionError(FAILURE)
    return seconds


@contextmanager
def _directory(path):
    """Open existing directory components without following any symlinks."""
    path = Path(os.path.abspath(path))
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _reject_ignore(source):
    try:
        os.stat(".gitleaksignore", dir_fd=source, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise EvolutionError("Target .gitleaksignore is not permitted in a secret scan")


def _copy_tree(source, destination, deadline, root=False):
    _remaining(deadline)
    if root:
        _reject_ignore(source)
    with os.scandir(source) as entries:
        for entry in entries:
            _remaining(deadline)
            if root and entry.name == ".gitleaksignore":
                raise EvolutionError("Target .gitleaksignore is not permitted in a secret scan")
            info = os.stat(entry.name, dir_fd=source, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                src = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source)
                try:
                    os.mkdir(entry.name, 0o700, dir_fd=destination)
                    dst = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=destination)
                    try:
                        _copy_tree(src, dst, deadline)
                    finally:
                        os.close(dst)
                finally:
                    os.close(src)
            elif stat.S_ISREG(info.st_mode):
                src = os.open(entry.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=source)
                try:
                    if not stat.S_ISREG(os.fstat(src).st_mode):
                        raise EvolutionError(FAILURE)
                    dst = os.open(entry.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=destination)
                    try:
                        while True:
                            _remaining(deadline)
                            chunk = os.read(src, 65536)
                            if not chunk:
                                break
                            pending = memoryview(chunk)
                            while pending:
                                _remaining(deadline)
                                pending = pending[os.write(dst, pending):]
                    finally:
                        os.close(dst)
                finally:
                    os.close(src)
            else:
                raise EvolutionError("Secret scan requires regular files and directories only")
    if root:
        _reject_ignore(source)


def scan_directory(work: Path, scanner: str | None = None):
    deadline = time.monotonic() + TIMEOUT
    binary = scanner or shutil.which("gitleaks")
    if not binary:
        raise EvolutionError("Gitleaks is required for live runs")
    try:
        binary = str(Path(binary).resolve(strict=True))
        work = Path(os.path.abspath(work))
        # This directory is outside the only workspace exposed to the evaluated agent.
        with tempfile.TemporaryDirectory(prefix="agent-evolution-scan-") as temporary:
            control = Path(temporary).resolve()
            if control.is_relative_to(work):
                raise EvolutionError(FAILURE)
            target = control / "target"
            target.mkdir(mode=0o700)
            with _directory(work) as source, _directory(target) as destination:
                _copy_tree(source, destination, deadline, root=True)
                _reject_ignore(destination)
            _scan(control, binary, ["dir", str(target)], deadline)
    except (OSError, ValueError, RecursionError):
        raise EvolutionError(FAILURE) from None


def _text_parts(value, deadline, serialized=True):
    """Retain serialized context and inspect decoded strings, including JSONL."""
    _remaining(deadline)
    if isinstance(value, str):
        reject_secrets(value)
        # These inputs are text, never binary attachments. Do not allow binary
        # sniffing to silently skip NUL-bearing content.
        if "\x00" in value:
            raise EvolutionError(FAILURE)
        yield value
        try:
            decoded = json.loads(value)
        except ValueError:
            for line in value.splitlines():
                _remaining(deadline)
                try:
                    decoded = json.loads(line)
                except ValueError:
                    continue
                yield from _text_parts(decoded, deadline)
        else:
            yield from _text_parts(decoded, deadline)
    elif isinstance(value, dict):
        if serialized:
            yield canonical(value)
        for key, item in value.items():
            yield from _text_parts(key, deadline, serialized=False)
            yield from _text_parts(item, deadline, serialized=False)
    elif isinstance(value, (list, tuple)):
        if serialized:
            yield canonical(value)
        for item in value:
            yield from _text_parts(item, deadline, serialized=False)
    elif serialized:
        yield canonical(value)


def scan_values(*values, scanner: str | None = None):
    """Inspect in-memory text/JSON with the same fixed policy, via stdin only."""
    deadline = time.monotonic() + TIMEOUT
    binary = scanner or shutil.which("gitleaks")
    if not binary:
        raise EvolutionError("Gitleaks is required for live runs")
    try:
        binary = str(Path(binary).resolve(strict=True))
        parts = [part for value in values for part in _text_parts(value, deadline)]
        for part in parts:
            reject_secrets(part)
        # Inspection-only text envelope; payloads sent to agents/stored as
        # evidence are not rewritten. No plaintext payload is written to disk.
        payload = "Agent evolution text inspection.\n" + "\n".join(parts)
        with tempfile.TemporaryDirectory(prefix="agent-evolution-text-scan-") as temporary:
            _scan(Path(temporary).resolve(), binary, ["stdin"], deadline, stdin=payload)
    except (OSError, ValueError, TypeError, RecursionError):
        raise EvolutionError(FAILURE) from None


def scan_streams(*streams: str, scanner: str | None = None):
    scan_values(*streams, scanner=scanner)
