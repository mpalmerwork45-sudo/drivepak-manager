"""Byte-built classic RBF volumes; writer checks never use downloaded content."""
import json
import os
from pathlib import Path
import stat
import threading

import pytest

from drivepak_manager.errors import Cancelled, FormatError, SafetyError
from drivepak_manager.filesystems.os9 import OS9
from drivepak_manager.image import Image


def number24(value):
    return value.to_bytes(3, "big")


def rbf_name(value):
    raw = bytearray(value.encode("ascii"))
    raw[-1] |= 0x80
    return bytes(raw).ljust(29, b"\0")


def logical_payload(image, partition):
    """Independent literal 512/256 mapping for exact whole-image checks."""
    raw = image.path.read_bytes()
    return b"".join(raw[(partition.start + n) * 512:(partition.start + n) * 512 + 256]
                    for n in range(partition.count))


def fd_segments(raw, lsn):
    descriptor = raw[lsn * 256:(lsn + 1) * 256]
    result = []
    for offset in range(16, 256, 5):
        start = int.from_bytes(descriptor[offset:offset + 3], "big")
        count = int.from_bytes(descriptor[offset + 3:offset + 5], "big")
        if not start and not count:
            break
        result.append((start, count))
    return descriptor, result


def lookup(raw, name, directory_lsn=3):
    directory, segments = fd_segments(raw, directory_lsn)
    data = b"".join(raw[start * 256:(start + count) * 256] for start, count in segments)
    data = data[:int.from_bytes(directory[9:13], "big")]
    for offset in range(0, len(data), 32):
        entry = data[offset:offset + 32]
        if not entry[0]:
            continue
        decoded = bytearray()
        for value in entry[:29]:
            decoded.append(value & 0x7f)
            if value & 0x80:
                break
        if decoded.decode("ascii").casefold() == name.casefold():
            return int.from_bytes(entry[29:32], "big")
    raise AssertionError(f"Independent directory reader did not find {name}")


def file_bytes(raw, name, directory_lsn=3):
    descriptor, segments = fd_segments(raw, lookup(raw, name, directory_lsn))
    size = int.from_bytes(descriptor[9:13], "big")
    return b"".join(raw[start * 256:(start + count) * 256]
                    for start, count in segments)[:size]


@pytest.fixture
def os9_image(card, os9_volume):
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_volume)
        part = image.layout().partition(index)
        # Realistic non-FF padding, independent of logical volume contents.
        with image.transaction():
            for n in range(part.count):
                image.write((part.start + n) * 512 + 256, bytes([(n * 17 + 3) % 251]) * 256)
        yield image, index, part


def full_directory(source, destination):
    raw = bytearray(source.read_bytes())
    raw[3 * 256 + 9:3 * 256 + 13] = (256).to_bytes(4, "big")
    for n in range(5):
        lsn = 7 + n
        descriptor = lsn * 256
        raw[descriptor] = 0x1b
        raw[descriptor + 8] = 1
        offset = 4 * 256 + (n + 3) * 32
        raw[offset:offset + 29] = rbf_name(f"EXTRA{n}")
        raw[offset + 29:offset + 32] = number24(lsn)
    raw[256:260] = b"\xff\xf0\0\0"  # sectors 0..11 allocated
    destination.write_bytes(raw)
    return destination


def test_insert_binary_file_preserves_unrelated_regions_and_padding(os9_image, tmp_path):
    image, index, part = os9_image
    source = tmp_path / "GAME.BIN"
    content = b"\0\xff\r\n\x80" * 151 + b"tail\0\xff"
    source.write_bytes(content)
    before = image.path.read_bytes()
    original_volume = logical_payload(image, part)
    free_before = OS9(image.volume(index)).validate_for_edit()["free_bytes"]
    OS9(image.volume(index, editing=True)).insert(source)
    result = logical_payload(image, part)
    assert file_bytes(result, "game.bin") == content
    assert file_bytes(result, "HELLO") == file_bytes(original_volume, "HELLO")
    assert result[:256] == original_volume[:256]  # header/reserved/boot metadata
    assert result[2 * 256:3 * 256] == original_volume[2 * 256:3 * 256]
    new_fd, new_segments = fd_segments(result, lookup(result, "GAME.BIN"))
    allocated = 1 + sum(n for _, n in new_segments)
    assert OS9(image.volume(index)).validate_for_edit()["free_bytes"] == free_before - allocated * 256
    assert new_fd[0] == 0x1b and new_fd[1:3] == b"\0\0" and new_fd[8] == 1
    after = image.path.read_bytes()
    assert before[:part.start * 512] == after[:part.start * 512]
    assert before[part.end * 512:] == after[part.end * 512:]
    for n in range(part.count):
        pos = (part.start + n) * 512 + 256
        assert before[pos:pos + 256] == after[pos:pos + 256]


