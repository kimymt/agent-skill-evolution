from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

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
        if r.status != "completed":
            raise EvolutionError("Native sandbox probe failed; no model was called")
        try:
            results = json.loads(r.stdout)
        except ValueError:
            raise EvolutionError("Native sandbox probe returned invalid evidence") from None
        if not results or not all(results.values()):
            raise EvolutionError("Native sandbox isolation incomplete")
        (work / "probe.txt").unlink(missing_ok=True)
        return results


def preflight(profile: Profile):
    if profile.backend == "simulation":
        return
    if profile.backend == "codex-native":
        found = probe("codex")
        if not found["available"] or found["version"] != profile.version:
            raise EvolutionError("Codex CLI version does not match the frozen profile")
        return
    if not shutil.which("docker"):
        raise EvolutionError("Docker is unavailable; no unsandboxed fallback")
    r = execute(["docker", "image", "inspect", profile.image], Path.cwd(), 15, 100000)
    if r.status != "completed":
        raise EvolutionError("Pinned image unavailable; build/install the CLI image explicitly")
    probe_name = "agent-evolution-probe-" + uuid.uuid4().hex
    try:
        version = execute(["docker", "run", "--rm", "--name", probe_name, "--network=none", "--read-only", "--cap-drop=ALL",
                           "--security-opt=no-new-privileges", profile.image, profile.agent, "--version"], Path.cwd(), 20, 20000)
    finally:
        execute(["docker", "rm", "-f", probe_name], Path.cwd(), 10, 10000)
    import re
    found = re.search(r"\b\d+\.\d+\.\d+\b", version.stdout)
    if version.status != "completed" or not found or found[0] != profile.version:
        raise EvolutionError("Container CLI version does not match the frozen profile")
    if profile.network != "none":
        r = execute(["docker", "network", "inspect", profile.network], Path.cwd(), 10, 10000)
        if r.status != "completed" or not json.loads(r.stdout)[0].get("Internal"):
            raise EvolutionError("Docker requires an internal network with a separately controlled model gateway")
    for key in profile.auth_env:
        if not os.environ.get(key):
            raise EvolutionError("A configured model credential is missing")


def run_agent(profile: Profile, work: Path, prompt: str, skill_folder: Path,
              timeout: float, limit: int, cancel: Path):
    preflight(profile)
    argv = command(profile, work, prompt)
    env = {k: v for k, v in os.environ.items() if k in {"PATH", "HOME", "USER", "LANG", "TMPDIR", "SYSTEMROOT"}}
    if profile.backend == "codex-native":
        check_native(work, skill_folder)
        return execute(argv, work, timeout, limit, env=env, cancel=cancel)
    if profile.backend != "docker":
        raise EvolutionError("Simulation never launches an agent")
    name = "agent-evolution-" + uuid.uuid4().hex
    mount_to = "/workspace/" + skill_folder.relative_to(work).as_posix()
    uid, gid = os.getuid(), os.getgid()
    if uid == 0:
        raise EvolutionError("Run the controller as a non-root user")
    wrapper = ["docker", "run", "--rm", "--name", name, "--init", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=128", "--memory=2g", "--cpus=2",
               f"--user={uid}:{gid}", "--network", profile.network,
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m,mode=1777",
               "--tmpfs", f"/home/evaluator:rw,nosuid,size=64m,uid={uid},gid={gid}",
               "--mount", f"type=bind,src={work},dst=/workspace",
               "--mount", f"type=bind,src={skill_folder},dst={mount_to},readonly",
               "--workdir=/workspace"]
    for key in profile.auth_env:
        wrapper += ["--env", key]
        env[key] = os.environ[key]
    wrapper += [profile.image, *argv]
    try:
        return execute(wrapper, work, timeout, limit, env=env, cancel=cancel)
    finally:
        execute(["docker", "rm", "-f", name], work, 10, 10000)
