#!/usr/bin/env python3
"""Build and verify independent observer releases without importing project code."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import errno
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import uuid


LEGACY_FILES = tuple(sorted((
    "scripts/observe.py",
    "src/agent_evolution/__init__.py",
    "src/agent_evolution/observer.py",
    "src/agent_evolution/util.py",
    "integrations/bridge.mjs",
    "integrations/opencode.js",
    "integrations/pi/index.js",
    "observer-runtime.json",
)))
FILES = tuple(sorted((*LEGACY_FILES, "src/agent_evolution/state.py")))
DIRECTORIES = {str(parent) for name in FILES for parent in PurePosixPath(name).parents if str(parent) != "."}


class BundleError(ValueError):
    """A release is incomplete, changed, or unsafe to use."""


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BundleError("Duplicate JSON key")
        result[key] = value
    return result


def _json(data):
    try:
        return json.loads(data, object_pairs_hook=_object)
    except (UnicodeError, ValueError) as error:
        raise BundleError("Invalid bundle JSON") from error


def _config(data):
    value = _json(data)
    if not isinstance(value, dict) or set(value) != {"version", "python", "skill"} or type(value["version"]) is not int or value["version"] != 1:
        raise BundleError("Invalid observer runtime configuration")
    for key in ("python", "skill"):
        path = value[key]
        if not isinstance(path, str) or not path or "\x00" in path or not Path(path).is_absolute() or os.path.normpath(path) != path:
            raise BundleError("Runtime paths must be normalized absolute paths")
    python = Path(value["python"])
    if python.resolve(strict=True) != python or not python.is_file() or not os.access(python, os.X_OK):
        raise BundleError("Runtime Python must be an executable real path")
    return value


@contextmanager
def _directory(path, create=False):
    """Open each component without following symlinks, including the parents."""
    absolute = Path(os.path.abspath(path))
    fd = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in absolute.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


@contextmanager
def _relative_directory(root, parts, create=False):
    fd = os.dup(root)
    try:
        for part in parts:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read(root, name, single_link=False):
    path = PurePosixPath(name)
    with _relative_directory(root, path.parts[:-1]) as parent:
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode) or (single_link and info.st_nlink != 1):
            raise BundleError("Bundle files must be regular files")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(fd, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (not stat.S_ISREG(opened.st_mode) or (single_link and opened.st_nlink != 1)
                    or (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino)):
                raise BundleError("Bundle file changed while opening")
            return stream.read()


def _manifest(contents):
    files = {name: {"sha256": sha256(contents[name]), "size": len(contents[name])} for name in sorted(contents)}
    return {"version": 1, "files": files, "digest": sha256(canonical(files).encode())}


def _check_tree(root, prefix=""):
    if stat.S_IMODE(os.fstat(root).st_mode) != 0o700:
        raise BundleError("Bundle directories must have mode 0700")
    found = set()
    for name in sorted(os.listdir(root)):
        relative = prefix + name
        info = os.stat(name, dir_fd=root, follow_symlinks=False)
        if stat.S_ISDIR(info.st_mode) and relative in DIRECTORIES:
            with _relative_directory(root, (name,)) as child:
                found.update(_check_tree(child, relative + "/"))
        elif stat.S_ISREG(info.st_mode) and relative in {*FILES, "manifest.json"}:
            if info.st_nlink != 1:
                raise BundleError("Bundle files must not have hardlink aliases")
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise BundleError("Bundle files must have mode 0600")
            found.add(relative)
        else:
            raise BundleError("Bundle contains an extra path, symlink, or unsupported file")
    return found


def _validated_contents(root, name):
    found = _check_tree(root)
    if found not in ({*FILES, "manifest.json"}, {*LEGACY_FILES, "manifest.json"}):
        raise BundleError("Bundle is missing required files")
    manifest = _json(_read(root, "manifest.json", single_link=True))
    if not isinstance(manifest, dict) or set(manifest) != {"version", "files", "digest"} or type(manifest["version"]) is not int or manifest["version"] != 1:
        raise BundleError("Invalid bundle manifest")
    files = manifest["files"]
    # Existing registrations may refer to the pre-state-helper release. Validate
    # that exact format for migration, while new builds/publications require FILES.
    if not isinstance(files, dict) or tuple(files) not in (FILES, LEGACY_FILES):
        raise BundleError("Manifest file list does not match the required sorted files")
    if found != {*files, "manifest.json"}:
        raise BundleError("Bundle is missing required files or has an unlisted file")
    for details in files.values():
        if (not isinstance(details, dict) or set(details) != {"sha256", "size"}
                or not isinstance(details["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", details["sha256"])
                or type(details["size"]) is not int or details["size"] < 0):
            raise BundleError("Invalid manifest file metadata")
    contents = {filename: _read(root, filename, single_link=True) for filename in files}
    expected = _manifest(contents)
    if manifest != expected or name != expected["digest"]:
        raise BundleError("Bundle content, digest, or directory name does not match")
    _config(contents["observer-runtime.json"])
    return manifest, contents


def validate(bundle: Path) -> dict:
    bundle = Path(os.path.abspath(bundle))
    with _directory(bundle) as root:
        manifest, _ = _validated_contents(root, bundle.name)
        return manifest


def _write(root, name, data):
    path = PurePosixPath(name)
    with _relative_directory(root, path.parts[:-1], create=True) as parent:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())


def _rename_exclusive(parent, source, destination):
    """Atomic publication must never replace even an empty existing directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function, flag = libc.renameatx_np, 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function, flag = libc.renameat2, 1  # RENAME_NOREPLACE
    else:
        raise BundleError("Atomic no-replace publication is unavailable on this platform")
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(parent, os.fsencode(source), parent, os.fsencode(destination), flag) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def _remove_stage(parent, name):
    with _relative_directory(parent, (name,)) as root:
        for entry in os.listdir(root):
            info = os.stat(entry, dir_fd=root, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                _remove_stage(root, entry)
            else:
                os.unlink(entry, dir_fd=root)
    os.rmdir(name, dir_fd=parent)


def _publish_contents(contents, output_parent):
    if tuple(sorted(contents)) != FILES:
        raise BundleError("New observer releases must include the current runtime files")
    manifest = _manifest(contents)
    output_parent = Path(os.path.abspath(output_parent))
    digest = manifest["digest"]
    with _directory(output_parent, create=True) as parent:
        try:
            with _relative_directory(parent, (digest,)) as existing:
                _validated_contents(existing, digest)
            return output_parent / digest
        except FileNotFoundError:
            pass
        stage = ".observer-stage-" + uuid.uuid4().hex
        os.mkdir(stage, 0o700, dir_fd=parent)
        staged = True
        try:
            with _relative_directory(parent, (stage,)) as root:
                for name in FILES:
                    _write(root, name, contents[name])
                _write(root, "manifest.json", (canonical(manifest) + "\n").encode())
                _validated_contents(root, digest)
                os.fsync(root)
            try:
                _rename_exclusive(parent, stage, digest)
                staged = False
            except OSError as error:
                if error.errno != errno.EEXIST:
                    raise
                with _relative_directory(parent, (digest,)) as existing:
                    _validated_contents(existing, digest)
            os.fsync(parent)
        finally:
            if staged:
                _remove_stage(parent, stage)
    return output_parent / digest


def build(source: Path, output_parent: Path) -> Path:
    with _directory(source) as root:
        contents = {name: _read(root, name) for name in FILES}
    _config(contents["observer-runtime.json"])
    return _publish_contents(contents, output_parent)


def publish(candidate: Path, release_root: Path) -> Path:
    candidate = Path(os.path.abspath(candidate))
    with _directory(candidate) as root:
        _, contents = _validated_contents(root, candidate.name)
    return _publish_contents(contents, release_root)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    builder = commands.add_parser("build")
    builder.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    builder.add_argument("--output", type=Path, required=True)
    verifier = commands.add_parser("verify")
    verifier.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            path = build(args.source, args.output)
            result = {"bundle": str(path), "manifest": validate(path)}
        else:
            result = validate(args.bundle)
    except (OSError, BundleError) as error:
        print(f"Observer bundle failed: {error}", file=sys.stderr)
        return 2
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