@pytest.mark.parametrize("length", [0, 1, 255, 256, 257, 1024])
def test_metadata_length_not_trailing_byte_scan(os9_image, tmp_path, length):
    image, index, part = os9_image
    source = tmp_path / "TRAILER"
    content = (bytes(range(256)) * 4)[:length]
    if length:
        content = content[:-1] + b"\0"
    source.write_bytes(content)
    OS9(image.volume(index, editing=True)).insert(source)
    raw = logical_payload(image, part)
    descriptor, _ = fd_segments(raw, lookup(raw, "TRAILER"))
    assert int.from_bytes(descriptor[9:13], "big") == length
    assert file_bytes(raw, "TRAILER") == content


def test_rename_only_changes_name_and_parent_timestamp(os9_image):
    image, index, part = os9_image
    before = image.path.read_bytes()
    old_fd = OS9(image.volume(index)).resolve("/HELLO").raw_fd
    OS9(image.volume(index, editing=True)).rename("/hello", "Renamed")
    raw = logical_payload(image, part)
    assert lookup(raw, "RENAMED") == 5
    assert raw[5 * 256:6 * 256] == old_fd
    assert file_bytes(raw, "renamed") == b"OS9\0\xff\r\nBINARY"
    allowed = set()
    for offset, length in ((4 * 256 + 64, 29), (3 * 256 + 3, 5)):
        for n in range(offset, offset + length):
            allowed.add((part.start + n // 256) * 512 + n % 256)
    after = image.path.read_bytes()
    assert all(a == b or position in allowed for position, (a, b) in enumerate(zip(before, after)))


def test_delete_frees_exact_fd_data_and_reuses_deleted_directory_entry(os9_image, tmp_path):
    image, index, part = os9_image
    before = logical_payload(image, part)
    free_before = OS9(image.volume(index)).validate_for_edit()["free_bytes"]
    OS9(image.volume(index, editing=True)).delete("/HELLO")
    deleted = logical_payload(image, part)
    assert deleted[4 * 256 + 64] == 0
    assert deleted[5 * 256:7 * 256] == before[5 * 256:7 * 256]
    assert deleted[256] == 0xf8  # sectors 0,1,2,3,4 stay used; 5,6 freed
    assert OS9(image.volume(index)).validate_for_edit()["free_bytes"] == free_before + 512
    source = tmp_path / "REUSED"
    source.write_bytes(b"new exact bytes")
    OS9(image.volume(index, editing=True)).insert(source)
    inserted = logical_payload(image, part)
    assert inserted[4 * 256 + 64:4 * 256 + 93] == rbf_name("REUSED")
    assert int.from_bytes(inserted[3 * 256 + 9:3 * 256 + 13], "big") == 96
    assert file_bytes(inserted, "reused") == source.read_bytes()


def test_replace_preserves_owner_attributes_and_creation_date(os9_image, tmp_path):
    image, index, part = os9_image
    old = OS9(image.volume(index)).resolve("/HELLO")
    source = tmp_path / "HELLO"
    source.write_bytes(b"replacement\0\xff\r\n" * 31)
    before = image.path.read_bytes()
    with pytest.raises(FormatError, match="exist|replace|Replace"):
        OS9(image.volume(index, editing=True)).insert(source)
    assert image.path.read_bytes() == before
    OS9(image.volume(index, editing=True)).insert(source, replace=True)
    new = OS9(image.volume(index)).resolve("/HELLO")
    assert new.raw_fd[0:3] == old.raw_fd[0:3]
    assert new.raw_fd[13:16] == old.raw_fd[13:16]
    assert new.raw_fd[8] == 1
    assert file_bytes(logical_payload(image, part), "HELLO") == source.read_bytes()


@pytest.mark.parametrize("length", [0, 26 * 256])
def test_replacement_reclaims_old_data_without_requiring_another_descriptor(os9_image, tmp_path, length):
    image, index, part = os9_image
    source = tmp_path / "HELLO"
    source.write_bytes(b"\x93" * length)
    original_lsn = OS9(image.volume(index)).resolve("/HELLO").lsn
    OS9(image.volume(index, editing=True)).insert(source, replace=True)
    raw = logical_payload(image, part)
    assert lookup(raw, "HELLO") == original_lsn
    assert file_bytes(raw, "HELLO") == source.read_bytes()
    if length:
        assert OS9(image.volume(index)).validate_for_edit()["free_bytes"] == 0


def test_extraction_sidecar_restores_metadata_and_rejects_changed_content(os9_image, tmp_path):
    image, index, part = os9_image
    source = tmp_path / "extracted"
    original = OS9(image.volume(index)).resolve("/HELLO")
    OS9(image.volume(index)).extract("/HELLO", source)
    metadata = json.loads(Path(str(source) + ".drivepak.json").read_text())
    assert metadata["bytes"] == len(source.read_bytes())
    OS9(image.volume(index, editing=True)).insert(source, name="COPY")
    copied = OS9(image.volume(index)).resolve("/COPY")
    assert copied.raw_fd[:8] == original.raw_fd[:8]
    assert copied.raw_fd[13:16] == original.raw_fd[13:16]
    assert file_bytes(logical_payload(image, part), "COPY") == source.read_bytes()
    source.write_bytes(source.read_bytes() + b"tampered")
    before = image.path.read_bytes()
    with pytest.raises(FormatError, match="metadata|sidecar|differ|checksum"):
        OS9(image.volume(index, editing=True)).insert(source, name="BAD")
    assert image.path.read_bytes() == before


def test_directory_grows_without_overwriting_existing_entries(card, os9_volume, tmp_path):
    volume = full_directory(os9_volume, tmp_path / "full-dir.os9")
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=volume)
        source = tmp_path / "ADDED"
        source.write_bytes(b"directory growth")
        before = OS9(image.volume(index)).list_dir()
        OS9(image.volume(index, editing=True)).insert(source)
        fs = OS9(image.volume(index))
        assert {f.name for f in fs.list_dir()} == {f.name for f in before} | {"ADDED"}
        root = fs.resolve("/")
        assert root.size >= 288 and root.size % 32 == 0
        fs.validate_for_edit()


def fragmented_volume(source, destination, sectors=32):
    raw = bytearray(source.read_bytes()).ljust(sectors * 256, b"\xa5")
    raw[:3] = number24(sectors)
    bitmap_bytes = (sectors + 7) // 8
    raw[4:6] = bitmap_bytes.to_bytes(2, "big")
    raw[256:256 + bitmap_bytes] = bytes(bitmap_bytes)
    # Unreachable allocated sectors are deliberately kept, simulating reserved
    # areas. Only isolated even sectors above the existing filesystem are free.
    for sector in range(sectors):
        if sector <= 6 or sector % 2:
            raw[256 + sector // 8] |= 0x80 >> (sector % 8)
    destination.write_bytes(raw)
    return destination


def test_fragmented_bitmap_builds_correct_multiple_segments(card, os9_volume, tmp_path):
    volume = fragmented_volume(os9_volume, tmp_path / "fragmented.os9")
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=volume)
        part = image.layout().partition(index)
        source = tmp_path / "FRAGMENTED"
        source.write_bytes(bytes(range(256)) * 2 + b"last\0\xff" * 20)
        before = logical_payload(image, part)
        OS9(image.volume(index, editing=True)).insert(source)
        raw = logical_payload(image, part)
        _, segments = fd_segments(raw, lookup(raw, "FRAGMENTED"))
        assert len(segments) == 3 and all(count == 1 for _, count in segments)
        assert file_bytes(raw, "FRAGMENTED") == source.read_bytes()
        for sector in range(7, 32, 2):
            assert raw[sector * 256:(sector + 1) * 256] == before[sector * 256:(sector + 1) * 256]
        OS9(image.volume(index)).validate_for_edit()


def test_fragmentation_exceeding_descriptor_capacity_fails_without_writes(card, os9_volume, tmp_path):
    volume = fragmented_volume(os9_volume, tmp_path / "too-fragmented.os9", sectors=128)
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=volume)
        source = tmp_path / "TOO_FRAGMENTED"
        source.write_bytes(b"\x93" * (49 * 256))
        before = image.path.read_bytes()
        with pytest.raises(FormatError, match="fragment|segment|Fragment|Segment"):
            OS9(image.volume(index, editing=True)).insert(source)
        assert image.path.read_bytes() == before


