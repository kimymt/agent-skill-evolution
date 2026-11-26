"""Private state paths; this is not isolation from another process with our UID."""
from __future__ import annotations

import ctypes
import errno
import fcntl
import os
from pathlib import Path, PurePosixPath
import stat
import sys
import uuid
from contextlib import contextmanager
from functools import lru_cache

from .util import EvolutionError


class StateError(EvolutionError):
    MESSAGES = {
        "E_UNSAFE_PATH": "State path has an unsafe link, type or name",
        "E_PERMISSION": "State ownership, permissions or ACL are not permitted",
        "E_IO": "State filesystem operation could not be completed",
        "E_BUSY": "Another operation holds the state lock",
        "E_STATE_CORRUPT": "State file content is invalid or exceeds its size limit",
    }

    def __init__(self, code: str):
        self.code = code if code in self.MESSAGES else "E_IO"
        super().__init__(self.MESSAGES[self.code])


@contextmanager
def _errors():
    try:
        yield
    except FileNotFoundError:
        raise
    except OSError as error:
        code = ("E_UNSAFE_PATH" if error.errno in {errno.ELOOP, errno.ENOTDIR}
                else "E_PERMISSION" if error.errno in {errno.EACCES, errno.EPERM, errno.EROFS}
                else "E_IO")
        raise StateError(code) from None


@lru_cache(maxsize=1)
def _darwin_libc():
    lib = ctypes.CDLL(None, use_errno=True)
    definitions = {
        "acl_get_fd_np": ([ctypes.c_int, ctypes.c_int], ctypes.c_void_p),
        "acl_get_entry": ([ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p)], ctypes.c_int),
        "acl_get_tag_type": ([ctypes.c_void_p, ctypes.POINTER(ctypes.c_int)], ctypes.c_int),
        "acl_free": ([ctypes.c_void_p], ctypes.c_int),
    }
    for name, (arguments, result) in definitions.items():
        function = getattr(lib, name)
        function.argtypes, function.restype = arguments, result
    return lib


def _check_acl(fd: int, *, private: bool):
    """Inspect the opened inode, never a path resolved independently of it."""
    try:
        if sys.platform == "darwin":
            lib = _darwin_libc()
            ctypes.set_errno(0)
            acl = lib.acl_get_fd_np(fd, 0x100)  # ACL_TYPE_EXTENDED
            if not acl:
                # Darwin filesec_get_property(FILESEC_ACL) uses ENOENT when
                # the opened inode has no extended ACL property.
                if ctypes.get_errno() == errno.ENOENT:
                    return
                raise StateError("E_PERMISSION")
            try:
                # Darwin returns 0 for an entry and -1/EINVAL at the end.
                for index in range(256):
                    entry = ctypes.c_void_p()
                    ctypes.set_errno(0)
                    result = lib.acl_get_entry(acl, index, ctypes.byref(entry))
                    if result == -1 and ctypes.get_errno() == errno.EINVAL:
                        return
                    if result != 0 or not entry.value:
                        raise StateError("E_PERMISSION")
                    tag = ctypes.c_int()
                    if lib.acl_get_tag_type(entry, ctypes.byref(tag)) != 0:
                        raise StateError("E_PERMISSION")
                    if private or tag.value != 2:  # ACL_EXTENDED_DENY only on ancestors
                        raise StateError("E_PERMISSION")
                raise StateError("E_PERMISSION")
            finally:
                lib.acl_free(acl)
        elif sys.platform.startswith("linux"):
            # POSIX ACLs contain grants, not Darwin-style deny entries. Also
            # refuse the alternate ACL representations used by some filesystems.
            names = os.listxattr(fd)
            if any(name in names for name in ("system.nfs4_acl", "system.richacl")):
                raise StateError("E_PERMISSION")
            for name in ("system.posix_acl_access", "system.posix_acl_default"):
                try:
                    os.getxattr(fd, name)
                except OSError as error:
                    if error.errno == errno.ENODATA:
                        continue
                    raise
                raise StateError("E_PERMISSION")
        else:
            raise StateError("E_PERMISSION")
    except (OSError, AttributeError, TypeError):
        # Unsupported or failed queries are not evidence that an ACL is absent.
        raise StateError("E_PERMISSION") from None


