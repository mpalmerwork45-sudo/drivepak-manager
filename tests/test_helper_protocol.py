"""Exercise the actual helper request/FD protocol using a fake block adapter.

No privilege is acquired and no /dev node is opened. Only the strict target
open/stat/ioctl boundary is substituted with a regular-file simulation.
"""
import array
import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import socket
import stat
import struct
from types import SimpleNamespace

import pytest

from drivepak_manager import helper
from drivepak_manager.devices import Device
from drivepak_manager.streams import write_json
from drivepak_manager.backup import verify_backup
from drivepak_manager.errors import SafetyError


def test_helper_snapshot_and_write_with_real_descriptor_transport(tmp_path, monkeypatch):
    target = tmp_path / "simulated-card.bin"
    before = bytes(range(256)) * 2048
    target.write_bytes(before)
    d = Device("/dev/drivepak-simulation", len(before), "Simulated reader", "SIM-1", "", "usb", "240:1", "/sys/simulated", "7", False, True)
    real_open, real_stat, real_fstat = os.open, os.stat, os.fstat
    real_read_text = Path.read_text
    target_fds = set()
    validated = []

    def validate(expected, storage_paths=(), storage_fds=()):
        assert expected == d.identity and len(storage_fds) in (1, 2)
        validated.append(True)
        return d

    def fake_open(path, flags, *args, **kwargs):
        if str(path) == d.path:
            assert flags & os.O_EXCL and flags & os.O_NOFOLLOW
            fd = real_open(target, flags & ~os.O_EXCL, *args, **kwargs)
            target_fds.add(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    def fake_fstat(fd):
        s = real_fstat(fd)
        if fd in target_fds:
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(240, 1))
        return s

    def fake_stat(path, *args, **kwargs):
        if str(path) == d.path:
            return SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(240, 1))
        return real_stat(path, *args, **kwargs)

    def fake_ioctl(fd, request, *args):
        assert fd in target_fds
        if request == helper.BLKGETSIZE64:
            return struct.pack("Q", len(before))
        assert request == helper.BLKFLSBUF
        return 0

    monkeypatch.setattr(helper, "validate", validate)
    monkeypatch.setattr(os, "open", fake_open)
    monkeypatch.setattr(os, "fstat", fake_fstat)
    monkeypatch.setattr(os, "stat", fake_stat)
    monkeypatch.setattr(helper.fcntl, "ioctl", fake_ioctl)
    monkeypatch.setattr(Path, "read_text", lambda p, *a, **kw: "7" if str(p) == "/sys/dev/block/240:1/diskseq" else real_read_text(p, *a, **kw))

    def request(operation, files, manifest=None, acknowledge_no_backup=False, acknowledge_in_place=False):
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        client.settimeout(10)
        target_fds.clear()
        payload = {"operation": operation, "expected": d.identity}
        if manifest:
            payload["manifest"] = manifest
        if acknowledge_no_backup:
            payload["acknowledge_no_backup"] = True
        if acknowledge_in_place:
            payload["acknowledge_in_place"] = True
            payload["journal_sha256"] = hashlib.sha256(Path(files[0].name).read_bytes()).hexdigest()

        def serve():
            with server:
                try:
                    helper.serve(server, os.getuid())
                except Exception as e:
                    helper.send(server, {"event": "error", "message": repr(e)})
                    raise

        with ThreadPoolExecutor(max_workers=1) as pool, client:
            future = pool.submit(serve)
            client.sendmsg([json.dumps(payload).encode()], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [f.fileno() for f in files]))])
            while True:
                event = json.loads(client.recv(65536))
                assert event["event"] != "error", event
                if event["event"] == "result":
                    future.result(timeout=5)
                    return event["result"]
                if event["event"] == "recovery-ready":
                    saved = tmp_path / "edits.dpp.json"
                    write_json(saved, event["manifest"])
                    with saved.open("rb", buffering=0) as receipt:
                        client.sendmsg([b"recovery-saved"], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [receipt.fileno()]))])

    backup = tmp_path / "backup.img"
    with backup.open("w+b", buffering=0) as out, backup.open("rb", buffering=0) as readback:
        manifest = request("backup", [out, readback])
    # Fd numbers can be reused after close; clear the simulation adapter's set
    # before ordinary verification opens another file under that number.
    target_fds.clear()
    write_json(str(backup) + ".json", manifest)
    assert backup.read_bytes() == before and verify_backup(backup)["status"] == "verified"
    source = tmp_path / "source.img"
    source.write_bytes(b"\x39" * 1024)
    with source.open("rb", buffering=0) as src, backup.open("rb", buffering=0) as back:
        result = request("write", [src, back], manifest)
    target_fds.clear()
    assert result["status"] == "verified" and len(validated) == 4
    assert target.read_bytes() == source.read_bytes() + before[1024:]
    assert backup.read_bytes() == before
    source.write_bytes(b"\x52" * 1536)
    previous = target.read_bytes()
    with source.open("rb", buffering=0) as src:
        result = request("write-direct", [src], acknowledge_no_backup=True)
    target_fds.clear()
    assert result["status"] == "verified" and result["backup_policy"] == "none-explicit-direct-write"
    assert len(validated) == 6
    assert target.read_bytes() == source.read_bytes() + previous[1536:]
    assert backup.read_bytes() == before
    from drivepak_manager.patches import prepare_changes
    original, modified, journal = [tmp_path / n for n in ("original.img", "modified.img", "edits.dpp")]
    original.write_bytes(target.read_bytes())
    b = bytearray(original.read_bytes())
    b[513], b[2050] = 0x66, 0x77
    modified.write_bytes(b)
    prepare_changes(original, modified, journal)
    with journal.open("rb", buffering=0) as src:
        result = request("write-patch", [src], acknowledge_in_place=True)
    target_fds.clear()
    assert result["status"] == "verified" and result["bytes_written"] == 1024
    assert target.read_bytes() == modified.read_bytes()
    assert json.loads((tmp_path / "edits.dpp.json").read_text())["kind"] == "changed-sector-recovery"


