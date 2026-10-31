import errno
import io
import json
from pathlib import Path
import threading

import pytest

from drivepak_manager.backup import SimulatedDevice, snapshot_stream, restore_stream, verify_backup
from drivepak_manager.errors import Cancelled, DrivePakError, SafetyError
from drivepak_manager.streams import read_exact, write_all, copy_verified


@pytest.fixture
def device(tmp_path):
    p = tmp_path / "simulated-device.bin"
    p.write_bytes(bytes(range(256)) * 4096)
    return SimulatedDevice(p)


def test_complete_snapshot_restore_readback_and_larger_tail(device, tmp_path):
    before = device.path.read_bytes()
    backup = tmp_path / "backup.img"
    manifest = device.backup(backup)
    assert manifest["status"] == "verified"
    assert backup.read_bytes() == before and manifest["bytes"] == len(before)
    assert verify_backup(backup) == manifest
    image = tmp_path / "short-distribution.img"
    image.write_bytes(b"\x51" * 512 * 500)
    result = device.write(image, backup)
    assert result["status"] == "verified" and result["bytes_written"] == image.stat().st_size
    assert device.path.read_bytes() == image.read_bytes() + before[image.stat().st_size:]
    # Old snapshot is no longer current; a fresh complete backup is required.
    with pytest.raises(SafetyError, match="no longer matches"):
        device.write(image, backup)
    fresh = tmp_path / "before-recovery.img"
    device.backup(fresh)
    device.write(backup, fresh)
    assert device.path.read_bytes() == before


def test_snapshot_bad_provenance_and_modified_backup(device, tmp_path):
    backup = tmp_path / "backup.img"
    device.backup(backup)
    image = tmp_path / "image.img"
    image.write_bytes(bytes(512))
    manifest = json.loads(Path(str(backup) + ".json").read_text())
    manifest["identity"]["inode"] += 1
    Path(str(backup) + ".json").write_text(json.dumps(manifest))
    with pytest.raises(SafetyError, match="provenance"):
        device.write(image, backup)
    backup.write_bytes(b"x" * device.capacity)
    with pytest.raises(SafetyError, match="checksum"):
        verify_backup(backup)


def test_second_target_requires_own_current_snapshot(device, tmp_path):
    backup = tmp_path / "backup.img"
    device.backup(backup)
    second = tmp_path / "second-device"
    second.write_bytes(device.path.read_bytes())
    image = tmp_path / "image.img"
    image.write_bytes(bytes(512))
    with pytest.raises(SafetyError, match="provenance"):
        SimulatedDevice(second).write(image, backup)
    assert second.read_bytes() == backup.read_bytes()


def test_short_read_handled_exactly_and_eof_rejected():
    class ShortReader(io.BytesIO):
        def read(self, n=-1):
            return super().read(min(n, 3))
    assert read_exact(ShortReader(b"abcdef"), 6) == b"abcdef"
    with pytest.raises(DrivePakError, match="Short read"):
        read_exact(ShortReader(b"abc"), 6)


def test_short_write_loops_and_zero_write_fails():
    class ShortWriter(io.BytesIO):
        def write(self, b):
            return super().write(b[:2])
    w = ShortWriter()
    write_all(w, b"abcdef")
    assert w.getvalue() == b"abcdef"
    class BrokenWriter(io.BytesIO):
        def write(self, b):
            return 0
    with pytest.raises(DrivePakError, match="Short write"):
        write_all(BrokenWriter(), b"x")


def test_read_errors_and_enospc_do_not_publish(device, tmp_path, monkeypatch):
    import drivepak_manager.streams as streams
    original = streams.write_all
    def full(*args):
        raise OSError(errno.ENOSPC, "injected full disk")
    monkeypatch.setattr(streams, "write_all", full)
    output = tmp_path / "backup.img"
    with pytest.raises(OSError):
        device.backup(output)
    assert not output.exists() and not Path(str(output) + ".json").exists()
    assert list(tmp_path.glob("*.partial")) or list(tmp_path.glob(".*.partial"))
    monkeypatch.setattr(streams, "write_all", original)


def test_snapshot_source_stability_failure(device, tmp_path):
    changed = False
    def progress(done, total, phase):
        nonlocal changed
        if phase == "Verifying saved backup" and not changed:
            with device.path.open("r+b") as f:
                f.write(b"changed")
            changed = True
    output = tmp_path / "unstable.img"
    with pytest.raises(SafetyError, match="changed during backup"):
        device.backup(output, progress)
    assert not output.exists()


def test_cancellation_and_replacement(device, tmp_path):
    event = threading.Event()
    event.set()
    output = tmp_path / "cancelled.img"
    with pytest.raises(Cancelled):
        device.backup(output, cancel=event)
    assert not output.exists()
    old = tmp_path / "old-device"
    device.path.rename(old)
    device.path.write_bytes(bytes(device.capacity))
    with pytest.raises(SafetyError, match="replaced"):
        device.guard()


