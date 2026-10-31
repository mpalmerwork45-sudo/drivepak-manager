"""Bounded, exact I/O and durable outputs, shared by file and device workflows."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import errno
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile

from .errors import Cancelled, SafetyError, DrivePakError

CHUNK = 4 * 1024 * 1024


def checkpoint(cancel=None):
    if cancel is not None and cancel.is_set():
        raise Cancelled("Cancelled. Incomplete output was not labelled verified.")


def report(progress, done, total, phase):
    if progress:
        progress(done, total, phase)


def read_exact(f, length):
    data = bytearray()
    while len(data) < length:
        b = f.read(length - len(data))
        if not b:
            raise DrivePakError(f"Short read: needed {length} bytes, received {len(data)}. No zero filling was performed.")
        data.extend(b)
    return bytes(data)


def write_all(f, data):
    view = memoryview(data)
    while view:
        count = f.write(view)
        if count is None or count <= 0 or count > len(view):
            raise DrivePakError("Short write: the destination stopped accepting bytes.")
        view = view[count:]


def fingerprint(f):
    s = os.fstat(f.fileno())
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


def hash_stream(f, length, progress=None, cancel=None, phase="Verifying", guard=None):
    f.seek(0)
    h = hashlib.sha256()
    for offset in range(0, length, CHUNK):
        checkpoint(cancel)
        if guard:
            guard()
        h.update(read_exact(f, min(CHUNK, length - offset)))
        report(progress, min(offset + CHUNK, length), length, phase)
    return h.hexdigest()


def hash_file(path, progress=None, cancel=None):
    with Path(path).open("rb", buffering=0) as f:
        before = fingerprint(f)
        result = hash_stream(f, before[2], progress, cancel)
        if before != fingerprint(f):
            raise SafetyError("The source file changed while reading; retry with a stable source.")
        return result


def sync_dir(path):
    if os.name == "posix":
        fd = os.open(str(path), os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def need_space(path, count):
    path = Path(path).resolve()
    while not path.exists() and path.parent != path:
        path = path.parent
    if shutil.disk_usage(path).free < count + 16 * 1024**2:
        raise SafetyError(f"Insufficient free space: need {count:,} bytes plus 16 MiB reserve.")


def distinct(*paths):
    resolved = [Path(p).resolve() for p in paths]
    if len(set(resolved)) != len(resolved):
        raise SafetyError("Source, backup, working image and output must use different paths.")
    existing = [p for p in resolved if p.exists()]
    keys = [(p.stat().st_dev, p.stat().st_ino) for p in existing]
    if len(set(keys)) != len(keys):
        raise SafetyError("Two paths are hard links to the same file.")


def publish_new(temporary, destination):
    """Atomic no-replace publication, including Linux filesystems without links."""
    if sys.platform == "linux":
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        renameat2 = getattr(libc, "renameat2", None)
        if renameat2:
            renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            renameat2.restype = ctypes.c_int
            if renameat2(-100, os.fsencode(temporary), -100, os.fsencode(destination), 1) == 0:
                return
            error = ctypes.get_errno()
            if error not in (errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP):
                raise OSError(error, os.strerror(error), str(destination))
    if os.name == "nt":
        # Windows rename fails instead of replacing an existing destination.
        os.rename(temporary, destination)
    else:
        try:
            os.link(temporary, destination)
        except OSError as e:
            if e.errno in (errno.EPERM, errno.EOPNOTSUPP):
                raise SafetyError("Destination filesystem cannot publish a new file without replacement atomically. Choose another filesystem; the partial output was retained.") from e
            raise
        os.unlink(temporary)


@contextmanager
def atomic_output(path, overwrite=False):
    """Publish a durable output only after success. Never replace by default."""
    path = Path(path).absolute()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise SafetyError(f"Output already exists: {path}. Choose a new name.")
    fd, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".partial", dir=path.parent)
    try:
        with os.fdopen(fd, "w+b", buffering=0) as f:
            f.partial_path = temp
            yield f
            f.flush()
            os.fsync(f.fileno())
        if overwrite:
            os.replace(temp, path)
        else:
            publish_new(temp, path)
        sync_dir(path.parent)
    except BaseException:
        # Keep partial output (unique filename) for diagnosis/recovery.
        raise


def copy_verified(source, destination, progress=None, cancel=None):
    source, destination = Path(source), Path(destination)
    distinct(source, destination)
    if not stat.S_ISREG(source.stat().st_mode):
        raise SafetyError("Image operations accept regular files. Use the Linux card backend for devices.")
    need_space(destination.parent, source.stat().st_size)
    with source.open("rb", buffering=0) as src, atomic_output(destination) as dest:
        initial = fingerprint(src)
        h = copy_stream(src, dest, initial[2], progress, cancel)
        dest.flush()
        os.fsync(dest.fileno())
        # Reopen separately: verification must exercise the saved destination.
        with open(dest.partial_path, "rb", buffering=0) as check:
            if os.fstat(check.fileno()).st_size != initial[2] or hash_stream(check, initial[2], progress, cancel, "Verifying saved image") != h:
                raise DrivePakError("Saved image verification failed.")
        if fingerprint(src) != initial:
            raise SafetyError("Source changed during the copy.")
    return h


def copy_stream(source, destination, length, progress=None, cancel=None, guard=None):
    source.seek(0)
    destination.seek(0)
    h = hashlib.sha256()
    for offset in range(0, length, CHUNK):
        checkpoint(cancel)
        if guard:
            guard()
        b = read_exact(source, min(CHUNK, length - offset))
        write_all(destination, b)
        h.update(b)
        report(progress, min(offset + len(b), length), length, "Copying")
    return h.hexdigest()


def write_json(path, value):
    with atomic_output(path) as f:
        write_all(f, (json.dumps(value, indent=2) + "\n").encode())


def utcnow():
    return datetime.now(timezone.utc).isoformat()
