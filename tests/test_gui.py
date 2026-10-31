import time

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QDialog

from drivepak_manager.gui import MainWindow, MappingDialog
from drivepak_manager.image import Session


def settle(qtbot, window):
    qtbot.waitUntil(lambda: window.worker is None, timeout=15000)


def test_open_save_browse_import_export_and_no_freeze(qtbot, card, tmp_path, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.show()
    beats = []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(1))
    timer.start(5)
    original_open = Session.open

    def slow_open(*args, **kwargs):
        time.sleep(0.15)
        return original_open(*args, **kwargs)
    monkeypatch.setattr(Session, "open", slow_open)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *args, **kw: (str(card), ""))
    window.actions["open"].trigger()
    settle(qtbot, window)
    assert len(beats) >= 5 and not errors
    assert window.session and window.session.original == card
    assert window.tree.topLevelItemCount() == 1
    disk = window.tree.topLevelItem(0).child(0)
    window.tree.setCurrentItem(disk)
    settle(qtbot, window)
    assert window.current == (1, 0)
    assert window.actions["insert"].isEnabled()
    source = tmp_path / "import.dsk"
    source.write_bytes(b"\x37" * 161280)
    mapping = MappingDialog(window.session.image, [str(source)], (1, 1), window)
    assert mapping.mappings()[0][:2] == (1, 1)
    assert mapping.tail.currentData() == "zero"
    mapping.close()
    # Trigger the real import action; only native dialogs are substituted.
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *args, **kw: ([str(source)], ""))
    monkeypatch.setattr(MappingDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window.actions["import_disk"].trigger()
    settle(qtbot, window)
    # refresh can start an asynchronous directory read as its second operation.
    settle(qtbot, window)
    assert window.session.changes
    saved = tmp_path / "saved.img"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kw: (str(saved), ""))
    window.actions["save"].trigger()
    settle(qtbot, window)
    assert saved.exists() and card.read_bytes() != saved.read_bytes()
    output = tmp_path / "export.dsk"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kw: (str(output), ""))
    monkeypatch.setattr(window, "export_size", lambda: 161280)
    window.actions["export_disk"].trigger()
    settle(qtbot, window)
    assert output.read_bytes() == source.read_bytes()
    # The deliberately non-filesystem raw import produces an actionable browse
    # error, while raw export/save continue working.
    assert not errors
    assert "Raw disk/partition export" in window.directory_status.text()
    assert not window.actions["insert"].isEnabled()
    timer.stop()
    window.close()


