import os
import errno
from pathlib import Path
import socket

import pytest

from drivepak_manager.devices import Topology, Device, discover, validate
from drivepak_manager.errors import SafetyError
from drivepak_manager.helper import check_fd
from drivepak_manager.linux import invoke


def row(name, mm, kind="disk", mounts=None, children=None, tran="usb"):
    return {"name": "/dev/" + name, "path": "/dev/" + name, "maj:min": mm, "type": kind,
            "size": 64 * 1024**2, "model": "Test reader", "serial": "SERIAL", "wwn": "",
            "tran": tran, "ro": False, "rm": False, "mountpoints": mounts or [], "children": children or []}


def fake_topology(tmp_path):
    rows = [row("system", "8:0", mounts=["/"], tran="sata"),
            row("usb", "8:16", children=[row("usb1", "8:17", "part")]),
            row("internal", "8:32", tran="sata")]
    for mm in ("8:0", "8:16", "8:32"):
        path = tmp_path / "dev/block" / mm
        path.mkdir(parents=True)
        (path / "diskseq").write_text("123")
    return Topology(rows, tmp_path)


def test_usb_removable_false_is_supported_system_internal_rejected(tmp_path):
    t = fake_topology(tmp_path)
    devices = {d.path: d for d in discover(t, protected_paths=[])}
    assert devices["/dev/usb"].usable  # removable flag is deliberately False
    assert devices["/dev/system"].reasons and devices["/dev/internal"].reasons
    assert "8:17" in t.family("8:16")
    t.connect("8:17", "253:0")
    t.rows["253:0"] = row("dm-0", "253:0", "crypt", mounts=["/home"])
    assert "8:16" in t.protected_keys()


def test_backing_conflicts_and_replacement(tmp_path, monkeypatch):
    import drivepak_manager.devices as devices
    topo = fake_topology(tmp_path)
    device = next(d for d in discover(topo, protected_paths=[]) if d.path == "/dev/usb")
    monkeypatch.setattr(devices, "topology", lambda: topo)
    monkeypatch.setattr(devices, "discover", lambda t: [device])
    monkeypatch.setattr(devices, "path_devices", lambda p, t: t.family("8:17"))
    with pytest.raises(SafetyError, match="backed by the target"):
        validate(device.identity, [tmp_path])
    expected = dict(device.identity, diskseq="124")
    with pytest.raises(SafetyError, match="replaced"):
        validate(expected)


def test_mounts_and_missing_diskseq_disable_access(tmp_path):
    t = fake_topology(tmp_path)
    t.rows["8:17"]["mountpoints"] = ["/media/card"]
    d = next(d for d in discover(t, protected_paths=[]) if d.path == "/dev/usb")
    assert not d.usable and d.mounts == ("/media/card",)
    (tmp_path / "dev/block/8:16/diskseq").unlink()
    d = next(d for d in discover(t, protected_paths=[]) if d.path == "/dev/usb")
    assert any("sequence" in r for r in d.reasons)


@pytest.mark.parametrize("capacity", [0, None])
def test_empty_reader_slot_is_visible_but_not_available(tmp_path, capacity):
    topo = fake_topology(tmp_path)
    topo.rows["8:16"]["size"] = capacity
    d = next(d for d in discover(topo, protected_paths=[]) if d.path == "/dev/usb")
    assert d.capacity == 0 and not d.usable
    assert "0 bytes" in d.status and "No readable card" in d.status
    assert "Available" not in d.status
    assert d.reasons and d.path == "/dev/usb"


def test_capacity_alone_blocks_access_even_without_discovery_reasons(tmp_path):
    from dataclasses import replace
    d = next(d for d in discover(fake_topology(tmp_path), protected_paths=[]) if d.path == "/dev/usb")
    for capacity in (0, 1, 513):
        invalid = replace(d, capacity=capacity, reasons=())
        assert not invalid.usable and invalid.problems
        assert "Available" not in invalid.status


def test_card_removed_after_selection_is_reported_before_storage_checks(tmp_path, monkeypatch):
    from dataclasses import replace
    import drivepak_manager.devices as devices
    import drivepak_manager.linux as linux
    topo = fake_topology(tmp_path)
    original = next(d for d in discover(topo, protected_paths=[]) if d.path == "/dev/usb")
    empty = replace(original, capacity=0)
    monkeypatch.setattr(devices, "topology", lambda: topo)
    monkeypatch.setattr(devices, "discover", lambda t: [empty])
    monkeypatch.setattr(devices, "path_devices", lambda *a: pytest.fail("Storage checks must not run without media"))
    monkeypatch.setattr(linux, "invoke", lambda *a, **kw: pytest.fail("Helper must not run without media"))
    with pytest.raises(SafetyError, match="No readable card"):
        validate(original.identity)
    output = tmp_path / "backup.img"
    with pytest.raises(SafetyError, match="No readable card"):
        linux.backup_card(empty, output)
    assert not output.exists() and not (tmp_path / "backup.img.json").exists()
    assert not list(tmp_path.glob("*.partial"))


