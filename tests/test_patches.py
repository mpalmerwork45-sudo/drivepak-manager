import hashlib
import json
import os
from pathlib import Path
import struct
import threading

import pytest

from drivepak_manager.errors import DrivePakError, SafetyError
from drivepak_manager.patches import HEADER, RECORD_SIZE, apply_changes_stream, prepare_changes
from drivepak_manager.streams import write_json


@pytest.fixture
def edit(tmp_path):
    old, new, card, journal = [tmp_path / n for n in ("original.img", "edited.img", "card.bin", "changes.dpp")]
    before = bytes(range(256)) * 16
    after = bytearray(before)
    after[512+17:512+21] = b"TEST"
    after[1536+31] = 0x71
    old.write_bytes(before)
    new.write_bytes(after)
    card.write_bytes(before + b"\x93" * 4096)
    info = prepare_changes(old, new, journal)
    assert info["sectors"] == 2 and info["bytes_changed"] == 1024
    identity = {"simulated": True, "path": str(card), "capacity": 8192, "inode": card.stat().st_ino}
    return old, new, card, journal, identity


def apply(edit, *, recovery=False, progress=None, cancel=None, ready=None, wrapper=lambda x:x,
          guard=None, expected_journal=None):
    old, new, card, journal, identity = edit
    saved = Path(str(journal) + ".json")
    manifest = json.loads(saved.read_text()) if recovery else None
    def save(receipt):
        if ready:
            ready(receipt)
        elif not recovery:
            assert card.read_bytes()[:4096] == old.read_bytes()
            write_json(saved, receipt)
    with card.open("r+b", buffering=0) as target, journal.open("rb", buffering=0) as source:
        return apply_changes_stream(wrapper(target), 8192, identity, source, save, progress, cancel, guard,
                                    acknowledge_in_place=True, recovery=recovery, manifest=manifest,
                                    expected_journal=expected_journal)


def test_apply_and_recover_only_changed_physical_sectors(edit):
    old, new, card, journal, identity = edit
    before = card.read_bytes()
    result = apply(edit)
    assert result["status"] == "verified" and result["bytes_written"] == 1024
    assert card.read_bytes() == new.read_bytes() + before[4096:]
    receipt = json.loads(Path(str(journal) + ".json").read_text())
    assert receipt["kind"] == "changed-sector-recovery" and "NOT a complete" in receipt["scope"]
    # Simulate an interrupted attempt: one sector already retains its before bytes.
    with card.open("r+b") as f:
        f.seek(512)
        f.write(before[512:1024])
    assert apply(edit, recovery=True)["status"] == "verified"
    assert card.read_bytes() == before
    assert old.read_bytes() == before[:4096] and journal.exists()


def test_different_baseline_and_missing_durable_receipt_fail_before_writing(edit):
    old, new, card, journal, identity = edit
    with card.open("r+b") as f:
        f.seek(3000)
        f.write(b"unexpected")
    changed = card.read_bytes()
    with pytest.raises(SafetyError, match="differs from the editor"):
        apply(edit)
    assert card.read_bytes() == changed and not Path(str(journal) + ".json").exists()
    card.write_bytes(old.read_bytes() + changed[4096:])
    def full_disk(receipt):
        raise OSError("no space for receipt")
    with pytest.raises(OSError, match="no space"):
        apply(edit, ready=full_disk)
    assert card.read_bytes()[:4096] == old.read_bytes()


@pytest.mark.parametrize("fault", ["truncated", "duplicate", "unaligned", "outside", "wrong-before"])
def test_malformed_journal_never_writes_target(edit, fault):
    old, new, card, journal, identity = edit
    raw = bytearray(journal.read_bytes())
    if fault == "truncated":
        raw.pop()
    elif fault == "duplicate":
        raw[HEADER.size+RECORD_SIZE:HEADER.size+RECORD_SIZE+8] = raw[HEADER.size:HEADER.size+8]
    elif fault in ("unaligned", "outside"):
        raw[HEADER.size:HEADER.size+8] = struct.pack(">Q", 513 if fault == "unaligned" else 8192)
    else:
        raw[HEADER.size+8] ^= 0xFF
    journal.chmod(0o600)
    journal.write_bytes(raw)
    before = card.read_bytes()
    with pytest.raises(SafetyError):
        apply(edit)
    assert card.read_bytes() == before


