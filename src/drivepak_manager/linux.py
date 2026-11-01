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
import zipfile

from .backup import verify_backup
from . import __version__
from .devices import validate
from .errors import Cancelled, SafetyError, DrivePakError
from .streams import atomic_output, checkpoint, distinct, hash_file, need_space, report, sync_dir, write_json

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


def invoke(operation, expected, fds, manifest=None, progress=None, cancel=None, *, acknowledge_no_backup=False,
           acknowledge_in_place=False, recovery=False, recovery_manifest_path=None, expected_journal=None):
    if not helper_installed():
        raise SafetyError("Linux helper is not installed/trusted. Build with tools/build_linux.sh, then install the generated helper using the documented administrator command. The GUI must remain unprivileged.")
    if operation in ("write-direct", "write-patch"):
        if operation == "write-direct" and acknowledge_no_backup is not True:
            raise SafetyError("Direct writing requires explicit acknowledgement that no backup will be created.")
        if operation == "write-patch" and (acknowledge_in_place is not True or not recovery_manifest_path):
            raise SafetyError("Card edits require explicit acknowledgement and a new recovery manifest destination.")
        try:
            with zipfile.ZipFile(HELPER.with_name("drivepak-manager-helper.pyz")) as archive:
                info = archive.getinfo("helper-capabilities.json")
                capability = json.loads(archive.read(info)) if info.file_size <= 4096 else None
                if not isinstance(capability, dict) or capability.get("protocol") != 1 or operation not in capability.get("operations", []):
                    raise ValueError("direct operation unavailable")
                if operation == "write-patch" and capability.get("card_edits_version") != 2:
                    raise ValueError("audited receipt and recovery checks unavailable")
        except (OSError, KeyError, TypeError, ValueError, zipfile.BadZipFile) as e:
            raise SafetyError(f"The installed helper needs updating for this card operation. Run sudo ./install-helper.sh from drivepak-manager-{__version__}-linux, then retry. No device was opened.") from e
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
                    if operation == "write-direct":
                        request["acknowledge_no_backup"] = True
                    if operation == "write-patch":
                        request.update(acknowledge_in_place=True, recovery=recovery, journal_sha256=expected_journal)
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
                        elif event["event"] == "recovery-ready":
                            if operation != "write-patch" or not recovery_manifest_path:
                                raise SafetyError("Unexpected recovery checkpoint from helper.")
                            checkpoint(cancel)
                            write_json(recovery_manifest_path, event["manifest"])
                            receipt_fd = os.open(recovery_manifest_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
                            try:
                                connection.sendmsg([b"recovery-saved"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [receipt_fd]))])
                            finally:
                                os.close(receipt_fd)
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


def write_card_direct(device, source, progress=None, cancel=None, *, acknowledge_no_backup=False):
    if acknowledge_no_backup is not True:
        raise SafetyError("Direct writing requires explicit acknowledgement that no recovery backup will be created.")
    validate(device.identity, [source])
    with open(source, "rb", buffering=0) as src:
        return invoke("write-direct", device.identity, [src.fileno()], progress=progress, cancel=cancel,
                      acknowledge_no_backup=True)


def apply_card_changes(device, journal, receipt, progress=None, cancel=None, *, recovery=False, expected_journal=None):
    """Apply/recover a durable journal; the helper checks the target before writing."""
    journal, receipt = Path(journal), Path(receipt)
    distinct(journal, receipt)
    if not recovery and receipt.absolute() != Path(str(journal) + ".json").absolute():
        raise SafetyError("Save the initial recovery receipt beside its journal as JOURNAL.dpp.json; recovery discovers it there.")
    validate(device.identity, [journal, receipt.parent])
    if receipt.exists():
        raise SafetyError("Recovery receipt already exists; choose a new destination.")
    if expected_journal is None:
        expected_journal = hash_file(journal, progress, cancel)
    manifest = None
    if recovery:
        saved = Path(str(journal) + ".json")
        if not saved.is_file() or saved.stat().st_size > 65536:
            raise SafetyError("Missing or oversized edit recovery receipt.")
        manifest = json.loads(saved.read_text())
    with journal.open("rb", buffering=0) as source:
        # CLI journals may have been copied by another program. Persist both
        # file and directory before the privileged operation can rely on them.
        os.fsync(source.fileno())
        sync_dir(journal.resolve().parent)
        return invoke("write-patch", device.identity, [source.fileno()], manifest, progress, cancel,
                      acknowledge_in_place=True, recovery=recovery, recovery_manifest_path=receipt,
                      expected_journal=expected_journal)