def test_directory_growth_keeps_segments_separate_at_bitmap_sector_boundary(card, os9_volume, tmp_path):
    full = full_directory(os9_volume, tmp_path / "full.os9").read_bytes()
    raw = bytearray(full).ljust(2050 * 256, b"\xa5")
    raw[:3] = number24(2050)
    raw[4:6] = (257).to_bytes(2, "big")
    raw[3 * 256 + 16:3 * 256 + 19] = number24(2047)
    raw[2047 * 256:2048 * 256] = full[4 * 256:5 * 256]
    raw[256:513] = b"\xff" * 257
    raw[257] &= ~0x08  # sole early free sector12 for the new empty file FD
    raw[512] &= ~0x80  # sector2048 for the next directory segment
    volume = tmp_path / "boundary.os9"
    volume.write_bytes(raw)
    # The ordinary synthetic card has 2048 free physical sectors. Extend only
    # this disposable fixture by eight sectors to accommodate the 2050-sector RBF.
    with card.open("ab") as output:
        output.write(b"\xa5" * 4096)
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=volume)
        part = image.layout().partition(index)
        source = tmp_path / "EMPTY"
        source.write_bytes(b"")
        OS9(image.volume(index, editing=True)).insert(source)
        result = logical_payload(image, part)
        descriptor, segments = fd_segments(result, 3)
        assert segments == [(2047, 1), (2048, 1)]
        assert int.from_bytes(descriptor[9:13], "big") == 288
        assert lookup(result, "EMPTY") == 12
        assert OS9(image.volume(index)).validate_for_edit()["free_bytes"] == 0


