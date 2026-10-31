import os
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