def validate_fd(fd: int, *, directory: bool, private: bool = True,
                allow_sticky: bool = False) -> os.stat_result:
    """Validate metadata and ACL of an open object without changing it.

    allow_sticky is internal to the standard temporary ancestor traversal;
    callers validating state objects should retain the private=True default.
    """
    with _errors():
        info = os.fstat(fd)
        expected = stat.S_ISDIR if directory else stat.S_ISREG
        if not expected(info.st_mode) or (not directory and info.st_nlink != 1):
            raise StateError("E_UNSAFE_PATH")
        uid, mode = os.geteuid(), stat.S_IMODE(info.st_mode)
        if private:
            if info.st_uid != uid or mode != (0o700 if directory else 0o600):
                raise StateError("E_PERMISSION")
        else:
            sticky = (directory and allow_sticky and info.st_uid == 0
                      and bool(mode & stat.S_ISVTX))
            if info.st_uid not in {0, uid} or (mode & 0o022 and not sticky):
                raise StateError("E_PERMISSION")
        _check_acl(fd, private=private)
        return info


def _absolute(root) -> Path:
    try:
        root = Path(root)
        if ".." in root.parts or "\x00" in os.fspath(root):
            raise StateError("E_UNSAFE_PATH")
        return Path(os.path.abspath(root))  # lexical only; never resolve symlinks
    except (TypeError, ValueError):
        raise StateError("E_UNSAFE_PATH") from None


def resolve_input_path(root, path) -> Path:
    """Resolve external aliases, preserving every component after entering state.

    A final-target-only Path.resolve() can erase a state-internal symlink which
    redirects outside state. Callers must validate the state root separately and
    use PrivateState for a returned path inside it (including any '..' tail).
    """
    root = _absolute(root)
    try:
        path = Path(path)
        if "\x00" in os.fspath(path):
            raise StateError("E_UNSAFE_PATH")
        if not path.is_absolute():
            path = Path.cwd() / path
    except (TypeError, ValueError):
        raise StateError("E_UNSAFE_PATH") from None
    current = Path(path.anchor)
    pending = list(path.parts[1:])
    links = 0
    with _errors():
        while pending:
            if current.is_relative_to(root):
                return current.joinpath(*pending)
            part = pending.pop(0)
            if part == "..":
                current = current.parent
                continue
            candidate = current / part
            if candidate.is_relative_to(root):
                return candidate.joinpath(*pending)
            try:
                info = candidate.lstat()
            except FileNotFoundError:
                current = candidate
                continue
            if not stat.S_ISLNK(info.st_mode):
                current = candidate
                continue
            links += 1
            if links > 40:
                raise StateError("E_UNSAFE_PATH")
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                current = Path(target.anchor)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        return current


def _parts(relative, *, empty=False) -> tuple[str, ...]:
    value = os.fspath(relative)
    if empty and value in {"", "."}:
        return ()
    path = PurePosixPath(value)
    if (not value or path.is_absolute() or ".." in path.parts or "\\" in value
            or "\x00" in value or str(path) != value or value == "."):
        raise StateError("E_UNSAFE_PATH")
    return path.parts


_DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
_FILE = os.O_NOFOLLOW | os.O_NONBLOCK
_TEMP_ANCESTORS = {Path("/tmp"), Path("/private/tmp"), Path("/var/tmp"), Path("/private/var/tmp")}


@contextmanager
def _root_directory(root: Path, *, create: bool, nearest=False):
    with _errors():
        fd = os.open(root.anchor, _DIRECTORY)
        path = Path(root.anchor)
        sticky_parent = False
        try:
            validate_fd(fd, directory=True, private=path == root)
            for part in root.parts[1:]:
                next_path = path / part
                created = False
                try:
                    child = os.open(part, _DIRECTORY, dir_fd=fd)
                except FileNotFoundError:
                    if nearest:
                        break
                    if not create:
                        raise
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                        created = True
                    except FileExistsError:
                        pass
                    child = os.open(part, _DIRECTORY, dir_fd=fd)
                try:
                    info = validate_fd(child, directory=True, private=created or next_path == root,
                                       allow_sticky=next_path in _TEMP_ANCESTORS)
                    if sticky_parent and info.st_uid != os.geteuid():
                        raise StateError("E_PERMISSION")
                except BaseException:
                    os.close(child)
                    raise
                os.close(fd)
                fd, path = child, next_path
                sticky_parent = (path in _TEMP_ANCESTORS and info.st_uid == 0
                                 and bool(info.st_mode & stat.S_ISVTX))
            yield fd, path
        finally:
            os.close(fd)


def _open_file(parent: int, name: str, *, create=False, writable=False) -> int:
    # Check type before open to avoid opening a planted device or FIFO. The
    # opened object is checked again; this does not isolate hostile same-UID code.
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if not create:
            raise
    else:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise StateError("E_UNSAFE_PATH")
    flags = _FILE | (os.O_RDWR if writable else os.O_RDONLY)
    if create:
        try:
            fd = os.open(name, flags | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent)
        except FileExistsError:
            fd = os.open(name, flags, dir_fd=parent)
    else:
        fd = os.open(name, flags, dir_fd=parent)
    try:
        validate_fd(fd, directory=False)
    except BaseException:
        os.close(fd)
        raise
    return fd