def test_cancellation_and_failed_readback_keep_recovery(edit):
    event = threading.Event()
    def cancel(done, total, phase):
        if phase == "Writing changed card sectors":
            event.set()
    with pytest.raises(DrivePakError, match="PHYSICAL EDITS INCOMPLETE"):
        apply(edit, progress=cancel, cancel=event)
    assert Path(str(edit[3]) + ".json").is_file()
    assert apply(edit, recovery=True)["status"] == "verified"
    # Retain the original verified receipt for the same journal.
    class CorruptReadback:
        def __init__(self, file): self.file, self.wrote = file, False
        def __getattr__(self, name): return getattr(self.file, name)
        def write(self, b):
            self.wrote = True
            return self.file.write(b)
        def read(self, n):
            b = self.file.read(n)
            return (bytes([b[0] ^ 0xFF]) + b[1:]) if b and self.wrote else b
    with pytest.raises(DrivePakError, match="PHYSICAL EDITS INCOMPLETE.*readback mismatch"):
        apply(edit, ready=lambda receipt:None, wrapper=CorruptReadback)
    assert apply(edit, recovery=True)["status"] == "verified"


def test_recovery_rejects_foreign_target_and_torn_sector(edit):
    apply(edit)
    identity = edit[4]
    identity["inode"] += 1
    with pytest.raises(SafetyError, match="target identity"):
        apply(edit, recovery=True)
    identity["inode"] -= 1
    with edit[2].open("r+b") as f:
        f.seek(512)
        f.write(b"torn contents")
    before = edit[2].read_bytes()
    with pytest.raises(SafetyError, match="differs from the journal"):
        apply(edit, recovery=True)
    assert edit[2].read_bytes() == before


def test_recovery_rejects_later_edits_outside_journal(edit):
    apply(edit)
    # A later edit can be filesystem metadata in a different sector. Restoring
    # only the old journal would combine two incompatible card states.
    with edit[2].open("r+b") as target:
        target.seek(3072)
        target.write(b"LATER FILESYSTEM METADATA")
    before = edit[2].read_bytes()
    with pytest.raises(SafetyError, match="outside.*journal"):
        apply(edit, recovery=True)
    assert edit[2].read_bytes() == before


def test_recovery_rejects_other_card_in_same_reader(edit):
    # USB serial numbers often identify the reader, not the inserted card.
    edit[4].update(model="USB reader", serial="READER-1", transport="usb")
    apply(edit)
    edit[4]["diskseq"] = "replacement"
    with edit[2].open("r+b") as target:
        target.seek(2560)
        target.write(b"OTHER CARD: same edited sector contents, different rest")
    before = edit[2].read_bytes()
    with pytest.raises(SafetyError, match="outside.*journal"):
        apply(edit, recovery=True)
    assert edit[2].read_bytes() == before


def test_journal_creation_verifies_persisted_record_bytes(tmp_path, monkeypatch):
    import drivepak_manager.patches as patches
    original, modified, journal = [tmp_path / n for n in ("original.img", "modified.img", "edits.dpp")]
    original.write_bytes(bytes(1024))
    modified.write_bytes(b"x" * 512 + bytes(512))
    real_write = patches.write_all
    def corrupt_record(file, data):
        if len(data) == RECORD_SIZE:
            data = bytearray(data)
            data[-1] ^= 1
        real_write(file, data)
    monkeypatch.setattr(patches, "write_all", corrupt_record)
    with pytest.raises(DrivePakError, match="journal.*verification"):
        prepare_changes(original, modified, journal)
    assert not journal.exists()


def test_reviewed_journal_cannot_be_replaced_with_different_edits(edit):
    journal = edit[3]
    reviewed = hashlib.sha256(journal.read_bytes()).hexdigest()
    raw = bytearray(journal.read_bytes())
    raw[HEADER.size + 8 + 512 + 9] ^= 0x33  # valid record, different after bytes
    journal.chmod(0o600)
    journal.write_bytes(raw)
    before = edit[2].read_bytes()
    with pytest.raises(SafetyError, match="journal changed after preparation or review"):
        apply(edit, expected_journal=reviewed)
    assert edit[2].read_bytes() == before and not Path(str(journal) + ".json").exists()


class FragmentedIO:
    def __init__(self, file):
        self.file = file
    def __getattr__(self, name):
        return getattr(self.file, name)
    def read(self, size):
        return self.file.read(min(size, 73))
    def write(self, data):
        return self.file.write(data[:37])


def test_short_successful_reads_and_writes_preserve_every_byte(edit):
    original = edit[2].read_bytes()
    assert apply(edit, wrapper=FragmentedIO)["status"] == "verified"
    assert edit[2].read_bytes() == edit[1].read_bytes() + original[4096:]
    assert apply(edit, recovery=True, wrapper=FragmentedIO)["status"] == "verified"
    assert edit[2].read_bytes() == original


