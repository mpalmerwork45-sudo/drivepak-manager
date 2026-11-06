import pytest

from drivepak_manager.cli import main


def test_cli_real_image_workflow(tmp_path, capsys):
    blank = tmp_path / "blank.img"
    assert main(["new", str(blank), "--capacity-mib", "4", "--disks", "1"]) == 0
    assert main(["inspect", str(blank)]) == 0
    source = tmp_path / "GAME.BIN"
    source.write_bytes(bytes(range(256)))
    edited = tmp_path / "edited.img"
    assert main(["insert", str(blank), str(edited), "--partition", "0", "--disk", "0", str(source)]) == 0
    assert main(["files", str(edited), "--partition", "0", "--disk", "0"]) == 0
    extracted = tmp_path / "extracted.bin"
    assert main(["extract", str(edited), "--partition", "0", "--disk", "0", "GAME.BIN", str(extracted)]) == 0
    assert extracted.read_bytes() == source.read_bytes()
    assert main(["copy", str(blank), str(blank)]) == 2


def test_cli_invalid_os9_export_requires_diagnostic_acknowledgement(card, os9_volume, tmp_path, capsys):
    from drivepak_manager.image import Image
    with Image(card, True) as image:
        idx = image.create_partition("OS9", os9_path=os9_volume)
        with image.transaction():
            image.volume(idx).write(0, b"\0\0\x21")
    output = tmp_path / "diagnostic.os9"
    args = ["volume-export", str(card), str(output), "--partition", str(idx)]
    assert main(args) == 2 and not output.exists()
    assert main(args + ["--diagnostic"]) == 0
    assert output.stat().st_size == 32 * 256


def test_cli_explicit_direct_write_to_simulated_device(tmp_path, capsys):
    import json
    target, source = tmp_path / "simulated-device", tmp_path / "NEWIMAGE.img"
    target.write_bytes(b"\x27" * 4096)
    source.write_bytes(b"\x46" * 512)
    assert main(["simulate-write", str(target), str(source), "--no-backup"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "verified" and result["backup_policy"] == "none-explicit-direct-write"
    assert target.read_bytes() == source.read_bytes() + b"\x27" * 3584


def test_cli_image_edit_journal_apply_and_recovery(tmp_path, capsys):
    original, modified, target, journal = [tmp_path / n for n in ("original.img", "modified.img", "card.bin", "edits.dpp")]
    original.write_bytes(b"\x81" * 4096)
    b = bytearray(original.read_bytes())
    b[1025] = 0x12
    modified.write_bytes(b)
    target.write_bytes(original.read_bytes())
    assert main(["prepare-edits", str(original), str(modified), str(journal)]) == 0
    assert main(["simulate-apply-edits", str(target), str(journal), "--receipt", str(journal) + ".json"]) == 0
    assert target.read_bytes() == modified.read_bytes()
    assert main(["simulate-apply-edits", str(target), str(journal), "--receipt", str(tmp_path / "recovered.json"), "--recover"]) == 0
    assert target.read_bytes() == original.read_bytes()


def test_cli_os9_insert_replace_rename_delete(card, os9_volume, tmp_path, capsys):
    from drivepak_manager.filesystems.os9 import OS9
    from drivepak_manager.image import Image
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_volume)
    original = card.read_bytes()
    source = tmp_path / "new.bin"
    source.write_bytes(bytes(range(256)) * 2 + b"\x00\xff\r\n")
    inserted = tmp_path / "inserted.img"
    assert main(["insert", str(card), str(inserted), "--partition", str(index), str(source),
                 "--name", "NewFile", "--attributes", "0x2d", "--owner", "0x4567"]) == 0
    with Image(inserted) as image:
        file = OS9(image.volume(index)).resolve("/NewFile")
        assert file.attributes == 0x2D and file.raw_fd[1:3] == b"\x45\x67"
    extracted = tmp_path / "extracted.bin"
    assert main(["extract", str(inserted), "--partition", str(index), "/NewFile", str(extracted)]) == 0
    assert extracted.read_bytes() == source.read_bytes()
    source.write_bytes(b"replacement\x00\xff\r\n" * 43)
    replaced = tmp_path / "replaced.img"
    assert main(["insert", str(inserted), str(replaced), "--partition", str(index), str(source),
                 "--name", "NewFile", "--replace"]) == 0
    with Image(replaced) as image:
        fs = OS9(image.volume(index))
        file = fs.resolve("/NewFile")
        assert b"".join(fs.chunks(file)) == source.read_bytes()
        assert file.attributes == 0x2D and file.raw_fd[1:3] == b"\x45\x67"
    renamed = tmp_path / "renamed.img"
    assert main(["rename-file", str(replaced), str(renamed), "--partition", str(index), "/NewFile", "Renamed"]) == 0
    deleted = tmp_path / "deleted.img"
    assert main(["delete-file", str(renamed), str(deleted), "--partition", str(index), "/Renamed"]) == 0
    with Image(deleted) as image:
        assert [file.name for file in OS9(image.volume(index)).list_dir()] == ["HELLO"]
    assert card.read_bytes() == original


def test_cli_rejects_mixed_basic_and_os9_metadata_before_output(card, tmp_path, capsys):
    source = tmp_path / "FILE.BIN"
    source.write_bytes(b"data")
    output = tmp_path / "rejected.img"
    assert main(["insert", str(card), str(output), "--partition", "1", str(source), "--type", "2"]) == 2
    assert "Disk BASIC" in capsys.readouterr().err and not output.exists()
    assert main(["insert", str(card), str(output), "--partition", "1", "--disk", "0", str(source), "--attributes", "0x1b"]) == 2
    assert "OS-9" in capsys.readouterr().err and not output.exists()


def test_cli_full_write_binds_source_to_final_confirmation(tmp_path, monkeypatch, capsys):
    import builtins
    from types import SimpleNamespace
    import drivepak_manager.cli as cli
    import drivepak_manager.linux as linux
    source = tmp_path / "SOURCE.img"
    source.write_bytes(b"a" * 512)
    device = SimpleNamespace(path="/dev/simulated", capacity=4096, model="Simulated reader", serial="TEST", diskseq="1", identity={"simulated": True})
    monkeypatch.setattr(cli, "selected_device", lambda path: device)
    monkeypatch.setattr(linux, "validate", lambda *a, **kw: device)
    monkeypatch.setattr(linux, "invoke", lambda *a, **kw: pytest.fail("Changed reviewed source reached privileged access"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    def confirm(prompt):
        source.write_bytes(b"b" * 512)
        return device.path
    monkeypatch.setattr(builtins, "input", confirm)
    assert cli.main(["write-card", device.path, str(source), "--no-backup"]) == 2
    assert "source image changed after review" in capsys.readouterr().err


@pytest.mark.parametrize("size", [0, 513, 8192])
def test_cli_full_write_checks_reviewed_range_before_confirmation(tmp_path, monkeypatch, size):
    from types import SimpleNamespace
    import drivepak_manager.cli as cli
    source = tmp_path / "INVALID.img"
    source.write_bytes(bytes(size))
    device = SimpleNamespace(path="/dev/simulated", capacity=4096)
    monkeypatch.setattr(cli, "selected_device", lambda path: device)
    monkeypatch.setattr(cli, "write_card_direct", lambda *a, **kw: pytest.fail("Invalid source reached card writer"))
    assert cli.main(["write-card", device.path, str(source), "--no-backup", "--confirm-device", device.path]) == 2