@pytest.mark.parametrize("error_number", [errno.ENOMEDIUM, errno.ENODEV, errno.ENXIO])
def test_helper_explains_missing_media_during_open(tmp_path, monkeypatch, error_number):
    from drivepak_manager.helper import open_card
    d = next(d for d in discover(fake_topology(tmp_path), protected_paths=[]) if d.path == "/dev/usb")
    def unavailable(path, flags):
        assert path == d.path and flags & os.O_EXCL and flags & os.O_NOFOLLOW
        raise OSError(error_number, os.strerror(error_number), path)
    monkeypatch.setattr(os, "open", unavailable)
    with pytest.raises(SafetyError, match="Refresh.*No card I/O started"):
        open_card(d, os.O_RDONLY)


def test_helper_never_opens_a_zero_capacity_reader(tmp_path, monkeypatch):
    from dataclasses import replace
    from drivepak_manager.helper import open_card
    d = next(d for d in discover(fake_topology(tmp_path), protected_paths=[]) if d.path == "/dev/usb")
    monkeypatch.setattr(os, "open", lambda *a, **kw: pytest.fail("An empty reader must not be opened"))
    with pytest.raises(SafetyError, match="No readable card"):
        open_card(replace(d, capacity=0), os.O_RDONLY)


def test_helper_missing_privileges_and_descriptor_restrictions(tmp_path, monkeypatch):
    import drivepak_manager.linux as linux
    monkeypatch.setattr(linux, "helper_installed", lambda: False)
    with pytest.raises(SafetyError, match="not installed"):
        invoke("write", {}, [])
    p = tmp_path / "regular"
    p.write_bytes(b"x")
    with p.open("rb") as f:
        check_fd(f.fileno(), os.getuid())
        with pytest.raises(SafetyError, match="access mode"):
            check_fd(f.fileno(), os.getuid(), writable=True)
    with open("/dev/null", "rb") as f:
        with pytest.raises(SafetyError, match="regular"):
            check_fd(f.fileno(), os.getuid())


def test_authentication_denial_is_actionable(tmp_path, monkeypatch):
    import io
    import drivepak_manager.linux as linux
    class Denied:
        returncode = 126
        stderr = io.BytesIO(b"Authentication was dismissed")
        def poll(self):
            return 126
        def wait(self, timeout):
            return 126
    monkeypatch.setattr(linux, "helper_installed", lambda: True)
    monkeypatch.setattr(linux.subprocess, "Popen", lambda *a, **kw: Denied())
    with pytest.raises(SafetyError, match="authentication failed"):
        invoke("write", {}, [])


def test_raw_device_use_inspection(tmp_path, monkeypatch):
    import stat
    from types import SimpleNamespace
    from drivepak_manager.devices import open_device_users
    proc = tmp_path / "999999"
    (proc / "fd").mkdir(parents=True)
    fd = proc / "fd/3"
    fd.symlink_to("/dev/synthetic")
    (proc / "comm").write_text("other-writer")
    original = os.stat
    monkeypatch.setattr(os, "stat", lambda p, *a, **kw: SimpleNamespace(st_mode=stat.S_IFBLK, st_rdev=os.makedev(8, 16)) if str(p) == str(fd) else original(p, *a, **kw))
    assert open_device_users({"8:16"}, procroot=tmp_path) == ["other-writer (PID 999999)"]


@pytest.mark.parametrize("response", ['{}', '{"filesystems": []}'])
def test_unknown_empty_backing_mapping_fails_closed(tmp_path, monkeypatch, response):
    import drivepak_manager.devices as devices
    monkeypatch.setattr(devices, "command", lambda args: response)
    with pytest.raises(SafetyError, match="backing"):
        devices.path_devices(tmp_path, Topology([], tmp_path / "sys"))


def test_deleted_source_or_journal_descriptor_rejected(tmp_path):
    journal = tmp_path / "removed.dpp"
    journal.write_bytes(b"saved recovery data")
    with journal.open("rb") as source:
        journal.unlink()
        with pytest.raises(SafetyError, match="deleted"):
            check_fd(source.fileno(), os.getuid())
