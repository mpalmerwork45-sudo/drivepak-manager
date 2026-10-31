"""Exercise the actual helper request/FD protocol using a fake block adapter.

No privilege is acquired and no /dev node is opened. Only the strict target
open/stat/ioctl boundary is substituted with a regular-file simulation.
"""
import array
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

    def request(operation, files, manifest=None, acknowledge_no_backup=False):
        client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        client.settimeout(10)
        target_fds.clear()
        payload = {"operation": operation, "expected": d.identity}
        if manifest:
            payload["manifest"] = manifest
        if acknowledge_no_backup:
            payload["acknowledge_no_backup"] = True

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


@pytest.mark.parametrize("acknowledgement", [None, False, 1, "true"])
def test_direct_helper_requires_boolean_acknowledgement_before_device_access(tmp_path, monkeypatch, acknowledgement):
    source = tmp_path / "source.img"
    source.write_bytes(bytes(512))
    monkeypatch.setattr(helper, "validate", lambda *a, **kw: pytest.fail("Unacknowledged request reached device validation"))
    client, server = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    with client, server, source.open("rb") as src:
        payload = {"operation": "write-direct", "expected": {}, "acknowledge_no_backup": acknowledgement}
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