@pytest.mark.parametrize("operation", ["write-direct", "write-patch"])
@pytest.mark.parametrize("acknowledgement", [None, False, 1, "true"])
def test_write_helper_requires_boolean_acknowledgement_before_device_access(tmp_path, monkeypatch, acknowledgement, operation):
    source = tmp_path / "source.img"
    source.write_bytes(bytes(512))
    monkeypatch.setattr(helper, "validate", lambda *a, **kw: pytest.fail("Unacknowledged request reached device validation"))
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with client, server, source.open("rb") as src:
        key = "acknowledge_no_backup" if operation == "write-direct" else "acknowledge_in_place"
        payload = {"operation": operation, "expected": {}, key: acknowledgement}
        client.sendmsg([json.dumps(payload).encode()], [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [src.fileno()]))])
        with pytest.raises(SafetyError, match="acknowledgement"):
            helper.serve(server, os.getuid())


def test_old_installed_helper_has_actionable_update_error_before_elevation(tmp_path, monkeypatch):
    import zipfile
    from drivepak_manager import linux
    entry = tmp_path / "drivepak-manager-helper"
    with zipfile.ZipFile(entry.with_name("drivepak-manager-helper.pyz"), "w") as archive:
        archive.writestr("__main__.py", "pass\n")
    monkeypatch.setattr(linux, "HELPER", entry)
    monkeypatch.setattr(linux, "helper_installed", lambda: True)
    monkeypatch.setattr(linux.subprocess, "Popen", lambda *a, **kw: pytest.fail("Outdated helper was elevated"))
    with pytest.raises(SafetyError, match="install-helper.sh.*No device was opened"):
        linux.invoke("write-direct", {}, [], acknowledge_no_backup=True)


def test_earlier_patch_helper_rejected_before_elevation(tmp_path, monkeypatch):
    import zipfile
    from drivepak_manager import linux
    entry = tmp_path / "drivepak-manager-helper"
    with zipfile.ZipFile(entry.with_name("drivepak-manager-helper.pyz"), "w") as archive:
        archive.writestr("helper-capabilities.json", json.dumps({"protocol": 1, "operations": ["write-patch"]}))
    monkeypatch.setattr(linux, "HELPER", entry)
    monkeypatch.setattr(linux, "helper_installed", lambda: True)
    monkeypatch.setattr(linux.subprocess, "Popen", lambda *a, **kw: pytest.fail("Unaudited helper was elevated"))
    with pytest.raises(SafetyError, match="install-helper.sh.*No device was opened"):
        linux.invoke("write-patch", {}, [], acknowledge_in_place=True, recovery_manifest_path=tmp_path / "receipt.json")


