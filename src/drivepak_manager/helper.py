"""Privileged Linux endpoint. No GUI, arbitrary file opens, shell, or install actions.

Only the installed root-owned zipapp is invoked by pkexec. The caller sends
regular-file descriptors over a peer-credential-checked local Unix socket.
"""
import array
import errno
import fcntl
import json
import os
from pathlib import Path
import pwd
import socket
import stat
import struct
import sys

from .backup import snapshot_stream, restore_stream, direct_write_stream
from .devices import validate
from .errors import SafetyError

BLKGETSIZE64 = 0x80081272
BLKFLSBUF = 0x1261


def open_card(device, access):
    if not device.usable:
        raise SafetyError(f"{device.path}: {device.status}")
    try:
        return os.open(device.path, access | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as e:
        if e.errno == errno.ENOMEDIUM:
            raise SafetyError(f"{device.path}: the reader reports no readable card. Reseat the card or try another reader, then Refresh and select it again. No card I/O started.") from e
        if e.errno in (errno.ENODEV, errno.ENXIO):
            raise SafetyError(f"{device.path}: the card reader or its media is no longer available. Reconnect it, then Refresh and select it again. No card I/O started.") from e
        raise


def send(sock, value):
    sock.send(json.dumps(value, separators=(",", ":")).encode())


class SocketCancellation:
    def __init__(self, sock):
        self.sock, self.cancelled = sock, False

    def is_set(self):
        if self.cancelled:
            return True
        try:
            data = self.sock.recv(128, socket.MSG_DONTWAIT)
            if not data or data == b"cancel":
                self.cancelled = True
        except BlockingIOError:
            pass
        return self.cancelled


def check_fd(fd, uid, writable=False):
    s = os.fstat(fd)
    mode = fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE
    if not stat.S_ISREG(s.st_mode) or (writable and s.st_uid != uid):
        raise SafetyError("Helper accepts only regular-file descriptors owned by the caller for output.")
    if mode != (os.O_RDWR if writable else os.O_RDONLY):
        raise SafetyError("Unexpected descriptor access mode.")
    fcntl.flock(fd, (fcntl.LOCK_EX if writable else fcntl.LOCK_SH) | fcntl.LOCK_NB)
    return s


def serve(sock, uid):
    msg, ancdata, flags, _ = sock.recvmsg(65536, socket.CMSG_SPACE(4 * 8))
    descriptors = array.array("i")
    for level, kind, value in ancdata:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            descriptors.frombytes(value[:len(value) - len(value) % descriptors.itemsize])
    opened = []
    try:
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC) or len(msg) > 32768:
            raise SafetyError("Oversized helper request.")
        request = json.loads(msg)
        if set(request) - {"operation", "expected", "manifest", "acknowledge_no_backup"}:
            raise SafetyError("Unknown helper request field.")
        op = request["operation"]
        if op not in ("backup", "write", "write-direct") or len(descriptors) != (1 if op == "write-direct" else 2):
            raise SafetyError("Unsupported helper operation or descriptor count.")
        direct = op == "write-direct"
        if direct and request.get("acknowledge_no_backup") is not True:
            raise SafetyError("Direct writing requires explicit acknowledgement that no backup is being created.")
        one = descriptors[0]
        s1 = check_fd(one, uid, writable=op == "backup")
        # Backup readback is the same file, so do not take a second flock which
        # would conflict with its independent exclusive output descriptor.
        if not direct:
            two = descriptors[1]
            s2 = os.fstat(two)
            if not stat.S_ISREG(s2.st_mode) or fcntl.fcntl(two, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                raise SafetyError("Readback/backup descriptor must be a read-only regular file.")
            if op == "backup":
                if (s1.st_dev, s1.st_ino) != (s2.st_dev, s2.st_ino) or s1.st_size:
                    raise SafetyError("Backup requires an empty new output and an independently reopened readback descriptor.")
            else:
                check_fd(two, uid)
                if (s1.st_dev, s1.st_ino) == (s2.st_dev, s2.st_ino):
                    raise SafetyError("Source image and recovery backup must be separate files.")
        expected = request["expected"]
        device = validate(expected, storage_paths=[pwd.getpwuid(uid).pw_dir], storage_fds=descriptors)
        if not device.path.startswith("/dev/") or str(Path(device.path).resolve()) != device.path:
            raise SafetyError("Expected a canonical device path directly under /dev.")
        access = os.O_RDONLY if op == "backup" else os.O_RDWR
        fd = open_card(device, access)
        opened.append(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        s = os.fstat(fd)
        if not stat.S_ISBLK(s.st_mode) or f"{os.major(s.st_rdev)}:{os.minor(s.st_rdev)}" != device.major_minor:
            raise SafetyError("Device descriptor identity does not match the selected card.")

        def guard():
            current = os.stat(device.path, follow_symlinks=False)
            capacity = struct.unpack("Q", fcntl.ioctl(fd, BLKGETSIZE64, bytes(8)))[0]
            seq = Path(f"/sys/dev/block/{device.major_minor}/diskseq").read_text().strip()
            if current.st_rdev != s.st_rdev or not stat.S_ISBLK(current.st_mode) or capacity != device.capacity or seq != device.diskseq:
                raise SafetyError("Device was removed, replaced or resized during the operation.")

        guard()
        # Recheck topology and all mounts after elevation and exclusive open.
        validate(expected, storage_paths=[pwd.getpwuid(uid).pw_dir], storage_fds=descriptors)
        cancel = SocketCancellation(sock)

        def progress(done, total, phase):
            send(sock, {"event": "progress", "done": done, "total": total, "phase": phase})

        with os.fdopen(os.dup(fd), "rb" if op == "backup" else "r+b", buffering=0) as target:
            # Flush/invalidate block cache before the independent device pass.
            target.invalidate = lambda: fcntl.ioctl(fd, BLKFLSBUF)
            if direct:
                with os.fdopen(os.dup(one), "rb", buffering=0) as source:
                    result = direct_write_stream(target, device.capacity, source, progress, cancel, guard,
                                                 acknowledge_no_backup=True)
                send(sock, {"event": "result", "result": result})
                return
            with os.fdopen(os.dup(one), "r+b" if op == "backup" else "rb", buffering=0) as first, os.fdopen(os.dup(two), "rb", buffering=0) as second:
                if op == "backup":
                    free = os.fstatvfs(first.fileno()).f_bavail * os.fstatvfs(first.fileno()).f_frsize
                    if free < device.capacity + 16 * 1024**2:
                        raise SafetyError("Not enough free space on backup storage.")
                    result = snapshot_stream(target, device.capacity, device.identity, first, second, progress, cancel, guard)
                else:
                    result = restore_stream(target, device.capacity, device.identity, first, second, request.get("manifest"), progress, cancel, guard)
        send(sock, {"event": "result", "result": result})
    finally:
        for fd in [*opened, *descriptors]:
            os.close(fd)


def main():
    sock = None
    try:
        if sys.platform != "linux" or os.geteuid() != 0 or len(sys.argv) != 2:
            raise SafetyError("This endpoint must be invoked through the installed pkexec helper.")
        uid = int(os.environ.get("PKEXEC_UID", "0"))
        if uid <= 0:
            raise SafetyError("An unprivileged calling user is required.")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sock.connect(sys.argv[1])
        _, peer_uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        if peer_uid != uid:
            raise SafetyError("Local socket peer does not match authenticated caller.")
        serve(sock, uid)
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        if sock:
            try:
                send(sock, {"event": "error", "message": message})
            except OSError:
                pass
        else:
            print(message, file=sys.stderr)
        return 1
    finally:
        if sock:
            sock.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
