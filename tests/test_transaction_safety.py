"""Failure drills for disposable working-image undo data; no devices are used."""
import errno
import io
from pathlib import Path

import pytest

from drivepak_manager.errors import SafetyError
from drivepak_manager.image import Image, Session


class CapacityLimitedRaw(io.RawIOBase):
    """A small filesystem-like cap on the undo file, including short writes."""
    def __init__(self, path, capacity):
        super().__init__()
        self.file = Path(path).open("w+b", buffering=0)
        self.capacity = capacity

    def readable(self):
        return True

    def writable(self):
        return True

    def seekable(self):
        return True

    def fileno(self):
        return self.file.fileno()

    def tell(self):
        return self.file.tell()

    def seek(self, offset, whence=0):
        return self.file.seek(offset, whence)

    def readinto(self, destination):
        return self.file.readinto(destination)

    def write(self, data):
        available = self.capacity - self.file.tell()
        if available <= 0:
            raise OSError(errno.ENOSPC, "Simulated undo storage is full")
        return self.file.write(data[:available])

    def close(self):
        if not self.closed:
            self.file.close()
        super().close()


def test_full_undo_storage_cannot_block_recovery_of_prior_writes(tmp_path, monkeypatch):
    import drivepak_manager.image as images
    path = tmp_path / "working.img"
    before = b"A" * 2048
    path.write_bytes(before)
    journals = []
    def limited_journal(*args, **kwargs):
        raw = CapacityLimitedRaw(tmp_path / "undo.bin", 256)
        journals.append(raw)
        # This reproduces real tempfile buffering, instead of assuming a
        # particular implementation: writes can appear to succeed until seek
        # flushes them while rollback is trying to read saved bytes.
        return raw if kwargs.get("buffering", -1) == 0 else io.BufferedRandom(raw)
    monkeypatch.setattr(images.tempfile, "TemporaryFile", limited_journal)
    with Image(path, True) as image:
        with pytest.raises(OSError, match="undo storage is full"):
            with image.transaction():
                image.write(0, b"B" * 256)
                image.write(256, b"C" * 256)
                raise RuntimeError("Cancellation after buffered undo writes")
        assert path.read_bytes() == before
        assert image.read(0, len(before)) == before
    assert journals


def test_failed_rollback_poisoned_image_cannot_be_read_edited_or_saved(tmp_path, monkeypatch):
    import drivepak_manager.image as images
    original = tmp_path / "original.img"
    original.write_bytes(b"A" * 2048)
    session = Session.open(original, tmp_path / "work")
    real_write = images.write_all
    failures = []
    def failing_target_write(destination, data):
        if destination is session.image.file:
            if not failures:
                real_write(destination, data[:64])
            failures.append(True)
            raise OSError(errno.EIO, "Simulated working storage failure")
        return real_write(destination, data)
    monkeypatch.setattr(images, "write_all", failing_target_write)
    try:
        with pytest.raises(SafetyError, match="rollback|recovery|recover|unusable"):
            with session.image.transaction():
                session.image.write(0, b"B" * 256)
        assert failures and session.image.failure_reason and not session.image.writable
        assert original.read_bytes() == b"A" * 2048
        assert session.working.read_bytes() != original.read_bytes()
        with pytest.raises(SafetyError):
            session.image.read(0, 256)
        with pytest.raises(SafetyError):
            with session.image.transaction():
                pass
        saved = tmp_path / "must-not-save.img"
        with pytest.raises(SafetyError):
            session.save_as(saved)
        assert not saved.exists()
        # A separate verified copy of the immutable original is the recovery
        # route. It never reuses the now-untrusted working file.
        monkeypatch.setattr(images, "write_all", real_write)
        damaged = session.working
        session.discard()
        assert session.working != damaged
        assert session.image.read(0, 2048) == original.read_bytes()
        assert not session.image.failure_reason
    finally:
        session.close()
