from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Callable

from .adapters import command, probe, codex_permissions
from .models import Profile
from .process import execute
from .util import EvolutionError


def check_native(work: Path, skill_folder: Path) -> dict:
    """Verify OS read/write/network denial without a model call."""
    with tempfile.TemporaryDirectory(prefix="agent-evolution-private-") as outside:
        sentinel = Path(outside) / "private.txt"
        sentinel.write_text("not agent data")
        code = '''import json,pathlib,socket,sys
w=pathlib.Path(sys.argv[1]); outside=pathlib.Path(sys.argv[2]); skill=pathlib.Path(sys.argv[3])
r={}
try:
 (w/'probe.txt').write_text('ok'); r['workspace_write']=True
except OSError: r['workspace_write']=False
for name,op in [('outside_read',lambda:outside.read_text()),('outside_write',lambda:outside.write_text('bad')),('skill_write',lambda:skill.write_text('bad'))]:
 try: op(); r[name]=False
 except PermissionError: r[name]=True
try:
 s=socket.socket(); s.settimeout(1); s.connect(('127.0.0.1',9)); r['network_denied']=False
except PermissionError: r['network_denied']=True
except OSError: r['network_denied']=False
print(json.dumps(r))
sys.exit(0 if all(r.values()) else 1)
'''
        argv = ["codex", "sandbox", "-P", "evolution", "-C", str(work), *codex_permissions(work),
                "--", sys._base_executable, "-c", code, str(work), str(sentinel), str(skill_folder / "SKILL.md")]
        r = execute(argv, work, 20, 20000)
        results = isolation_evidence(r, "Native sandbox")
        (work / "probe.txt").unlink(missing_ok=True)
        return results


class ProbeUnverified(EvolutionError):
    """The host could not produce usable isolation evidence."""


def preflight(profile: Profile) -> dict:
    # Keep one preflight contract for doctor, start, trials and generation.
    from .environment import inspect_profile, require_ready
    return require_ready(inspect_profile(profile))


def container_command(profile: Profile, work: Path, skill_folder: Path, name: str) -> list[str]:
    mount_to = "/workspace/" + skill_folder.relative_to(work).as_posix()
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise EvolutionError("Run the controller as a non-root user")
    return ["docker", "run", "--rm", "--name", name, "--init", "--read-only", "--cap-drop=ALL",
            "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2g", "--cpus=2",
            f"--user={uid}:{gid}", "--network", profile.network,
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,mode=1777",
            "--tmpfs", f"/home/evaluator:rw,nosuid,size=64m,uid={uid},gid={gid}",
            "--env", "HOME=/home/evaluator",
            "--mount", f"type=bind,src={work},dst=/workspace",
            "--mount", f"type=bind,src={skill_folder},dst={mount_to},readonly",
            "--workdir=/workspace"]


def check_docker(profile: Profile, work: Path, skill_folder: Path) -> dict:
    """Exercise the same container mounts and user as evaluations; no model or credentials."""
    code = r'''import errno,json,pathlib,socket,sys
w=pathlib.Path('/workspace'); skill=pathlib.Path(sys.argv[1]); outside=pathlib.Path(sys.argv[2])
r={}
try:
 (w/'probe.txt').write_text('ok'); r['workspace_write']=True
except OSError: r['workspace_write']=False
try:
 outside.read_text(); r['outside_read']=False
except (FileNotFoundError,PermissionError): r['outside_read']=True
for name,path in [('outside_write',pathlib.Path('/evolution-outside-probe')),('skill_write',skill)]:
 try: path.write_text('bad'); r[name]=False
 except OSError as e: r[name]=e.errno in (errno.EACCES,errno.EPERM,errno.EROFS)
try:
 s=socket.socket(); s.settimeout(1); s.connect(('192.0.2.1',9)); r['network_denied']=False
except OSError as e: r['network_denied']=e.errno in (errno.EACCES,errno.EPERM,errno.ENETUNREACH)
print(json.dumps(r))
sys.exit(0 if all(r.values()) else 1)
'''
    with tempfile.TemporaryDirectory(prefix="agent-evolution-private-") as outside:
        sentinel = Path(outside) / "private.txt"
        sentinel.write_text("not agent data")
        name = "agent-evolution-probe-" + uuid.uuid4().hex
        target = "/workspace/" + (skill_folder / "SKILL.md").relative_to(work).as_posix()
        try:
            result = execute([*container_command(profile, work, skill_folder, name), profile.image,
                              "python3", "-c", code, target, str(sentinel)], work, 20, 20000)
        finally:
            execute(["docker", "rm", "-f", name], work, 10, 10000)
        return isolation_evidence(result, "Container")


def isolation_evidence(result, label: str) -> dict:
    try:
        values = json.loads(result.stdout)
    except ValueError:
        raise ProbeUnverified(f"{label} isolation probe could not run; no model was called") from None
    expected = {"workspace_write", "outside_read", "outside_write", "skill_write", "network_denied"}
    if not isinstance(values, dict) or set(values) != expected or any(type(v) is not bool for v in values.values()):
        raise ProbeUnverified(f"{label} isolation probe returned invalid evidence")
    if not all(values.values()):
        raise EvolutionError(f"{label} isolation contract failed")
    if result.status != "completed":
        raise ProbeUnverified(f"{label} isolation probe did not complete")
    return values


def run_agent(profile: Profile, work: Path, prompt: str, skill_folder: Path,
              timeout: float, limit: int, cancel: Path | Callable[[], bool], expected_identity: dict | None = None):
    from .environment import runtime_identity
    if expected_identity is None:
        expected_identity = preflight(profile)
    if runtime_identity(profile) != expected_identity:
        raise EvolutionError("Runtime changed before launch; start a new run")
    argv = command(profile, work, prompt)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "USER", "LANG", "TMPDIR", "SYSTEMROOT"}}
    if profile.backend == "codex-native":
        check_native(work, skill_folder)
        return execute(argv, work, timeout, limit, env=env, cancel=cancel)
    if profile.backend != "docker":
        raise EvolutionError("Simulation never launches an agent")
    name = "agent-evolution-" + uuid.uuid4().hex
    wrapper = container_command(profile, work, skill_folder, name)
    for key in profile.auth_env:
        wrapper += ["--env", key]
        env[key] = os.environ[key]
    wrapper += [profile.image, *argv]
    try:
        return execute(wrapper, work, timeout, limit, env=env, cancel=cancel)
    finally:
        execute(["docker", "rm", "-f", name], work, 10, 10000)
