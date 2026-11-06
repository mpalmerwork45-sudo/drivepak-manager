import json
import pytest

from drivepak_manager.errors import FormatError, SafetyError
from drivepak_manager.image import Image
from drivepak_manager.filesystems.decb import DECB


def test_binary_file_insert_extract_rename_delete_preserves_unrelated(card, tmp_path):
    before = card.read_bytes()
    source = tmp_path / "GAME.BIN"
    content = (bytes(range(256)) * 35 * 9) + b"\x00\xff\r\n"
    source.write_bytes(content)
    with Image(card, True) as image:
        DECB(image.volume(1, 0)).insert(source, file_type=2, ascii_flag=0)
        fs = DECB(image.volume(1, 0))
        assert fs.get("GAME.BIN").size == len(content)
        assert fs.read_file("GAME.BIN") == content
        # Independent granule-34 address: directory track is skipped.
        assert image.read((1024 + 34 * 9 + 18) * 512, 256) == content[34 * 2304:34 * 2304 + 256]
        output = tmp_path / "EXTRACT.BIN"
        fs.extract("GAME.BIN", output)
        assert output.read_bytes() == content
        assert json.loads((tmp_path / "EXTRACT.BIN.drivepak.json").read_text())["file_type"] == 2
        after_insert = card.read_bytes()
        fs.rename("GAME.BIN", "NEW.BIN")
        after_rename = card.read_bytes()
        delta = {i for i, (a, b) in enumerate(zip(after_insert, after_rename)) if a != b}
        assert delta <= set(range((1024 + 308) * 512, (1024 + 308) * 512 + 11))
        DECB(image.volume(1, 0)).delete("NEW.BIN")
        assert DECB(image.volume(1, 0)).files == []
        DECB(image.volume(1, 1)).insert(output, name="COPY.BIN")
        assert DECB(image.volume(1, 1)).read_file("COPY.BIN") == content
    after = card.read_bytes()
    for offset in range(len(before) // 512):
        # Every physical padding half survives every file operation.
        assert before[offset * 512 + 256:(offset + 1) * 512] == after[offset * 512 + 256:(offset + 1) * 512]
    assert after[:1024 * 512] == before[:1024 * 512]


@pytest.mark.parametrize("length", [0, 1, 255, 256, 257, 2304, 2305, 68 * 2304])
def test_file_lengths_exact(card, tmp_path, length):
    p = tmp_path / "FILE.DAT"
    data = b"\xff" * length
    p.write_bytes(data)
    with Image(card, True) as image:
        DECB(image.volume(1, 0)).insert(p, file_type=1, ascii_flag=255)
        fs = DECB(image.volume(1, 0))
        assert fs.read_file("FILE.DAT") == data
        assert fs.get("FILE.DAT").size == length
        with pytest.raises(FormatError):
            fs.insert(p)


def test_deleted_directory_before_live_file_and_metadata(card, tmp_path):
    p = tmp_path / "ONE.BIN"
    p.write_bytes(b"1234")
    with Image(card, True) as image:
        DECB(image.volume(1, 0)).insert(p)
        DECB(image.volume(1, 0)).insert(p, name="TWO.BIN")
        DECB(image.volume(1, 0)).delete("ONE.BIN")
        assert [f.name for f in DECB(image.volume(1, 0)).files] == ["TWO.BIN"]
        DECB(image.volume(1, 0)).insert(p, name="THREE.BIN")
        assert {f.name for f in DECB(image.volume(1, 0)).files} == {"TWO.BIN", "THREE.BIN"}
        view = image.volume(1, 0)
        with image.transaction():
            view.write(308 * 256 + 16, b"unknownmetadata!")
        DECB(view).rename("THREE.BIN", "EDIT.BIN")
        assert view.read(308 * 256 + 16, 16) == b"unknownmetadata!"


def test_corruption_cyclic_orphan_crosslink_and_disk_full(card, tmp_path):
    p = tmp_path / "A.BIN"
    p.write_bytes(b"x" * (68 * 2304))
    with Image(card, True) as image:
        view = image.volume(1, 0)
        DECB(view).insert(p)
        p.write_bytes(b"y")
        before = card.read_bytes()
        with pytest.raises(FormatError, match="free granules"):
            DECB(view).insert(p, name="B.BIN")
        assert card.read_bytes() == before
        with image.transaction():
            view.write(307 * 256, b"\0")
        with pytest.raises(FormatError, match="cyclic"):
            DECB(view)


def test_orphan_disables_mutation_but_allows_browsing(card, tmp_path):
    p = tmp_path / "A.BIN"
    p.write_bytes(b"x")
    with Image(card, True) as image:
        view = image.volume(1, 0)
        with image.transaction():
            view.write(307 * 256 + 67, b"\xc1")
        fs = DECB(view)
        assert fs.files == [] and fs.issues
        with pytest.raises(FormatError, match="owner"):
            fs.insert(p)


def test_reused_filesystem_object_never_overwrites_new_files(card, tmp_path):
    first, second = tmp_path / "FIRST.BIN", tmp_path / "SECOND.BIN"
    first.write_bytes(b"first file\x00\xff")
    second.write_bytes(b"second file\r\n")
    with Image(card, True) as image:
        fs = DECB(image.volume(1, 0))
        fs.insert(first)
        fs.insert(second)
        current = DECB(image.volume(1, 0))
        assert {f.name for f in current.files} == {"FIRST.BIN", "SECOND.BIN"}
        assert current.read_file("FIRST.BIN") == first.read_bytes()
        assert current.read_file("SECOND.BIN") == second.read_bytes()
        assert {f.name for f in fs.files} == {"FIRST.BIN", "SECOND.BIN"}


@pytest.mark.parametrize("operation", ["rename", "delete"])
def test_stale_file_selection_cannot_mutate_a_replacement(card, tmp_path, operation):
    source = tmp_path / "FILE.BIN"
    source.write_bytes(b"payload")
    with Image(card, True) as image:
        view = image.volume(1, 0)
        DECB(view).insert(source, name="OLD.BIN")
        stale = DECB(view)
        DECB(view).delete("OLD.BIN")
        DECB(view).insert(source, name="NEW.BIN")
        before = card.read_bytes()
        with pytest.raises(FormatError, match="not found"):
            if operation == "rename":
                stale.rename("OLD.BIN", "WRONG.BIN")
            else:
                stale.delete("OLD.BIN")
        assert card.read_bytes() == before


def test_import_source_growth_is_bounded_and_rejected_before_edits(card, tmp_path, monkeypatch):
    import drivepak_manager.filesystems.decb as decb
    source = tmp_path / "GROW.BIN"
    source.write_bytes(b"small source")
    read = decb.read_exact
    requested = []
    def grow(file, length):
        requested.append(length)
        source.write_bytes(b"too big" * 100000)
        return read(file, length)
    with Image(card, True) as image:
        before = card.read_bytes()
        monkeypatch.setattr(decb, "read_exact", grow)
        with pytest.raises(SafetyError, match="source changed"):
            DECB(image.volume(1, 0)).insert(source)
        assert requested == [len(b"small source")]
        assert card.read_bytes() == before


def test_other_edit_during_import_planning_is_never_overwritten(card, tmp_path, monkeypatch):
    import drivepak_manager.filesystems.decb as decb
    first, other = tmp_path / "FIRST.BIN", tmp_path / "OTHER.BIN"
    first.write_bytes(b"first import")
    other.write_bytes(b"other edit")
    with Image(card, True) as image:
        view = image.volume(1, 0)
        read = decb.read_exact
        concurrent = []
        def edit_while_reading(file, length):
            data = read(file, length)
            if not concurrent:
                concurrent.append(True)
                DECB(view).insert(other)
                concurrent.append(card.read_bytes())
            return data
        monkeypatch.setattr(decb, "read_exact", edit_while_reading)
        with pytest.raises(SafetyError, match="changed while planning"):
            DECB(view).insert(first)
        assert card.read_bytes() == concurrent[1]
        fs = DECB(view)
        assert [file.name for file in fs.files] == ["OTHER.BIN"]
        assert fs.read_file("OTHER.BIN") == other.read_bytes()
