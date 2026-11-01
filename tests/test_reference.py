"""Opt-in integration: immutable public reference, disposable streamed copies."""
import os
from pathlib import Path

import pytest

from drivepak_manager.image import Image, Session
from drivepak_manager.filesystems.decb import DECB
from drivepak_manager.filesystems.os9 import OS9
from drivepak_manager.errors import FormatError
from drivepak_manager.streams import hash_file

pytestmark = pytest.mark.reference
EXPECTED = "8f46cf246b6b625efc171b3e25b805bdca89496c2a34b8491fcf4600dd8cf2c2"


@pytest.fixture(scope="module")
def reference():
    if os.environ.get("DRIVEPAK_REFERENCE") != "1":
        pytest.skip("Set DRIVEPAK_REFERENCE=1 after tools/download_references.py")
    root = Path(__file__).resolve().parents[1]
    path = root / "data/reference/card-2gb/DrivePak 2GB Image/drivepak-2gb.img"
    assert path.stat().st_size == 1866210304
    assert hash_file(path) == EXPECTED
    return path


def test_reference_unchanged_open_and_save(reference, tmp_path):
    s = Session.open(reference, tmp_path / "work")
    output = tmp_path / "unchanged.img"
    assert s.save_as(output) == EXPECTED
    assert hash_file(output) == EXPECTED
    s.close()


def test_reference_offsets_roundtrip_file_edit_and_every_unrelated_byte(reference, tmp_path):
    session = Session.open(reference, tmp_path / "work")
    image = session.image
    assert image.layout().editable and len(image.layout().partitions) == 65
    for disk, expected in ((0, "DPX.BAS"), (1, "ASTRO.BIN"), (2, "BIRDS.BIN")):
        fs = DECB(image.volume(0, disk))
        assert expected in [f.name for f in fs.files]
        # Literal independently derived directory address, not engine constants.
        offset = (1024 + disk * 2880 + 17 * 18 + 2) * 512
        assert image.read(offset, 8).decode().rstrip() == expected.split(".")[0]
    raw = tmp_path / "full-slot.dsk"
    image.export_disk(0, 2, raw)
    image.import_disks([(0, 2, raw)], "preserve")
    assert hash_file(session.working) == EXPECTED
    DECB(image.volume(0, 0)).rename("DPX.BAS", "MANAGER.BAS")
    allowed_start = (1024 + 17 * 18 + 2) * 512
    with reference.open("rb") as a, session.working.open("rb") as b:
        pos = 0
        while True:
            one, two = a.read(4 * 1024**2), b.read(4 * 1024**2)
            if not one:
                assert not two
                break
            if pos <= allowed_start < pos + len(one):
                n = allowed_start - pos
                assert one[:n] == two[:n] and one[n + 11:] == two[n + 11:]
                assert two[n:n + 11] == b"MANAGER BAS"
            else:
                assert one == two
            pos += len(one)
    assert OS9(image.volume(3)).list_dir()
    with pytest.raises(FormatError, match="declares"):
        OS9(image.volume(36))
    session.close()
    assert hash_file(reference) == EXPECTED


def test_reference_changed_sector_apply_and_recovery(reference, tmp_path):
    """Full-size actual layout, but the 'card' is strictly a disposable file."""
    import struct
    from drivepak_manager.backup import SimulatedDevice
    from drivepak_manager.patches import HEADER, prepare_changes
    from drivepak_manager.streams import copy_verified
    session = Session.open(reference, tmp_path / "work")
    try:
        DECB(session.image.volume(0, 0)).rename("DPX.BAS", "MANAGER.BAS")
        target, journal = tmp_path / "simulated-card.bin", tmp_path / "changes.dpp"
        copy_verified(reference, target)
        prepared = prepare_changes(reference, session.working, journal, expected_original=EXPECTED)
        # This one independently calculated directory sector is the entire write
        # set, including its preserved 256-byte physical padding.
        assert prepared["sectors"] == 1 and prepared["bytes_changed"] == 512
        with journal.open("rb") as records:
            records.seek(HEADER.size)
            assert struct.unpack(">Q", records.read(8))[0] == (1024 + 308) * 512
        card = SimulatedDevice(target)
        result = card.apply_changes(journal, str(journal) + ".json", expected_journal=prepared["journal_sha256"])
        assert result["status"] == "verified" and result["bytes_written"] == 512
        assert hash_file(target) == hash_file(session.working) == prepared["modified_sha256"]
        assert card.apply_changes(journal, tmp_path / "recovered.json", recovery=True)["status"] == "verified"
        assert hash_file(target) == hash_file(reference) == EXPECTED
    finally:
        session.close()
