"""Unprivileged client of the installed, authenticated Linux device helper."""
import array
import json
import os
from pathlib import Path
import select
import socket
import stat
import struct
import subprocess
import tempfile
import time

from .backup import verify_backup
from .devices import validate
from .errors import Cancelled, SafetyError, DrivePakError
from .streams import atomic_output, checkpoint, distinct, need_space, report, write_json

HELPER = Path("/usr/local/libexec/drivepak-manager-helper")


def helper_installed():
    for file in (HELPER, HELPER.with_name("drivepak-manager-helper.pyz")):
        for p in [file, *file.parents]:
            try:
                s = p.lstat()
            except FileNotFoundError:
                return False
            if s.st_uid != 0 or s.st_mode & 0o022 or stat.S_ISLNK(s.st_mode):
                return False
    return True


def invoke(operation, expected, fds, manifest=None, progress=None, cancel=None):
    if not helper_installed():
        raise SafetyError("Linux helper is not installed/trusted. Build with tools/build_linux.sh, then install the generated helper using the documented administrator command. The GUI must remain unprivileged.")
    with tempfile.TemporaryDirectory(prefix="dpm-") as directory:
        endpoint = str(Path(directory) / "control")
        with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as listener:
            try:
                listener.bind(endpoint)
            except OSError as e:
                raise SafetyError(f"Cannot create the private card-helper socket: {e}. Launch on a desktop permitting local Unix sockets.") from e
            listener.listen(1)
            proc = subprocess.Popen(["/usr/bin/pkexec", "--disable-internal-agent", str(HELPER), endpoint],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
            connected = False
            try:
                started = time.monotonic()
                while not select.select([listener], [], [], 0.1)[0]:
                    checkpoint(cancel)
                    if proc.poll() is not None:
                        details = proc.stderr.read(8192).decode(errors="replace").strip()
                        raise SafetyError(f"Card access authentication failed (exit {proc.returncode}). {details} Ensure a polkit agent is running and the helper is installed.")
                    if time.monotonic() - started > 300:
                        raise SafetyError("Timed out waiting for card access authentication.")
                connection, _ = listener.accept()
                connected = True
                with connection:
                    _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != 0:
                        raise SafetyError("The connected card helper is not privileged.")
                    request = {"operation": operation, "expected": expected}
                    if manifest is not None:
                        request["manifest"] = manifest
                    connection.sendmsg([json.dumps(request).encode()], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))])
                    sent_cancel = False
                    while True:
                        if cancel and cancel.is_set() and not sent_cancel:
                            connection.send(b"cancel")
                            sent_cancel = True
                        if not select.select([connection], [], [], 0.1)[0]:
                            if proc.poll() is not None:
                                raise DrivePakError("Card helper exited without a verification result. If writing had begun, treat the card as incomplete and recover from its backup.")
                            continue
                        raw = connection.recv(65536)
                        if not raw:
                            raise DrivePakError("Card helper disconnected. The operation is unverified; preserve the backup and inspect the card before reuse.")
                        event = json.loads(raw)
                        if event["event"] == "progress":
                            report(progress, event["done"], event["total"], event["phase"])
                        elif event["event"] == "error":
                            raise DrivePakError(event["message"])
                        elif event["event"] == "result":
                            return event["result"]
            finally:
                # Closing the connection is cancellation, observed between chunks.
                # Do not forcibly terminate a running physical write.
                if not connected and proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                if proc.stderr:
                    proc.stderr.close()


def backup_card(device, destination, working_storage=None, progress=None, cancel=None):
    destination = Path(destination).absolute()
    destination.parent.mkdir(parents=True, exist_ok=True)
    validate(device.identity, [destination.parent, *([working_storage] if working_storage else [])])
    if Path(str(destination) + ".json").exists():
        raise SafetyError("A backup manifest already exists at that name. Choose a new backup filename.")
    need_space(destination.parent, device.capacity)
    with atomic_output(destination) as out:
        with open(out.partial_path, "rb", buffering=0) as readback:
            m = invoke("backup", device.identity, [out.fileno(), readback.fileno()], progress=progress, cancel=cancel)
    write_json(str(destination) + ".json", m)
    return m


def write_card(device, source, recovery_backup, progress=None, cancel=None):
    distinct(source, recovery_backup)
    validate(device.identity, [source, recovery_backup])
    manifest = verify_backup(recovery_backup, progress, cancel)
    with open(source, "rb", buffering=0) as src, open(recovery_backup, "rb", buffering=0) as backup:
        return invoke("write", device.identity, [src.fileno(), backup.fileno()], manifest, progress, cancel)
