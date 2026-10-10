from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ProcessResult:
    status: str
    code: int | None
    stdout: str
    stderr: str
    seconds: float


def execute(argv: list[str], cwd: Path, timeout: float, limit: int = 2_000_000,
            env: dict | None = None, cancel: Path | None = None, stdin: str = "") -> ProcessResult:
    started = time.monotonic()
    try:
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    except OSError:
        return ProcessResult("infrastructure_error", None, "", "Process could not start", 0)
    sel = selectors.DefaultSelector()
    outputs = {"stdout": bytearray(), "stderr": bytearray()}
    assert p.stdout and p.stderr and p.stdin
    for stream, name in ((p.stdout, "stdout"), (p.stderr, "stderr")):
        os.set_blocking(stream.fileno(), False)
        sel.register(stream, selectors.EVENT_READ, name)
    pending = memoryview(stdin.encode())
    os.set_blocking(p.stdin.fileno(), False)
    if pending:
        sel.register(p.stdin, selectors.EVENT_WRITE, "stdin")
    else:
        p.stdin.close()
    state = "completed"
    try:
        while sel.get_map():
            if cancel and cancel.exists():
                state = "cancelled"
                break
            if time.monotonic() - started > timeout:
                state = "timeout"
                break
            for key, _ in sel.select(.05):
                if key.data == "stdin":
                    try:
                        n = os.write(key.fd, pending[:8192])
                        pending = pending[n:]
                    except BrokenPipeError:
                        pending = memoryview(b"")
                    if not pending:
                        sel.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                chunk = os.read(key.fd, 65536)
                if not chunk:
                    sel.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                outputs[key.data].extend(chunk)
                if sum(map(len, outputs.values())) > limit:
                    state = "output_limit"
                    break
            if state != "completed":
                break
    except KeyboardInterrupt:
        state = "cancelled"
    finally:
        if state == "completed":
            try:
                p.wait(timeout=.3)
            except subprocess.TimeoutExpired:
                state = "infrastructure_error"
        # Also terminate descendants that inherited pipes after the parent exited.
        try:
            os.killpg(p.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            p.wait(timeout=.3)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        p.wait()
        sel.close()
        for stream in (p.stdin, p.stdout, p.stderr):
            if not stream.closed:
                stream.close()
    if state == "completed" and p.returncode != 0:
        state = "failed"
    return ProcessResult(state, p.returncode, outputs["stdout"].decode(errors="replace"),
                         outputs["stderr"].decode(errors="replace"), time.monotonic() - started)