@pytest.mark.parametrize("fault", ["eof", "read-error", "zero-write", "partial-write"])
def test_io_faults_never_report_edits_verified(edit, fault):
    original = edit[2].read_bytes()
    class Fault(FragmentedIO):
        def read(self, size):
            if fault == "eof":
                return b""
            if fault == "read-error":
                raise OSError("Simulated unplug during read")
            return super().read(size)
        def write(self, data):
            if fault == "zero-write":
                return 0
            self.file.write(data[:19])  # stops halfway through the four changed bytes
            raise OSError("Simulated unplug after a partial sector write")
    with pytest.raises((DrivePakError, OSError)) as failure:
        apply(edit, wrapper=Fault)
    receipt = Path(str(edit[3]) + ".json")
    if fault in ("eof", "read-error"):
        assert not receipt.exists() and edit[2].read_bytes() == original
    else:
        assert "PHYSICAL EDITS INCOMPLETE OR UNVERIFIED" in str(failure.value)
        assert receipt.exists()
        if fault == "partial-write":
            torn = edit[2].read_bytes()
            with pytest.raises(SafetyError, match="differs from the journal"):
                apply(edit, recovery=True)
            assert edit[2].read_bytes() == torn
        else:
            assert edit[2].read_bytes() == original


def test_target_flush_failure_keeps_journal_and_can_recover(edit, monkeypatch):
    real_sync = os.fsync
    target_fd = None
    def capture(file):
        nonlocal target_fd
        target_fd = file.fileno()
        return file
    def fail_sync(fd):
        if fd == target_fd:
            raise OSError("Simulated device flush failure")
        real_sync(fd)
    monkeypatch.setattr(os, "fsync", fail_sync)
    before = edit[2].read_bytes()
    with pytest.raises(DrivePakError, match="INCOMPLETE OR UNVERIFIED.*flush failure"):
        apply(edit, wrapper=capture)
    assert Path(str(edit[3]) + ".json").is_file()
    monkeypatch.setattr(os, "fsync", real_sync)
    assert apply(edit, recovery=True)["status"] == "verified"
    assert edit[2].read_bytes() == before


def test_guard_rejects_replacement_after_recovery_checkpoint(edit):
    replaced = False
    def guard():
        if replaced:
            raise SafetyError("Simulated device replacement")
    def ready(receipt):
        nonlocal replaced
        write_json(str(edit[3]) + ".json", receipt)
        replaced = True
    before = edit[2].read_bytes()
    with pytest.raises(SafetyError, match="device replacement"):
        apply(edit, guard=guard, ready=ready)
    assert edit[2].read_bytes() == before


@pytest.mark.parametrize("fault", ["space", "fsync"])
def test_failed_journal_durability_prevents_publication(edit, tmp_path, monkeypatch, fault):
    import drivepak_manager.patches as patches
    def failure(*args):
        raise OSError("Simulated journal storage failure")
    monkeypatch.setattr(patches if fault == "space" else patches.os,
                        "need_space" if fault == "space" else "fsync", failure)
    output = tmp_path / "failed.dpp"
    before = edit[2].read_bytes()
    with pytest.raises(OSError, match="journal storage failure"):
        prepare_changes(edit[0], edit[1], output)
    assert not output.exists() and edit[2].read_bytes() == before


def test_unacknowledged_edit_and_modified_original_rejected(edit, tmp_path):
    with pytest.raises(SafetyError, match="acknowledgement"):
        apply_changes_stream(None, 0, {}, None, None)
    with pytest.raises(SafetyError, match="original image changed"):
        prepare_changes(edit[0], edit[1], tmp_path / "bad.dpp", expected_original="wrong")
    assert not (tmp_path / "bad.dpp").exists()


def test_partition_and_file_edits_apply_byte_exactly(tmp_path):
    import shutil
    from drivepak_manager.image import Image, create_blank
    from drivepak_manager.filesystems.decb import DECB
    original, modified, target, journal = [tmp_path / n for n in ("original.img", "modified.img", "target.bin", "edits.dpp")]
    create_blank(original, 8 * 1024**2, 1)
    shutil.copyfile(original, modified)
    shutil.copyfile(original, target)
    payload = tmp_path / "TEST.BIN"
    payload.write_bytes(b"\x00\xff\x0d\x0a" * 100)
    with Image(modified, True) as image:
        image.rename_partition(0, "T&D")
        DECB(image.volume(0, 0, editing=True)).insert(payload)
        added = image.create_partition("EXTRA", 1)
        image.set_default("DOS", added)
    from drivepak_manager.backup import SimulatedDevice
    sim = SimulatedDevice(target)
    prepare_changes(original, modified, journal)
    result = sim.apply_changes(journal, str(journal) + ".json")
    assert result["status"] == "verified"
    assert target.read_bytes() == modified.read_bytes()
    with Image(target) as image:
        layout = image.layout()
        assert layout.editable and layout.defaults["DOS"] == layout.partition(added).start
        assert DECB(image.volume(0, 0)).files[0].name == "TEST.BIN"
    sim.apply_changes(journal, tmp_path / "recovered.json", recovery=True)
    assert target.read_bytes() == original.read_bytes()
