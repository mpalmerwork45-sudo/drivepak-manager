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