def test_nested_directory_insert_rename_and_delete(card, os9_nested_volume, tmp_path):
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_nested_volume)
        source = tmp_path / "PROGRAM"
        source.write_bytes(b"\x87\xcd\0\xff")
        OS9(image.volume(index, editing=True)).insert(source, directory="/cmds")
        fs = OS9(image.volume(index))
        assert fs.resolve("/CMDS/program").size == 4
        OS9(image.volume(index, editing=True)).rename("/CMDS/PROGRAM", "GO")
        assert OS9(image.volume(index)).resolve("/cmds/go").size == 4
        OS9(image.volume(index, editing=True)).delete("/CMDS/GO")
        assert OS9(image.volume(index)).list_dir("/CMDS") == []


@pytest.mark.parametrize("bad_name", ["", ".", "..", "a" * 30, "path/name", "bad\\name", "bad\0name"])
def test_invalid_names_do_not_change_image(os9_image, tmp_path, bad_name):
    image, index, _ = os9_image
    source = tmp_path / "SOURCE"
    source.write_bytes(b"x")
    before = image.path.read_bytes()
    with pytest.raises(FormatError):
        OS9(image.volume(index, editing=True)).insert(source, name=bad_name)
    assert image.path.read_bytes() == before


def test_case_insensitive_collision_and_maximum_name(os9_image, tmp_path):
    image, index, _ = os9_image
    source = tmp_path / "SOURCE"
    source.write_bytes(b"x")
    before = image.path.read_bytes()
    with pytest.raises(FormatError):
        OS9(image.volume(index, editing=True)).insert(source, name="hello")
    assert image.path.read_bytes() == before
    name = "N" * 29
    OS9(image.volume(index, editing=True)).insert(source, name=name)
    assert OS9(image.volume(index)).resolve("/" + name.lower()).size == 1


