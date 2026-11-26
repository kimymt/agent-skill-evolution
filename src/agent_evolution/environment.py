"""Model-free capability checks and per-run runtime identities, without a version allowlist."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

from .adapters import command, codex_permissions
from .models import AGENTS, Profile
from .process import execute
from .secret_scan import scan_directory
from .util import EvolutionError, canonical, digest

# Each adapter owns explicit invocation requirements; unknown products never fall through.
REQUIRED_FLAGS = {
    "codex": ("--json", "--ephemeral", "--ignore-user-config", "--ignore-rules", "--strict-config", "--model", "--config"),
    "claude": ("--output-format", "--model", "--effort", "--setting-sources", "--strict-mcp-config", "--mcp-config", "--allowedTools"),
    "opencode": ("--format", "--model", "--variant"),
    "pi": ("--mode", "--no-session", "--no-extensions", "--no-prompt-templates", "--no-context-files", "--model", "--thinking"),
}


def _version(output: str) -> str:
    match = re.search(r"\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b", output)
    if not match:
        raise EvolutionError("CLI did not report a recognizable version")
    return match[0]


def executable_identity(name: str) -> dict:
    binary = shutil.which(name)
    if not binary:
        raise EvolutionError("Required executable is unavailable: " + name)
    try:
        resolved = Path(binary).resolve(strict=True)
        fingerprint = digest(resolved.read_bytes())
    except OSError:
        raise EvolutionError("Executable identity could not be read") from None
    result = execute([binary, "--version"], Path.cwd(), 10, 20000)
    if result.status != "completed":
        raise EvolutionError("Executable version check failed")
    return {"binary": binary, "resolved": str(resolved), "sha256": fingerprint,
            "version": _version(result.stdout)}


def _container_probe(profile: Profile, work: Path, skill: Path, args: list[str]):
    from .isolation import container_command
    name = "agent-evolution-inspect-" + uuid.uuid4().hex
    try:
        return execute([*container_command(profile, work, skill, name), profile.image, *args], work, 20, 100000)
    finally:
        execute(["docker", "rm", "-f", name], work, 10, 10000)


def _workspace(root: Path, profile: Profile) -> tuple[Path, Path]:
    work = root / "workspace"
    work.mkdir()
    location = ".claude/skills" if profile.agent == "claude" else ".agents/skills"
    skill = work / location / "capability-probe"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: capability-probe\ndescription: Offline probe\n---\nProbe only.\n")
    return work.resolve(), skill.resolve()


def runtime_identity(profile: Profile) -> dict:
    if profile.agent not in AGENTS:
        raise EvolutionError("Unknown agent adapter")
    identity = {"agent": profile.agent, "backend": profile.backend,
                "profile_hash": digest(profile.model_dump_json()),
                "adapter_hash": digest((Path(__file__).parent / "adapters.py").read_bytes())}
    if profile.backend == "simulation":
        return {**identity, "version": "simulation"}
    identity["scanner"] = executable_identity("gitleaks")
    if profile.backend == "codex-native":
        identity.update(executable_identity("codex"))
        identity["settings_hash"] = digest(canonical(codex_permissions(Path.cwd())))
        identity["probe_python"] = str(Path(sys._base_executable).resolve())
    else:
        identity["docker"] = executable_identity("docker")
        result = execute(["docker", "image", "inspect", profile.image], Path.cwd(), 15, 100000)
        try:
            data = json.loads(result.stdout)[0]
            image_id = data["Id"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise EvolutionError("Pinned Docker image is unavailable") from None
        if result.status != "completed" or not isinstance(image_id, str) or not image_id.startswith("sha256:"):
            raise EvolutionError("Pinned Docker image identity is invalid")
        identity.update(image=profile.image, image_id=image_id)
        if profile.network != "none":
            result = execute(["docker", "network", "inspect", profile.network], Path.cwd(), 10, 100000)
            try:
                network = json.loads(result.stdout)[0]
                if result.status != "completed" or network.get("Internal") is not True:
                    raise ValueError
                identity["network_id"] = network["Id"]
            except (ValueError, KeyError, IndexError, TypeError):
                raise EvolutionError("Docker requires an internal network with a controlled model gateway") from None
        with tempfile.TemporaryDirectory(prefix="agent-evolution-identify-") as temp:
            work, skill = _workspace(Path(temp), profile)
            version = _container_probe(profile, work, skill, [profile.agent, "--version"])
        if version.status != "completed":
            raise EvolutionError("Container CLI version check failed")
        identity["version"] = _version(version.stdout)
    return identity


def inspect_profile(profile: Profile) -> dict:
    """No model, run, reservation, or stored credential is created or read."""
    from .isolation import check_native, check_docker, ProbeUnverified
    checks = []
    report = {"profile": profile.id, "agent": profile.agent, "backend": profile.backend,
              "requested_version": profile.version or "auto", "identity": None, "checks": checks,
              "model_called": False, "ready": False}

    def add(name, status, detail, required=True):
        checks.append({"check": name, "status": status, "required": required, "detail": detail})

    if profile.agent not in REQUIRED_FLAGS:
        add("adapter", "failed", "Unknown agent adapter")
        return report
    add("adapter", "passed", "Registered adapter")
    try:
        identity = runtime_identity(profile)
    except EvolutionError as error:
        add("runtime", "failed", str(error))
        return report
    report["identity"] = identity
    add("runtime", "passed", "Executable and backend identities recorded")
    exact = profile.version not in (None, "auto")
    if profile.backend != "simulation" and exact and identity["version"] != profile.version:
        add("version", "failed", "Actual CLI version does not match the explicitly requested version")
        return report
    add("version", "passed", "Exact requested version matched" if exact else "Observed version will be frozen in the run")
    if profile.backend == "simulation":
        add("simulation", "passed", "No model or CLI is used; simulation cannot qualify for promotion")
        report["ready"] = True
        return report
    add("secret_scanner", "passed", "Gitleaks executable is available")
    if profile.backend == "docker":
        missing = any(not os.environ.get(key) for key in profile.auth_env)
        add("credential_environment", "failed" if missing else "passed",
            "A configured credential variable is absent" if missing else "Configured variables are present; values were not inspected")
    try:
        with tempfile.TemporaryDirectory(prefix="agent-evolution-capability-") as temp:
            work, skill = _workspace(Path(temp), profile)
            args = command(profile, work, "")[:-1] + ["--help"]
            help_result = (execute(args, work, 15, 100000) if profile.backend == "codex-native"
                           else _container_probe(profile, work, skill, args))
            supported = help_result.status == "completed" and all(flag in help_result.stdout for flag in REQUIRED_FLAGS[profile.agent])
            add("arguments", "passed" if supported else "failed",
                "Required invocation flags accepted by help; this is not a model execution" if supported else "Required invocation flags were not confirmed")
            if profile.backend == "codex-native":
                # `features` has no --strict-config/--ignore-user-config flags.
                # Confirm the explicit overrides here; exec retains all three flags.
                settings = execute(["codex", *codex_permissions(work),
                                    "-c", "model_reasoning_effort=" + json.dumps(profile.effort),
                                    "features", "list"], work, 15, 100000)
                effective = {line.split()[0]: line.split()[-1] for line in settings.stdout.splitlines() if len(line.split()) >= 3}
                disabled = ("hooks", "memories", "multi_agent", "plugins", "remote_plugin", "apps", "js_repl", "code_mode")
                matched = settings.status == "completed" and all(effective.get(name) == "false" for name in disabled) and effective.get("unified_exec") == "true"
                add("settings", "passed" if matched else "unverified",
                    "Feature overrides confirmed through host configuration diagnostics; exec separately ignores user config and rules and requires strict config" if matched else "Required effective configuration could not be confirmed")
            else:
                add("settings", "unverified",
                    "Adapter-specific effective configuration probe is not available; help and fresh container home do not prove CLI settings")
            try:
                details = check_native(work, skill) if profile.backend == "codex-native" else check_docker(profile, work, skill)
                add("isolation", "passed", "Workspace, outside paths, skill writes and direct network probes passed")
                checks[-1]["evidence"] = details
            except ProbeUnverified as error:
                add("isolation", "unverified", str(error))
            except EvolutionError as error:
                add("isolation", "failed", str(error))
            try:
                scan_directory(work, identity["scanner"]["resolved"])
            except EvolutionError:
                add("secret_scan", "failed", "Secret scanner did not complete")
            else:
                add("secret_scan", "passed", "Offline fixture scan completed")
    except OSError:
        add("probe_workspace", "unverified", "Temporary probe workspace could not be created or read")
    try:
        unchanged = runtime_identity(profile) == identity
        add("stable_runtime", "passed" if unchanged else "failed", "Runtime unchanged during probes" if unchanged else "Runtime changed during probes")
    except EvolutionError:
        add("stable_runtime", "failed", "Runtime identity could not be rechecked")
    add("live_auth_and_events", "unverified", "Authentication, model connection and real event format require an authorized model execution", required=False)
    report["ready"] = all(item["status"] == "passed" for item in checks if item["required"])
    return report


def require_ready(report: dict) -> dict:
    if not report["ready"]:
        failed = ", ".join(item["check"] + ":" + item["status"] for item in report["checks"] if item["required"] and item["status"] != "passed")
        raise EvolutionError("Evaluation preflight did not pass (" + failed + "); run doctor --config for details")
    return report["identity"]


def inspect_state_path(path: Path) -> dict:
    """Apply the same private-state policy without creating a state or database."""
    from .state import PrivateState, inspect_path
    from .store import DATABASE_FILES
    try:
        inspect_path(path)
        try:
            state = PrivateState(path, create=False)
        except FileNotFoundError:
            pass
        else:
            for name in DATABASE_FILES:
                state.file_info(name, missing_ok=True)
            ignore = state.read_bytes(".gitignore", missing_ok=True, max_bytes=16)
            if ignore is not None and ignore not in {b"*", b"*\n"}:
                raise EvolutionError("State Git exclusion must contain only *")
        return {"check": "state_path", "status": "passed", "required": True, "path": str(path.absolute()),
                "detail": "Private-state path, existing database files and write access verified; no state database created"}
    except (OSError, EvolutionError):
        return {"check": "state_path", "status": "failed", "required": True, "path": str(path.absolute()),
                "detail": "State path violates ownership, permissions, ACL, link or Git exclusion policy, or is not writable"}
