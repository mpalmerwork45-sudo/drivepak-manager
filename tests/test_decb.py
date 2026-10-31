import json
import pytest

from drivepak_manager.errors import FormatError
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
