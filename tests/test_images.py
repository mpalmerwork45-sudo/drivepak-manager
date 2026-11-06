import hashlib
import json
import threading

import pytest

from drivepak_manager.errors import DrivePakError, FormatError, SafetyError, Cancelled
from drivepak_manager.format import inspect
from drivepak_manager.image import Image, Session, create_blank, read_disk_file
from drivepak_manager.filesystems.decb import DECB
from drivepak_manager.filesystems.os9 import OS9, RawVolume
from drivepak_manager.streams import copy_verified


def changed(before, after):
    assert len(before) == len(after)
    return {i for i, (a, b) in enumerate(zip(before, after)) if a != b}


def test_independent_offsets_deleted_entry_and_preservation(card, tmp_path):
    original = card.read_bytes()
    with Image(card) as image:
        layout = image.layout()
        assert layout.editable and layout.table == 3 and layout.deleted == [0]
        assert layout.partition(1).start == 1024
        assert layout.partition(1).disks == 2
        assert image.volume(1, 1).read(308 * 256, 11) == b"\xff" * 11
        assert image.read((1024 + 2880 + 308) * 512 + 256, 256) == bytes([(308 + 19) % 251]) * 256
    copy_verified(card, tmp_path / "same.img")
    assert (tmp_path / "same.img").read_bytes() == original


