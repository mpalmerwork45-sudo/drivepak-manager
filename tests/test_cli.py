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