@pytest.mark.parametrize("fault", [None, "missing-fd", "cancel", "disconnect", "writable", "wrong-content", "unpublished", "storage-conflict", "new-device-use", "fsync-failure"])
def test_helper_checks_published_recovery_receipt_and_storage(tmp_path, monkeypatch, fault):
    receipt = tmp_path / "receipt.json"
    expected = {"kind": "changed-sector-recovery", "journal_sha256": "expected", "status": "verified"}
    write_json(receipt, {} if fault == "wrong-content" else expected)
    validated = []
    def validate(identity, storage_paths=(), storage_fds=()):
        assert identity == {"selected": "simulated"} and len(storage_fds) == 1
        assert os.fstat(storage_fds[0]).st_ino == receipt.stat().st_ino
        validated.append(True)
        if fault in ("storage-conflict", "new-device-use"):
            raise SafetyError("Simulated unsafe storage or new device use")
    monkeypatch.setattr(helper, "validate", validate)
    if fault == "fsync-failure":
        def fail_sync(fd):
            raise OSError("Simulated receipt flush failure")
        monkeypatch.setattr(os, "fsync", fail_sync)
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with client, server, receipt.open("r+b" if fault == "writable" else "rb", buffering=0) as saved:
        if fault == "unpublished":
            receipt.unlink()
        if fault == "disconnect":
            client.shutdown(socket.SHUT_WR)
        else:
            fds = [] if fault in ("missing-fd", "cancel") else [saved.fileno()]
            ancillary = [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))] if fds else []
            client.sendmsg([b"cancel" if fault == "cancel" else b"recovery-saved"], ancillary)
        if fault:
            with pytest.raises((SafetyError, OSError)):
                helper.confirm_recovery_receipt(server, os.getuid(), expected, {"selected": "simulated"}, [])
        else:
            fd, state = helper.confirm_recovery_receipt(server, os.getuid(), expected, {"selected": "simulated"}, [])
            try:
                assert helper.fingerprint_from_fd(fd) == state
                assert validated == [True]
            finally:
                os.close(fd)


def test_linux_client_transfers_durable_receipt_descriptor(tmp_path, monkeypatch):
    """Actual client socket loop; fake pkexec never opens a block device."""
    import io
    import threading
    import zipfile
    from drivepak_manager import linux
    entry, journal, receipt = [tmp_path / n for n in ("helper", "edits.dpp", "edits.dpp.json")]
    journal.write_bytes(b"journal used only for transport testing")
    manifest = {"kind": "changed-sector-recovery", "status": "verified", "journal_sha256": hashlib.sha256(journal.read_bytes()).hexdigest()}
    with zipfile.ZipFile(entry.with_name("drivepak-manager-helper.pyz"), "w") as archive:
        archive.writestr("helper-capabilities.json", json.dumps({"protocol": 1, "operations": ["write-patch"], "card_edits_version": 2}))
    monkeypatch.setattr(linux, "HELPER", entry)
    monkeypatch.setattr(linux, "helper_installed", lambda: True)
    real_socket = socket.socket
    class SimulatedPeerSocket(real_socket):
        def getsockopt(self, level, option, *args):
            if level == socket.SOL_SOCKET and option == socket.SO_PEERCRED:
                return struct.pack("3i", os.getpid(), 0, os.getgid())
            return super().getsockopt(level, option, *args)
    monkeypatch.setattr(socket, "socket", SimulatedPeerSocket)
    checked = []
    def validate(expected, storage_paths=(), storage_fds=()):
        assert len(storage_fds) == 2
        assert os.fstat(storage_fds[1]).st_ino == receipt.stat().st_ino
        checked.append(True)
    monkeypatch.setattr(helper, "validate", validate)
    class LocalProcess:
        def __init__(self, args, **kwargs):
            assert args[0] == "/usr/bin/pkexec"  # intercepted, never executed
            self.stderr, self.returncode = io.BytesIO(), None
            self.thread = threading.Thread(target=self.run, args=(args[-1],))
            self.thread.start()
        def run(self, endpoint):
            fds = array.array("i")
            try:
                with real_socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as server:
                    server.connect(endpoint)
                    raw, ancillary, flags, _ = server.recvmsg(65536, socket.CMSG_SPACE(32))
                    for level, kind, data in ancillary:
                        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                            fds.frombytes(data[:len(data) - len(data) % fds.itemsize])
                    request = json.loads(raw)
                    assert request["journal_sha256"] == manifest["journal_sha256"]
                    fd, state = helper.confirm_recovery_receipt(server, os.getuid(), manifest, {}, fds)
                    fds.append(fd)
                    assert helper.fingerprint_from_fd(fd) == state
                    helper.send(server, {"event": "result", "result": {"status": "verified"}})
                self.returncode = 0
            except Exception as e:
                self.stderr.write(repr(e).encode())
                self.stderr.seek(0)
                self.returncode = 1
            finally:
                for fd in fds:
                    os.close(fd)
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            self.thread.join(timeout)
            assert not self.thread.is_alive()
            return self.returncode
    monkeypatch.setattr(linux.subprocess, "Popen", LocalProcess)
    with journal.open("rb", buffering=0) as source:
        result = linux.invoke("write-patch", {}, [source.fileno()], acknowledge_in_place=True,
                              recovery_manifest_path=receipt, expected_journal=manifest["journal_sha256"])
    assert result["status"] == "verified" and checked == [True]
    assert json.loads(receipt.read_text()) == manifest