class PrivateState:
    def __init__(self, root, create=True):
        self.root = _absolute(root)
        with _root_directory(self.root, create=create):
            pass

    @contextmanager
    def directory(self, relative="", create=False):
        parts = _parts(relative, empty=True)
        with _root_directory(self.root, create=False) as (root_fd, _), _errors():
            fd = os.dup(root_fd)
            try:
                for part in parts:
                    if create:
                        try:
                            os.mkdir(part, 0o700, dir_fd=fd)
                        except FileExistsError:
                            pass
                    child = os.open(part, _DIRECTORY, dir_fd=fd)
                    try:
                        validate_fd(child, directory=True)
                    except BaseException:
                        os.close(child)
                        raise
                    os.close(fd)
                    fd = child
                yield fd
            finally:
                os.close(fd)

    def ensure_dir(self, relative):
        parts = _parts(relative, empty=True)
        with self.directory(relative, create=True):
            pass
        return self.root.joinpath(*parts)

    @contextmanager
    def _file(self, relative, *, create=False, writable=False):
        parts = _parts(relative)
        with self.directory("/".join(parts[:-1]), create=create) as parent, _errors():
            fd = _open_file(parent, parts[-1], create=create, writable=writable)
            try:
                yield fd
            finally:
                os.close(fd)

    def file_info(self, relative, missing_ok=False):
        try:
            with self._file(relative) as fd:
                return os.fstat(fd)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise

    def ensure_file(self, relative):
        with self._file(relative, create=True):
            pass
        return self.root.joinpath(*_parts(relative))

    def read_bytes(self, relative, missing_ok=False, max_bytes=None):
        if max_bytes is not None and (type(max_bytes) is not int or max_bytes < 0):
            raise StateError("E_STATE_CORRUPT")
        try:
            with self._file(relative) as fd:
                result = bytearray()
                while True:
                    amount = 65536 if max_bytes is None else min(65536, max_bytes + 1 - len(result))
                    chunk = os.read(fd, amount)
                    if not chunk:
                        return bytes(result)
                    result.extend(chunk)
                    if max_bytes is not None and len(result) > max_bytes:
                        raise StateError("E_STATE_CORRUPT")
        except FileNotFoundError:
            if missing_ok:
                return None
            raise

    def write_bytes(self, relative, data):
        parts = _parts(relative)
        data = data.encode() if isinstance(data, str) else data
        with self.directory("/".join(parts[:-1]), create=True) as parent, _errors():
            try:
                existing = _open_file(parent, parts[-1])
            except FileNotFoundError:
                pass
            else:
                os.close(existing)
            temporary = ".state-" + uuid.uuid4().hex
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE, 0o600, dir_fd=parent)
            try:
                validate_fd(fd, directory=False)
                pending = memoryview(data)
                while pending:
                    written = os.write(fd, pending)
                    if written <= 0:
                        raise StateError("E_IO")
                    pending = pending[written:]
                os.fsync(fd)
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                os.close(fd)
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass

    @contextmanager
    def lock(self, relative, blocking=False):
        with self._file(relative, create=True, writable=True) as fd:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise StateError("E_BUSY") from None
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)

    def ensure_ignore(self):
        with self.directory() as parent, _errors():
            try:
                fd = os.open(".gitignore", os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE, 0o600, dir_fd=parent)
            except FileExistsError:
                if self.read_bytes(".gitignore", max_bytes=2) not in {b"*", b"*\n"}:
                    raise StateError("E_STATE_CORRUPT")
            else:
                try:
                    validate_fd(fd, directory=False)
                    if os.write(fd, b"*\n") != 2:
                        raise StateError("E_IO")
                    os.fsync(fd)
                    os.fsync(parent)
                finally:
                    os.close(fd)


def inspect_path(root) -> Path:
    """Validate existing components and probe writes without creating state."""
    root = _absolute(root)
    with _root_directory(root, create=False, nearest=True) as (parent, existing), _errors():
        name = ".state-probe-" + uuid.uuid4().hex
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _FILE, 0o600, dir_fd=parent)
        try:
            validate_fd(fd, directory=False)
            if os.write(fd, b"probe") != 5:
                raise StateError("E_IO")
        finally:
            os.close(fd)
            os.unlink(name, dir_fd=parent)
        return existing