def test_no_space_leaves_every_byte_unchanged(os9_image, tmp_path):
    image, index, _ = os9_image
    source = tmp_path / "TOO_BIG"
    source.write_bytes(b"x" * (26 * 256))
    before = image.path.read_bytes()
    with pytest.raises(FormatError, match="space|free|large|allocation"):
        OS9(image.volume(index, editing=True)).insert(source)
    assert image.path.read_bytes() == before


@pytest.mark.parametrize("fault", ["cancel", "write-error"])
def test_interrupted_file_insert_rolls_back_entire_working_image(os9_image, tmp_path, monkeypatch, fault):
    image, index, _ = os9_image
    source = tmp_path / "ROLLBACK"
    source.write_bytes(b"\0\xff\r\n" * 600)
    event = threading.Event()
    view = image.volume(index, editing=True)
    real_write = view.write
    calls = []
    def interrupted_write(offset, data):
        real_write(offset, data)
        calls.append(offset)
        if fault == "write-error":
            raise OSError("injected error after mutation")
        event.set()
    monkeypatch.setattr(view, "write", interrupted_write)
    before = image.path.read_bytes()
    with pytest.raises((Cancelled, OSError)):
        OS9(view).insert(source, cancel=event)
    assert calls and image.path.read_bytes() == before


@pytest.mark.parametrize("phase", ["Checking OS-9 import source", "Inserting OS-9 file"])
def test_changing_source_during_import_is_rejected_or_rolled_back(os9_image, tmp_path, phase):
    image, index, _ = os9_image
    source = tmp_path / "UNSTABLE"
    source.write_bytes(b"data\0\xff" * 400)
    changed = []
    def change_source(done, total, label):
        if done and label == phase and not changed:
            with source.open("ab") as output:
                output.write(b"external change")
            changed.append(True)
    before = image.path.read_bytes()
    with pytest.raises(SafetyError, match="source changed"):
        OS9(image.volume(index, editing=True)).insert(source, progress=change_source)
    assert changed and image.path.read_bytes() == before


@pytest.mark.parametrize("corruption", ["bitmap-free-data", "bitmap-free-fd", "crosslink", "linkcount", "directory-cycle", "wrong-parent", "cluster2"])
def test_ambiguous_allocation_refuses_edits_but_preserves_image(os9_image, tmp_path, corruption):
    image, index, _ = os9_image
    view = image.volume(index, editing=True)
    with image.transaction():
        if corruption == "bitmap-free-data":
            view.write(256, b"\xfc")  # sector6 was live file data
        elif corruption == "bitmap-free-fd":
            view.write(256, b"\xfa")  # sector5 was live file descriptor
        elif corruption == "crosslink":
            view.write(5 * 256 + 16, number24(4))  # file points into root directory
        elif corruption == "linkcount":
            view.write(5 * 256 + 8, b"\x02")
        elif corruption == "directory-cycle":
            view.write(4 * 256 + 64, rbf_name("LOOP") + number24(3))
        elif corruption == "wrong-parent":
            view.write(4 * 256 + 32 + 29, number24(5))
        else:
            view.write(6, (2).to_bytes(2, "big"))
    before = image.path.read_bytes()
    source = tmp_path / "BLOCKED"
    source.write_bytes(b"must not appear")
    with pytest.raises(FormatError):
        OS9(view).validate_for_edit()
    with pytest.raises(FormatError):
        OS9(view).insert(source)
    assert image.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["insert", "delete"])
def test_boot_file_payload_is_not_replaced_or_freed(os9_image, tmp_path, operation):
    image, index, _ = os9_image
    view = image.volume(index, editing=True)
    with image.transaction():
        view.write(21, number24(6))
        view.write(24, (256).to_bytes(2, "big"))
    before = image.path.read_bytes()
    source = tmp_path / "HELLO"
    source.write_bytes(b"replacement")
    with pytest.raises(FormatError, match="boot|Boot"):
        if operation == "insert":
            OS9(view).insert(source, replace=True)
        else:
            OS9(view).delete("/HELLO")
    assert image.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_directory_mutations_are_rejected(os9_image, operation):
    image, index, _ = os9_image
    before = image.path.read_bytes()
    with pytest.raises(FormatError):
        if operation == "rename":
            OS9(image.volume(index, editing=True)).rename("/", "ROOT")
        else:
            OS9(image.volume(index, editing=True)).delete("/")
    assert image.path.read_bytes() == before