def test_worker_error_and_cancel_leave_ui_available(qtbot, tmp_path, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    def waiting(progress, cancel):
        while not cancel.is_set():
            time.sleep(0.01)
        raise ValueError("Injected cancellation result")
    window.run_task("Cancellable test", waiting)
    assert not window.actions["open"].isEnabled()
    window.cancel_operation()
    settle(qtbot, window)
    assert errors == ["Injected cancellation result"]
    assert window.actions["open"].isEnabled()
    window.close()


def test_gui_progress_handles_larger_than_four_gigabytes(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.run_task("Large count", lambda p, c: p(5 * 1024**3, 6 * 1024**3, "Synthetic progress"))
    settle(qtbot, window)
    assert window.progress.value() == 833
    window.close()


def test_gui_file_partition_and_os9_workflows(qtbot, tmp_path, os9_volume, monkeypatch):
    from PySide6.QtWidgets import QInputDialog, QLineEdit, QSpinBox
    from drivepak_manager.image import create_blank
    original = tmp_path / "blank.img"
    create_blank(original, 16 * 1024**2, 1)
    before = original.read_bytes()
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(window, "can_replace_session", lambda: True)
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.open_path(original)
    settle(qtbot, window)
    window.tree.setCurrentItem(window.tree.topLevelItem(0).child(0))
    settle(qtbot, window)
    source = tmp_path / "TEST.BIN"
    source.write_bytes(b"binary\x00\xff\r\ncontent")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(source), ""))
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window.actions["insert"].trigger()
    settle(qtbot, window)
    assert window.files.rowCount() == 1
    window.files.selectRow(0)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("RENAMED.BIN", True))
    window.actions["rename_file"].trigger()
    settle(qtbot, window)
    assert window.files.item(0, 0).text() == "RENAMED.BIN"
    window.files.selectRow(0)
    extract_dir = tmp_path / "extract"
    extract_dir.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **kw: str(extract_dir))
    window.actions["extract"].trigger()
    settle(qtbot, window)
    assert (extract_dir / "RENAMED.BIN").read_bytes() == source.read_bytes()
    window.files.selectRow(0)
    window.actions["delete_file"].trigger()
    settle(qtbot, window)
    assert window.files.rowCount() == 0

    def accept_one_slot(dialog):
        for spin in dialog.findChildren(QSpinBox):
            spin.setValue(1)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", accept_one_slot)
    window.actions["create_part"].trigger()
    settle(qtbot, window)
    assert len(window.session.image.layout().partitions) == 2
    window.tree.setCurrentItem(window.tree.topLevelItem(1))
    settle(qtbot, window)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("NEWBANK", True))
    window.actions["rename_part"].trigger()
    settle(qtbot, window)
    assert window.session.image.layout().partition(1).name == "NEWBANK"
    window.actions["delete_part"].trigger()
    settle(qtbot, window)
    assert len(window.session.image.layout().partitions) == 1
    # Add OS-9 via the same connected partition dialog.
    def accept_os9(dialog):
        lines = [line for line in dialog.findChildren(QLineEdit) if not isinstance(line.parent(), QSpinBox)]
        lines[0].setText("OS9")
        lines[1].setText(str(os9_volume))
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", accept_os9)
    window.actions["create_part"].trigger()
    settle(qtbot, window)
    window.tree.setCurrentItem(window.tree.topLevelItem(1))
    settle(qtbot, window)
    assert window.files.item(0, 0).text() == "HELLO"
    window.files.selectRow(0)
    window.actions["extract"].trigger()
    settle(qtbot, window)
    assert (extract_dir / "HELLO").read_bytes() == b"OS9\x00\xff\r\nBINARY"
    assert not errors and original.read_bytes() == before
    window.close()


def test_gui_backup_edit_write_full_workflow_with_simulated_card(qtbot, card, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import drivepak_manager.gui as gui
    from drivepak_manager.backup import SimulatedDevice, verify_backup
    from drivepak_manager.filesystems.decb import DECB
    sim = SimulatedDevice(card)
    before = card.read_bytes()
    device = SimpleNamespace(capacity=sim.capacity, model="Simulated USB reader", path="/dev/SIMULATED", serial="SIM-001", diskseq="1", wwn="")
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(window, "can_replace_session", lambda: True)
    monkeypatch.setattr(window, "choose_device", lambda title: device)
    backups = [tmp_path / "initial.img", tmp_path / "current-before-write.img"]
    destinations = iter(backups)
    monkeypatch.setattr(window, "backup_destination", lambda device: str(next(destinations)))
    monkeypatch.setattr(gui, "backup_card", lambda d, out, work, progress, cancel: sim.backup(out, progress, cancel))
    monkeypatch.setattr(gui, "write_card", lambda d, source, backup, progress, cancel: sim.write(source, backup, progress, cancel))
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.actions["backup"].trigger()
    settle(qtbot, window)
    assert window.session and verify_backup(backups[0])
    source = tmp_path / "GAME.BIN"
    source.write_bytes(bytes(range(256)))
    DECB(window.session.image.volume(1, 0)).insert(source)
    window.edited("Inserted GAME.BIN")
    window.actions["write"].trigger()
    settle(qtbot, window)
    assert "readback verified" in window.written_status
    assert card.read_bytes() == window.session.working.read_bytes()
    assert all(b.read_bytes() == before for b in backups)
    assert verify_backup(backups[0]) and verify_backup(backups[1]) and not errors
    window.close()
