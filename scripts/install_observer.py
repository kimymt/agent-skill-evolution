#!/usr/bin/env python3
"""Install a verified observer release and merge only owned registrations."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import stat
import sys
from urllib.parse import unquote, urlsplit

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "scripts"))
sys.path.insert(0, str(PROJECT / "src"))
from observer_bundle import publish, validate
from agent_evolution.util import atomic_write

MARKER = "// Managed by agent-evolution observer installer\n"
MODULES = {"pi": "integrations/pi/index.js", "opencode": "integrations/opencode.js"}
KNOWN_AGENTS = ("codex", "claude", "pi", "opencode", "grok")


def normal_path(path):
    path = Path(os.path.abspath(path))
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise ValueError(f"Symlink registration/release path refused: {component}")
        if component != path and component.exists() and not component.is_dir():
            raise ValueError(f"Non-directory parent refused: {component}")
    return path


def read_registration(path):
    path = normal_path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 2_000_000:
            raise ValueError(f"Invalid registration file: {path}")
        return stream.read()


def release_location(home, runtime, release_root=None):
    manifest = validate(runtime)
    root = normal_path(release_root or home / ".local/share/agent-evolution/observer/releases")
    if root.is_relative_to(PROJECT.parent.resolve()):
        raise ValueError("Observer releases must be outside the Documents workspace")
    return manifest, root / manifest["digest"]


def command_for(runtime, agent):
    config = verified_runtime_config(runtime, validate(runtime))
    return shlex.join([config["python"], "-I", "-S", "-B", str(runtime / "scripts/observe.py"),
                       "--agent", agent, "hook"])


def verified_runtime_config(runtime, manifest):
    data = read_registration(runtime / "observer-runtime.json")
    expected = manifest["files"]["observer-runtime.json"]
    if data is None or len(data) != expected["size"] or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("Runtime configuration changed after verification")
    return json.loads(data)


def managed_command(command, agent, release_root):
    if not isinstance(command, str):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    if args == [str(PROJECT / ".venv/bin/python"), str(PROJECT / "scripts/observe.py"), "--agent", agent, "hook"]:
        return True
    if args[:2] == [str(PROJECT / ".venv/bin/python"), str(PROJECT / "scripts/observe.py")]:
        raise ValueError("Modified observer command preserved; review before replacing")
    if len(args) != 8 or args[1:4] != ["-I", "-S", "-B"] or args[5:] != ["--agent", agent, "hook"]:
        return False
    script = Path(args[4])
    bundle = script.parent.parent
    if script != bundle / "scripts/observe.py" or bundle.parent != release_root or not re.fullmatch(r"[a-f0-9]{64}", bundle.name):
        return False
    validate(bundle)
    return args == shlex.split(command_for(bundle, agent))


def loader(module):
    return (MARKER + f"export {{ default }} from {json.dumps(module.as_uri())};\n").encode()


def managed_loader(before, agent, release_root):
    relative = MODULES[agent]
    if before == loader(PROJECT / relative):
        return True
    try:
        match = re.fullmatch(re.escape(MARKER) + r'export \{ default \} from ("[^\n]+");\n', before.decode())
        if not match:
            return False
        uri = urlsplit(json.loads(match[1]))
        if uri.scheme != "file" or uri.netloc or uri.query or uri.fragment:
            return False
        module = Path(unquote(uri.path))
        bundle = module.parents[len(Path(relative).parts) - 1]
        if module != bundle / relative or bundle.parent != release_root or not re.fullmatch(r"[a-f0-9]{64}", bundle.name):
            return False
        validate(bundle)
        return before == loader(bundle / relative)
    except (UnicodeError, ValueError, IndexError):
        return False


def hook_events(agent):
    if agent == "codex":
        return ["UserPromptSubmit", "PostToolUse", "Stop", "Interrupt"]
    if agent in {"claude", "grok"}:
        return ["UserPromptSubmit", "PostToolUse", "Stop", "PostToolUseFailure"]
    raise ValueError(f"Unknown hook agent: {agent}")


def registration_agent(path):
    path = Path(path)
    if path.name == "hooks.json":
        return "codex"
    if path.name == "settings.json":
        return "claude"
    if path.name == "agent-evolution.json":
        return "grok"
    raise ValueError(f"Unknown registration path: {path}")


def default_codex_hook_paths(home):
    paths = [home / ".codex/hooks.json"]
    work = home / ".codex-work"
    if work.is_dir():
        paths.append(work / "hooks.json")
    return paths


def resolve_codex_hook_paths(home, codex_homes=None):
    if codex_homes is None:
        return default_codex_hook_paths(home)
    if isinstance(codex_homes, str):
        items = [part.strip() for part in codex_homes.split(",") if part.strip()]
    else:
        items = [str(item) for item in codex_homes]
    paths = []
    for item in items:
        base = Path(item)
        if not base.is_absolute():
            base = home / base
        paths.append(base if base.name == "hooks.json" else base / "hooks.json")
    if not paths:
        raise ValueError("No Codex hook paths specified")
    if len(paths) != len(set(path.as_posix() for path in paths)):
        raise ValueError("Duplicate Codex hook paths")
    return paths


def plan_json_hooks(path, agent, command, release_root):
    before = read_registration(path)
    config = json.loads(before) if before is not None else {}
    if not isinstance(config, dict) or not isinstance(config.setdefault("hooks", {}), dict):
        raise ValueError(f"Invalid hook configuration: {path}")
    events = hook_events(agent)
    for event in events:
        groups = config["hooks"].setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Invalid hook event: {event}")
        retained = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list) or any(not isinstance(h, dict) for h in group["hooks"]):
                raise ValueError(f"Invalid hook group: {event}")
            handlers = []
            for handler in group["hooks"]:
                owned = handler.get("type") == "command" and managed_command(handler.get("command"), agent, release_root)
                if owned:
                    if (set(group) != {"hooks"} or set(handler) != {"type", "command", "timeout"}
                            or handler["timeout"] != (3 if event == "Interrupt" else 10)):
                        raise ValueError("Modified observer definition preserved; review before replacing")
                else:
                    handlers.append(handler)
            if handlers or not group["hooks"]:
                retained.append({**group, "hooks": handlers})
        retained.append({"hooks": [{"type": "command", "command": command, "timeout": 3 if event == "Interrupt" else 10}]})
        config["hooks"][event] = retained
    after = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode()
    return before, after


def plan(home, agents, runtime, release_root=None, codex_homes=None):
    home = normal_path(home)
    if not agents or len(set(agents)) != len(agents) or any(a not in KNOWN_AGENTS for a in agents):
        raise ValueError("Unknown or duplicate agent")
    manifest, destination = release_location(home, runtime, release_root)
    config_runtime = verified_runtime_config(runtime, manifest)
    changes = []
    for agent in agents:
        if agent in {"codex", "claude", "grok"}:
            command = shlex.join([config_runtime["python"], "-I", "-S", "-B", str(destination / "scripts/observe.py"),
                                  "--agent", agent, "hook"])
            if agent == "codex":
                paths = resolve_codex_hook_paths(home, codex_homes)
            elif agent == "claude":
                paths = [home / ".claude/settings.json"]
            else:
                paths = [home / ".grok/hooks/agent-evolution.json"]
            for path in paths:
                before, after = plan_json_hooks(path, agent, command, destination.parent)
                if before != after:
                    changes.append((path, before, after))
            continue
        path = home / (".pi/agent/extensions/agent-evolution.js" if agent == "pi" else ".config/opencode/plugins/agent-evolution.js")
        before = read_registration(path)
        if before is not None and not managed_loader(before, agent, destination.parent):
            raise ValueError(f"Existing extension preserved: {path}")
        after = loader(destination / MODULES[agent])
        if before != after:
            changes.append((path, before, after))
    return changes


def prior_managed(path, before, release_root):
    if before is None:
        return None
    if path.suffix == ".js":
        return before.decode()
    agent = registration_agent(path)
    entries = []
    events = set(hook_events(agent))
    for event, groups in json.loads(before).get("hooks", {}).items():
        if event not in events:
            continue
        for gi, group in enumerate(groups):
            for hi, handler in enumerate(group["hooks"]):
                if handler.get("type") == "command" and managed_command(handler.get("command"), agent, release_root):
                    entries.append({"event": event, "group_index": gi, "handler_index": hi,
                                    "group": {k: v for k, v in group.items() if k != "hooks"}, "handler": handler})
    return entries


def install(home, agents, runtime, dry_run=False, release_root=None, codex_homes=None):
    home, runtime = normal_path(home), normal_path(runtime)
    changes = plan(home, agents, runtime, release_root, codex_homes=codex_homes)
    manifest, destination = release_location(home, runtime, release_root)
    result = [{"path": str(path), "runtime": str(destination), "dry_run": dry_run} for path, _, _ in changes]
    if dry_run:
        return result
    # Check all registrations before publishing or changing the first one.
    for path, before, _ in changes:
        if read_registration(path) != before:
            raise ValueError(f"Configuration changed while installing: {path}")
    if publish(runtime, destination.parent) != destination:
        raise ValueError("Unexpected published release path")
    if not changes:
        return []
    receipts = normal_path(destination.parent.parent / "changes")
    receipts.mkdir(parents=True, mode=0o700, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    receipt_path = receipts / f"{timestamp}.json"
    receipt = {"version": 1, "runtime": str(destination), "digest": manifest["digest"], "status": "prepared", "changes": [
        {"path": str(path), "before_sha256": hashlib.sha256(before).hexdigest() if before is not None else None,
         "after_sha256": hashlib.sha256(after).hexdigest(), "previous_managed": prior_managed(path, before, destination.parent), "applied": False}
        for path, before, after in changes]}
    atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode())
    try:
        for index, (path, before, after) in enumerate(changes):
            if read_registration(path) != before:
                raise ValueError(f"Configuration changed while installing: {path}")
            path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            mode = stat.S_IMODE(path.stat().st_mode) if before is not None else 0o600
            atomic_write(path, after, mode=mode)
            receipt["changes"][index]["applied"] = True
            atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode())
    except Exception:
        receipt["status"] = "partial; inspect registrations before continuing; no automatic rollback"
        atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode())
        raise
    receipt["status"] = "applied"
    atomic_write(receipt_path, json.dumps(receipt, ensure_ascii=False, indent=2).encode())
    for entry in result:
        entry["receipt"] = str(receipt_path)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--agents", default="codex,claude,grok")
    parser.add_argument("--codex-homes", default=None,
                        help="Comma-separated Codex homes that contain hooks.json. "
                             "Default: ~/.codex and, if present, ~/.codex-work")
    parser.add_argument("--runtime", type=Path, required=True, help="Verified candidate release directory")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        changes = install(args.home, args.agents.split(","), args.runtime, args.dry_run,
                          codex_homes=args.codex_homes)
    except (ValueError, OSError, KeyError, TypeError):
        print("Observer installation failed; inspect registrations and release changes receipt", file=sys.stderr)
        return 2
    print(json.dumps({"changes": changes,
                      "codex_trust": "Review the new definitions with /hooks; trust records were not changed",
                      "grok_trust": "If hooks are skipped in a project folder, review with /hooks and /hooks-trust; trust records were not changed"},
                     indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