@pytest.mark.parametrize("offset", [0x64, 0x67, 0x69])
def test_ambiguous_extended_metadata_keeps_browsing_but_refuses_file_edits(os9_image, tmp_path, offset):
    image, index, _ = os9_image
    view = image.volume(index, editing=True)
    with image.transaction():
        view.write(offset, b"\x01")
    before = image.path.read_bytes()
    fs = OS9(view)
    assert [f.name for f in fs.list_dir()] == ["HELLO"]
    with pytest.raises(FormatError):
        fs.validate_for_edit()
    source = tmp_path / "BLOCKED"
    source.write_bytes(b"bytes")
    with pytest.raises(FormatError):
        fs.insert(source)
    assert image.path.read_bytes() == before


def test_explicit_non_256_sector_size_is_rejected(os9_image):
    image, index, _ = os9_image
    view = image.volume(index, editing=True)
    with image.transaction():
        view.write(0x68, b"\x02")
    before = image.path.read_bytes()
    with pytest.raises(FormatError):
        OS9(view)
    assert image.path.read_bytes() == before


@pytest.mark.parametrize("operation", ["insert", "raw-volume"])
def test_opened_descriptor_type_is_checked_instead_of_trusting_path(os9_image, os9_volume, tmp_path, monkeypatch, operation):
    from drivepak_manager.filesystems.os9 import RawVolume
    image, index, _ = os9_image
    source = tmp_path / "CHANGED"
    source.write_bytes(b"still a disposable regular-file adapter")
    inspected = os9_volume if operation == "raw-volume" else source
    real_open, real_fstat = Path.open, os.fstat
    mismatched = set()
    class ChangedType:
        def __init__(self, file):
            self.file = file
            mismatched.add(file.fileno())
        def __getattr__(self, name):
            return getattr(self.file, name)
        def close(self):
            mismatched.discard(self.file.fileno())
            self.file.close()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.close()
    def opened(path, *args, **kwargs):
        file = real_open(path, *args, **kwargs)
        return ChangedType(file) if path == inspected else file
    def fstat(fd):
        actual = real_fstat(fd)
        if fd not in mismatched:
            return actual
        fields = list(actual)
        fields[0] = stat.S_IFCHR | 0o600
        return os.stat_result(fields)
    monkeypatch.setattr(Path, "open", opened)
    monkeypatch.setattr(os, "fstat", fstat)
    before = image.path.read_bytes()
    with pytest.raises((FormatError, SafetyError), match="regular"):
        if operation == "raw-volume":
            with RawVolume(inspected):
                pass
        else:
            OS9(image.volume(index, editing=True)).insert(inspected)
    assert image.path.read_bytes() == before


def test_insertion_does_not_overwrite_a_later_valid_working_image_edit(os9_image, tmp_path):
    image, index, part = os9_image
    first, second = tmp_path / "FIRST", tmp_path / "SECOND"
    first.write_bytes(b"first import")
    second.write_bytes(b"valid later edit that must survive")
    later = []
    def concurrent_edit(done, total, phase):
        if phase == "Checking OS-9 import source" and done and not later:
            # Simulate another writer between validation/planning and the first
            # transaction. Both files alone fit and form a valid RBF filesystem.
            later.append(True)
            OS9(image.volume(index, editing=True)).insert(second)
            later.append(image.path.read_bytes())
    with pytest.raises(SafetyError, match="working image changed|Working image changed"):
        OS9(image.volume(index, editing=True)).insert(first, progress=concurrent_edit)
    assert len(later) == 2 and image.path.read_bytes() == later[1]
    assert file_bytes(logical_payload(image, part), "SECOND") == second.read_bytes()
    assert {file.name for file in OS9(image.volume(index)).list_dir()} == {"HELLO", "SECOND"}