@pytest.mark.parametrize("size", [161280, 184320, 322560, 368640, 737280])
def test_explicit_export_import_sizes_and_padding(card, tmp_path, size):
    before = card.read_bytes()
    out = tmp_path / "export.dsk"
    with Image(card, True) as image:
        image.export_disk(1, 1, out, size)
        assert len(out.read_bytes()) == size
        expected = b"".join(before[(1024 + 2880 + n) * 512:(1024 + 2880 + n) * 512 + 256] for n in range(size // 256))
        assert out.read_bytes() == expected
        image.import_disks([(1, 1, out)], "preserve")
    assert card.read_bytes() == before


def test_import_isolated_payload_tail_and_batch_atomicity(card, tmp_path):
    before = card.read_bytes()
    raw = tmp_path / "small.dsk"
    raw.write_bytes(b"\x7c" * 161280)
    with Image(card, True) as image:
        image.import_disks([(1, 1, raw)], "ff")
    after = card.read_bytes()
    delta = changed(before, after)
    assert delta and all((1024 + 2880) * 512 <= i < (1024 + 5760) * 512 and i % 512 < 256 for i in delta)
    assert after[(1024 + 2880 + 630) * 512:(1024 + 2880 + 630) * 512 + 256] == b"\xff" * 256
    with Image(card, True) as image:
        with pytest.raises(FormatError):
            image.import_disks([(1, 0, raw), (1, 0, raw)])
        bad = tmp_path / "headered.dsk"
        bad.write_bytes(b"abc" + raw.read_bytes())
        with pytest.raises(FormatError, match="JVC"):
            image.import_disks([(1, 0, raw), (1, 1, bad)])
    assert card.read_bytes() == after


def test_cancel_rolls_back_transaction(card, tmp_path):
    before = card.read_bytes()
    raw = tmp_path / "raw.dsk"
    raw.write_bytes(b"x" * 161280)
    cancel = threading.Event()
    with Image(card, True) as image:
        with pytest.raises(Cancelled):
            image.import_disks([(1, 0, raw), (1, 1, raw)], progress=lambda *args: cancel.set(), cancel=cancel)
    assert card.read_bytes() == before


def test_transaction_rollback_on_write_failure(card, monkeypatch):
    before = card.read_bytes()
    with Image(card, True) as image:
        with pytest.raises(OSError):
            with image.transaction():
                image.write(120, b"a" * 50)
                image.write(130, b"b" * 30)
                raise OSError("simulated disk full")
    assert card.read_bytes() == before


def test_partition_edits_defaults_and_indices(card):
    before = card.read_bytes()
    with Image(card, True) as image:
        image.rename_partition(1, "RENAMED")
        after = card.read_bytes()
        assert changed(before, after) <= set(range(4 * 512 + 1, 4 * 512 + 17))
        assert image.layout().partition(1).disks == 2
        image.delete_partition(1)
        assert image.layout().defaults["DOS"] == 0
        assert image.read(1024 * 512, 2880 * 2 * 512) == before[1024 * 512:(1024 + 5760) * 512]
        idx = image.create_partition("NEW", 1)
        assert idx == 0  # use a deleted table entry, never compact/renumber live ones
        assert image.layout().defaults["DOS"] == 1024
        assert image.layout().partition(0).disks == 1
        assert image.read(96, 16) == b"unknown-header!!"


def test_partition_reassign_and_create_terminator(tmp_path):
    p = tmp_path / "blank.img"
    create_blank(p, 8 * 1024**2, 1)
    with Image(p, True) as image:
        idx = image.create_partition("TWO", 1)
        assert idx == 1 and image.layout().terminator == 3
        second_start = image.layout().partition(1).start
        image.delete_partition(0, {"DOS": 1})
        assert image.layout().defaults["DOS"] == second_start
        assert image.layout().partition(1).start == second_start
        assert image.create_partition("REUSE", 1) == 0
        assert image.layout().partition(1).start == second_start
        with pytest.raises(FormatError):
            image.set_default("DD", 0)


@pytest.mark.parametrize("mutation, expected", [
    (lambda b: b.__setitem__(slice(17, 20), b"\xff\xff\xfe"), "pointer"),
    (lambda b: b.__setitem__(slice(4 * 512 + 32, 4 * 512 + 35), b"\xff\xff\xff"), "bounds"),
    (lambda b: b.__setitem__(slice(4 * 512 + 32, 4 * 512 + 35), b"\0\0\x03"), "table"),
    (lambda b: b.__setitem__(slice(21, 24), b"\0\0\x07"), "Default DOS"),
    (lambda b: b.__setitem__(16, 1), "extension"),
])
def test_malformed_header_is_diagnostic_only(card, mutation, expected):
    b = bytearray(card.read_bytes())
    mutation(b)
    card.write_bytes(b)
    with Image(card, True) as image:
        layout = image.layout()
        assert not layout.editable and expected.lower() in "; ".join(layout.errors).lower()
        with pytest.raises(FormatError):
            image.rename_partition(1, "NOPE")


def test_overlap_missing_terminator_and_truncation(card):
    b = bytearray(card.read_bytes())
    b[3 * 512:4 * 512] = b[4 * 512:5 * 512]
    card.write_bytes(b)
    with Image(card) as image:
        assert "overlap" in "; ".join(image.layout().errors)
    card.write_bytes(b[:5 * 512])
    with Image(card) as image:
        assert not image.layout().editable
        with pytest.raises(FormatError, match="addressing"):
            image.volume(1, 0)
        assert "terminator" in "; ".join(image.layout().errors)
    card.write_bytes(b[:1000])
    with Image(card) as image:
        assert "truncated" in image.layout().errors[0]


def test_full_table_and_unknown_types(tmp_path):
    path = tmp_path / "many.img"
    b = bytearray(2048 * 512)
    b[17:20] = b"\0\0\x01"
    for idx in range(200):
        b[(idx + 1) * 512] = 1
        b[(idx + 1) * 512 + 32:(idx + 1) * 512 + 35] = b"\0\x04\0"
    b[201 * 512] = 128
    path.write_bytes(b)
    with Image(path) as image:
        assert len(image.layout().partitions) == 200
        assert image.layout().editable


def test_session_immutable_save_discard_hardlinks(card, tmp_path):
    before = card.read_bytes()
    s = Session.open(card, tmp_path / "work")
    s.image.rename_partition(1, "EDITED")
    s.changed("rename")
    s.save_as(tmp_path / "saved.img")
    assert card.read_bytes() == before
    assert (tmp_path / "saved.img").read_bytes() != before
    with pytest.raises(SafetyError):
        s.save_as(card)
    alias = tmp_path / "alias.img"
    alias.hardlink_to(card)
    with pytest.raises(SafetyError):
        s.save_as(alias)
    s.discard()
    assert s.working.read_bytes() == before and not s.changes
    s.close()


def test_discard_rejects_original_changed_between_check_and_copy(card, tmp_path, monkeypatch):
    import drivepak_manager.image as module
    session = Session.open(card, tmp_path / "work")
    session.image.rename_partition(1, "KEEPEDIT")
    session.changed("rename")
    working, edited = session.working, session.working.read_bytes()
    real_hash = module.hash_file
    def replace_after_hash(path, *args, **kwargs):
        digest = real_hash(path, *args, **kwargs)
        with card.open("r+b", buffering=0) as f:
            f.write(b"CHANGED!")
        return digest
    monkeypatch.setattr(module, "hash_file", replace_after_hash)
    try:
        with pytest.raises(SafetyError, match="changed while discarding"):
            session.discard()
        assert session.working == working
        assert session.working.read_bytes() == edited
        assert session.image.layout().partition(1).name == "KEEPEDIT"
        assert session.changes == ["rename"]
    finally:
        session.close()


def test_os9_browse_extract_and_add_replace(card, os9_volume, tmp_path):
    with RawVolume(os9_volume) as raw:
        fs = OS9(raw)
        assert fs.name == "TEST" and [f.name for f in fs.list_dir()] == ["HELLO"]
        fs.extract("/HELLO", tmp_path / "hello")
        assert (tmp_path / "hello").read_bytes() == b"OS9\x00\xff\x0d\x0aBINARY"
    with Image(card, True) as image:
        idx = image.create_partition("OS9", os9_path=os9_volume)
        assert idx == 0 and image.layout().partition(1).disks == 2
        image.set_default("DD", idx)
        original_padding = image.read(image.layout().partition(idx).start * 512 + 256, 256)
        out = tmp_path / "volume.os9"
        image.volume(idx).export(out)
        assert out.read_bytes() == os9_volume.read_bytes()
        image.import_os9(idx, out)
        assert original_padding == image.read(image.layout().partition(idx).start * 512 + 256, 256)
        image.delete_partition(idx)
        assert image.layout().defaults["DD"] == 0


@pytest.mark.parametrize("replace", [False, True])
def test_os9_volume_import_is_bound_to_validated_file(card, os9_volume, monkeypatch, replace):
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_volume) if replace else None
        before = card.read_bytes()
        real_transaction = image.transaction
        def swap_source_after_validation():
            size = os9_volume.stat().st_size
            os9_volume.rename(os9_volume.with_suffix(".old"))
            os9_volume.write_bytes(bytes(size))  # same length, invalid replacement
            return real_transaction()
        monkeypatch.setattr(image, "transaction", swap_source_after_validation)
        with pytest.raises(SafetyError, match="changed after validation"):
            if replace:
                image.import_os9(index, os9_volume)
            else:
                image.create_partition("OS9", os9_path=os9_volume)
        assert card.read_bytes() == before  # includes table, padding and defaults


def test_disk_reader_rejects_growth_with_a_bounded_read(tmp_path, monkeypatch):
    import drivepak_manager.image as module
    source = tmp_path / "growing.dsk"
    source.write_bytes(b"x" * 161280)
    real_read = module.read_exact
    lengths = []
    def grow_before_read(f, length):
        lengths.append(length)
        with source.open("ab", buffering=0) as other:
            other.write(b"y" * 256)
        return real_read(f, length)
    monkeypatch.setattr(module, "read_exact", grow_before_read)
    with pytest.raises(SafetyError, match="changed while reading"):
        read_disk_file(source)
    assert lengths == [161280]


def test_os9_bad_extent_segments_names(os9_volume):
    b = bytearray(os9_volume.read_bytes())
    b[:3] = b"\xff\xff\xff"
    os9_volume.write_bytes(b)
    with RawVolume(os9_volume) as raw, pytest.raises(FormatError, match="declares"):
        OS9(raw)


def test_creation_preserves_unassigned_default_of_existing_bank(tmp_path):
    p = tmp_path / "blank.img"
    create_blank(p, 16 * 1024**2, 1)
    with Image(p, True) as image:
        image.set_default("DOS", None)
        image.create_partition("EXTRA", 1)
        assert image.layout().defaults["DOS"] == 0


def test_zero_length_partition_and_ff_extension_are_not_repaired(card):
    b = bytearray(card.read_bytes())
    b[3 * 512] = 1
    b[3 * 512 + 32:3 * 512 + 38] = bytes(6)
    card.write_bytes(b)
    with Image(card, True) as image:
        assert image.layout().editable
        image.rename_partition(0, "ZERO")
        with pytest.raises(FormatError, match="extent"):
            image.volume(0)
    b = bytearray(card.read_bytes())
    b[16] = 255
    card.write_bytes(b)
    with Image(card) as image:
        assert not image.layout().editable


def test_os9_bad_segments_and_names_fail(os9_volume):
    original = os9_volume.read_bytes()
    b = bytearray(original)
    b[5 * 256 + 16:5 * 256 + 19] = b"\0\0\x1f"
    b[5 * 256 + 19:5 * 256 + 21] = b"\0\x02"
    os9_volume.write_bytes(b)
    with RawVolume(os9_volume) as raw:
        with pytest.raises(FormatError, match="segment"):
            OS9(raw).list_dir()
    b = bytearray(original)
    b[4 * 256 + 64:4 * 256 + 64 + 29] = b"A" * 29
    os9_volume.write_bytes(b)
    with RawVolume(os9_volume) as raw:
        with pytest.raises(FormatError, match="terminator"):
            OS9(raw).list_dir()


def test_import_formats_rejected(tmp_path):
    for content in (b"abc" * 5, b"dk" + bytes(161278), b"HXCPICFE" + bytes(161272)):
        p = tmp_path / "bad.dsk"
        p.write_bytes(content)
        with pytest.raises(FormatError):
            read_disk_file(p)


def test_os9_default_in_floppy_bank_allows_import_without_changing_reference(legacy_mixed_card, tmp_path):
    image_path = legacy_mixed_card
    start = (1024 + 2880 + 31 * 2880) * 512
    end = start + 2880 * 512
    def unrelated_digest():
        h = hashlib.sha256()
        with image_path.open("rb") as f:
            for first, last in ((0, start), (end, image_path.stat().st_size)):
                f.seek(first)
                remaining = last - first
                while remaining:
                    b = f.read(min(65536, remaining))
                    assert b
                    h.update(b)
                    remaining -= len(b)
        return h.hexdigest()
    untouched = unrelated_digest()
    source = tmp_path / "game.dsk"
    data = b"\x7d" * 161280
    source.write_bytes(data)
    with Image(image_path, True) as image:
        layout = image.layout()
        assert layout.editable and not layout.errors
        assert any("Default U3" in w for w in layout.warnings)
        assert layout.defaults["U3"] == 1024 and layout.partition(0).kind == 2
        assert OS9(image.volume(0)).list_dir()
        before = image.read(start, 2880 * 512)
        image.import_disks([(1, 31, source)], "zero")
        after = image.read(start, 2880 * 512)
        assert image.volume(1, 31).read(0, len(data)) == data
        assert image.volume(1, 31).read(len(data), 256) == bytes(256)
        assert image.layout().defaults["U3"] == 1024
        assert OS9(image.volume(0)).list_dir()
        assert all(before[n * 512 + 256:(n + 1) * 512] == after[n * 512 + 256:(n + 1) * 512] for n in range(2880))
    assert unrelated_digest() == untouched


@pytest.mark.parametrize("count", [0, 1, 127, 128, 129, 1023, 1024])
def test_table_count_limits_and_name_is_not_magic(tmp_path, count):
    path = tmp_path / "table.img"
    b = bytearray((count + 1026) * 512)
    b[:8] = b"CUSTOM!!"
    b[17:20] = b"\0\0\x01"
    for n in range(count):
        b[(n + 1) * 512] = 1  # zero-length unknown entry; retains its table index
    b[(count + 1) * 512] = 128
    path.write_bytes(b)
    with Image(path) as image:
        l = image.layout()
        assert l.name == "CUSTOM!!" and len(l.partitions) == min(count, 1023)
        assert l.editable == (count <= 1023)