def test_undersized_and_hardlink_rejection(device, tmp_path):
    backup = tmp_path / "backup.img"
    device.backup(backup)
    image = tmp_path / "too-big.img"
    image.write_bytes(bytes(device.capacity + 512))
    before = device.path.read_bytes()
    with pytest.raises(SafetyError, match="fitting"):
        device.write(image, backup)
    assert device.path.read_bytes() == before
    alias = tmp_path / "alias.img"
    alias.hardlink_to(backup)
    with pytest.raises(SafetyError, match="hard links"):
        device.write(alias, backup)


def test_readback_mismatch_is_never_success(device, tmp_path):
    backup = tmp_path / "backup.img"
    manifest = device.backup(backup)
    source = tmp_path / "source.img"
    source.write_bytes(b"p" * 512)

    class CorruptReadback:
        def __init__(self, file):
            self.file, self.did_write = file, False
        def __getattr__(self, key):
            return getattr(self.file, key)
        def write(self, b):
            self.did_write = True
            return self.file.write(b)
        def read(self, n):
            b = self.file.read(n)
            return b"\0" + b[1:] if b and self.did_write else b

    with device.path.open("r+b", buffering=0) as target, source.open("rb", buffering=0) as src, backup.open("rb", buffering=0) as back:
        with pytest.raises(DrivePakError, match="PHYSICAL WRITE INCOMPLETE.*readback"):
            restore_stream(CorruptReadback(target), device.capacity, device.identity, src, back, manifest)
    assert backup.read_bytes() != device.path.read_bytes()
    assert verify_backup(backup)["status"] == "verified"


def test_cancellation_after_write_reports_nonatomic(device, tmp_path):
    backup = tmp_path / "backup.img"
    device.backup(backup)
    source = tmp_path / "source.img"
    source.write_bytes(bytes(device.capacity))
    event = threading.Event()
    def progress(done, total, phase):
        if phase == "Copying":
            event.set()
    with pytest.raises(DrivePakError, match="PHYSICAL WRITE INCOMPLETE"):
        device.write(source, backup, progress, event)
    assert verify_backup(backup)


def test_failed_output_verification_and_space_checks(device, tmp_path, monkeypatch):
    import drivepak_manager.streams as streams
    monkeypatch.setattr(streams.shutil, "disk_usage", lambda p: type("Space", (), {"free": 1})())
    with pytest.raises(SafetyError, match="Insufficient"):
        device.backup(tmp_path / "no-space.img")


def test_regular_file_operations_reject_device_nodes(tmp_path):
    with pytest.raises(SafetyError):
        SimulatedDevice(Path("/dev/null"))
    with pytest.raises(SafetyError):
        copy_verified("/dev/null", tmp_path / "bad.img")


def test_truncated_source_and_read_error_abort_snapshot(device, tmp_path):
    output = tmp_path / "truncated.img"
    device.capacity += 512
    with pytest.raises(DrivePakError, match="Short read"):
        device.backup(output)
    assert not output.exists() and not Path(str(output) + ".json").exists()
    device.capacity -= 512
    class BadReader:
        def __init__(self, f):
            self.file = f
        def __getattr__(self, name):
            return getattr(self.file, name)
        def read(self, count):
            raise OSError(errno.EIO, "injected device read error")
    with device.path.open("rb", buffering=0) as target, (tmp_path / "partial").open("w+b", buffering=0) as out, (tmp_path / "partial").open("rb", buffering=0) as check:
        with pytest.raises(OSError, match="read error"):
            snapshot_stream(BadReader(target), device.capacity, device.identity, out, check)


def test_saved_copy_mismatch_is_partial_only(device, tmp_path, monkeypatch):
    import drivepak_manager.streams as streams
    monkeypatch.setattr(streams, "hash_stream", lambda *args, **kwargs: "bad digest")
    output = tmp_path / "bad-copy.img"
    with pytest.raises(DrivePakError, match="verification failed"):
        copy_verified(device.path, output)
    assert not output.exists()


def test_atomic_publication_never_replaces_a_racing_output(tmp_path):
    from drivepak_manager.streams import atomic_output
    output = tmp_path / "protected.img"
    with pytest.raises(FileExistsError):
        with atomic_output(output) as f:
            f.write(b"new data")
            output.write_bytes(b"only recoverable backup")
    assert output.read_bytes() == b"only recoverable backup"


def test_short_device_write_never_success(device, tmp_path):
    backup = tmp_path / "backup.img"
    manifest = device.backup(backup)
    source = tmp_path / "source.img"
    source.write_bytes(bytes(512))
    class BadWriter:
        def __init__(self, f):
            self.file = f
        def __getattr__(self, name):
            return getattr(self.file, name)
        def write(self, b):
            return 0
    with device.path.open("r+b", buffering=0) as target, source.open("rb", buffering=0) as src, backup.open("rb", buffering=0) as back:
        with pytest.raises(DrivePakError, match="PHYSICAL WRITE INCOMPLETE.*Short write"):
            restore_stream(BadWriter(target), device.capacity, device.identity, src, back, manifest)
