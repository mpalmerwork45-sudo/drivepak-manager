import time

import pytest

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog, QDialog

from drivepak_manager.gui import MainWindow, MappingDialog
from drivepak_manager.image import Session


def settle(qtbot, window):
    qtbot.waitUntil(lambda: window.worker is None, timeout=15000)


def test_open_save_browse_import_export_and_no_freeze(qtbot, card, tmp_path, monkeypatch):
    window = MainWindow()
    qtbot.addWidget(window)
    assert str(tmp_path / "preferences") in window.settings.fileName()
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


def test_card_selector_blocks_empty_reader_until_refreshed_and_reselected(qtbot, monkeypatch):
    from dataclasses import replace
    from PySide6.QtWidgets import QDialogButtonBox
    import drivepak_manager.gui as gui
    from drivepak_manager.devices import Device
    empty = Device("/dev/empty-reader-slot", 0, "USB microSD reader", "READER", "", "usb", "240:1", "/sys/simulated", "1", False, True)
    monkeypatch.setattr(gui, "discover", lambda: [empty])
    dialog = gui.DeviceDialog("Test card selector")
    qtbot.addWidget(dialog)
    dialog.show()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.table.item(0, 0).text() == empty.path
    assert dialog.table.horizontalHeaderItem(0).text() == "Path"
    dialog.table.setCurrentCell(0, 0)
    assert "0 bytes" in dialog.table.item(0, 4).text()
    assert "No readable card" in dialog.reason.text()
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    assert not dialog.unmount_button.isEnabled()
    dialog.accept()
    assert dialog.isVisible() and dialog.result() == QDialog.DialogCode.Rejected
    loaded = replace(empty, capacity=32 * 1024**2)
    monkeypatch.setattr(gui, "discover", lambda: [loaded])
    dialog.refresh()
    qtbot.waitUntil(lambda: dialog.worker is None)
    assert dialog.selected() is None
    assert not dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.table.setCurrentCell(0, 0)
    assert dialog.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
    dialog.accept()
    assert dialog.result() == QDialog.DialogCode.Accepted


def test_gui_progress_handles_larger_than_four_gigabytes(qtbot):
    window = MainWindow()
    qtbot.addWidget(window)
    window.run_task("Large count", lambda p, c: p(5 * 1024**3, 6 * 1024**3, "Synthetic progress"))
    settle(qtbot, window)
    assert window.progress.value() == 833
    window.close()


def test_busy_gui_does_not_read_worker_image_metadata(qtbot, card, tmp_path, monkeypatch):
    import threading
    window = MainWindow()
    qtbot.addWidget(window)
    window.session = Session.open(card, tmp_path / "work")
    release = threading.Event()
    layout = window.session.image.layout
    monkeypatch.setattr(window.session.image, "layout", lambda: pytest.fail("Busy GUI accessed shared worker image"))
    try:
        window.run_task("Waiting", lambda p, c: release.wait(5))
        window.update_actions()
        assert window.worker and not window.actions["import_disk"].isEnabled()
    finally:
        monkeypatch.setattr(window.session.image, "layout", layout)
        release.set()
        settle(qtbot, window)
    window.close()


def test_flushing_card_is_indeterminate_and_cancel_waits_for_io(qtbot, tmp_path, monkeypatch):
    import os
    import threading
    from drivepak_manager.backup import SimulatedDevice
    target, source = tmp_path / "simulated-card.bin", tmp_path / "source.img"
    target.write_bytes(b"\x38" * 4096)
    source.write_bytes(b"\x79" * 512)
    sim = SimulatedDevice(target)
    key = (target.stat().st_dev, target.stat().st_ino)
    entered, release = threading.Event(), threading.Event()
    real_sync = os.fsync
    def slow_sync(fd):
        s = os.fstat(fd)
        if (s.st_dev, s.st_ino) == key and not entered.is_set():
            entered.set()
            if not release.wait(10):
                raise OSError("Simulated flush not released by test")
        return real_sync(fd)
    monkeypatch.setattr(os, "fsync", slow_sync)
    window = MainWindow()
    qtbot.addWidget(window)
    window.show()
    errors, results, beats = [], [], []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(1))
    timer.start(5)
    try:
        window.run_task("Writing simulated card", lambda p, c: sim.write_direct(source, p, c, acknowledge_no_backup=True), results.append, errors.append)
        qtbot.waitUntil(lambda: entered.is_set() and window.progress.maximum() == 0 and "Flushing card" in window.statusBar().currentMessage())
        assert window.worker and not window.actions["write"].isEnabled()
        qtbot.waitUntil(lambda: len(beats) >= 3)
        window.cancel_operation()
        assert window.worker and not window.cancel_button.isEnabled()
    finally:
        release.set()
        settle(qtbot, window)
        timer.stop()
    assert not results and len(errors) == 1
    assert "PHYSICAL WRITE INCOMPLETE OR UNVERIFIED" in errors[0]
    assert source.read_bytes() == b"\x79" * 512
    assert target.read_bytes()[512:] == b"\x38" * 3584
    window.close()


def test_fast_chained_tasks_deliver_results_and_progress_on_gui_thread(qtbot, monkeypatch):
    import threading
    window = MainWindow()
    qtbot.addWidget(window)
    gui_thread = threading.get_ident()
    results, errors, progress_threads, worker_threads = [], [], [], []
    show_message = window.statusBar().showMessage
    def record_message(*args):
        progress_threads.append(threading.get_ident())
        return show_message(*args)
    monkeypatch.setattr(window.statusBar(), "showMessage", record_message)
    monkeypatch.setattr(window, "fail", errors.append)
    def start(number):
        def work(progress, cancel):
            worker_threads.append(threading.get_ident())
            progress(1, 1, "Fast task")
            return number
        def complete(result):
            assert threading.get_ident() == gui_thread
            assert result == number
            results.append(result)
            if number < 19:
                start(number + 1)
        window.run_task("Chained task", work, complete)
    start(0)
    qtbot.waitUntil(lambda: len(results) == 20 or bool(errors))
    settle(qtbot, window)
    assert not errors and results == list(range(20))
    assert progress_threads and set(progress_threads) == {gui_thread}
    assert all(thread != gui_thread for thread in worker_threads)
    window.close()


def test_import_tandd_slot_31_with_os9_default_in_a_floppy_bank(qtbot, legacy_mixed_card, tmp_path, monkeypatch):
    from drivepak_manager.filesystems.decb import blank_disk
    from drivepak_manager.streams import hash_file
    original_digest = hash_file(legacy_mixed_card)
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(window, "can_replace_session", lambda: True)
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.open_path(legacy_mixed_card)
    settle(qtbot, window)
    bank = window.tree.topLevelItem(1)
    assert "T&D" in bank.text(0) and bank.childCount() == 32
    window.tree.setCurrentItem(bank.child(31))
    settle(qtbot, window)
    assert window.current == (1, 31) and window.actions["import_disk"].isEnabled()
    assert 'DRIVE# "T&D" then DRIVE 0,#31' in window.breadcrumb.text()
    assert not window.layout_notice.isHidden() and "Default U3" in window.layout_notice.text()
    source = tmp_path / "game.dsk"
    payload = bytearray(blank_disk()[:161280])
    payload[:16] = b"ORIGINAL GAME!!!"
    source.write_bytes(payload)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **kw: ([str(source)], ""))
    def accept_mapping(dialog):
        assert dialog.mappings() == [(1, 31, str(source))]
        assert "T&D" in dialog.combos[0].currentText()
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(MappingDialog, "exec", accept_mapping)
    window.actions["import_disk"].trigger()
    settle(qtbot, window)
    assert not errors and window.session.changes
    assert window.session.image.volume(1, 31).read(0, len(payload)) == payload
    assert window.session.image.layout().defaults["U3"] == 1024
    assert hash_file(legacy_mixed_card) == original_digest
    window.close()


def test_structural_error_remains_visible_when_browsing_a_disk(qtbot, card, tmp_path, monkeypatch):
    b = bytearray(card.read_bytes())
    b[21:24] = b"\0\0\x07"  # default points outside every live partition start
    card.write_bytes(b)
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    window.open_path(card)
    settle(qtbot, window)
    window.tree.setCurrentItem(window.tree.topLevelItem(0).child(0))
    settle(qtbot, window)
    assert not window.actions["import_disk"].isEnabled()
    assert "Default DOS" in window.actions["import_disk"].toolTip()
    assert "Default DOS" in window.layout_notice.text()
    assert not window.layout_notice.isHidden()
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
    # The mutation refresh chains a separate directory-read worker. Wait for
    # its rendered outcome, rather than assuming the first idle signal is final.
    qtbot.waitUntil(lambda: bool(errors) or (window.files.item(0, 0) is not None and window.files.item(0, 0).text() == "RENAMED.BIN"), timeout=15000)
    assert not errors
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


def test_gui_os9_insert_replace_rename_delete(qtbot, card, os9_volume, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QCheckBox, QComboBox, QInputDialog, QLineEdit, QSpinBox
    from drivepak_manager.filesystems.os9 import OS9
    from drivepak_manager.image import Image
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_volume)
    original = card.read_bytes()
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(window, "can_replace_session", lambda: True)
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.open_path(card)
    settle(qtbot, window)
    item = next(window.tree.topLevelItem(row) for row in range(window.tree.topLevelItemCount())
                if tuple(window.tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)) == (index, None))
    window.tree.setCurrentItem(item)
    settle(qtbot, window)
    assert window.actions["insert"].isEnabled()
    assert "bytes free" in window.directory_status.text()
    source = tmp_path / "ADD.BIN"
    source.write_bytes(b"OS9 insert\x00\xff\r\n" * 20)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(source), ""))
    replacing = [False]
    def accept_insert(dialog):
        assert dialog.windowTitle() == "Insert OS-9 file"
        lines = [line for line in dialog.findChildren(QLineEdit) if not isinstance(line.parent(), QSpinBox)]
        lines[0].setText("ADDED")
        for checkbox in dialog.findChildren(QCheckBox):
            if checkbox.text().startswith("Replace"):
                checkbox.setChecked(replacing[0])
            if checkbox.text() == "Set owner explicitly" and not replacing[0]:
                checkbox.setChecked(True)
        if not replacing[0]:
            dialog.findChild(QComboBox).setCurrentIndex(2)  # executable preset
            next(spin for spin in dialog.findChildren(QSpinBox) if not spin.prefix()).setValue(0x4567)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(QDialog, "exec", accept_insert)
    window.actions["insert"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or (window.worker is None and window.files.rowCount() == 2), timeout=15000)
    assert not errors
    added = OS9(window.session.image.volume(index)).resolve("/ADDED")
    assert added.attributes == 0x3F and added.raw_fd[1:3] == b"\x45\x67"
    replacing[0] = True
    source.write_bytes(b"replacement without text conversion\x00\xff\r\n")
    window.actions["insert"].trigger()
    settle(qtbot, window)
    qtbot.waitUntil(lambda: window.worker is None and window.files.rowCount() == 2, timeout=15000)
    added = OS9(window.session.image.volume(index)).resolve("/ADDED")
    assert added.attributes == 0x3F and added.raw_fd[1:3] == b"\x45\x67"
    def select(name):
        row = next(row for row in range(window.files.rowCount()) if window.files.item(row, 0).text() == name)
        window.files.selectRow(row)
    select("ADDED")
    assert window.actions["rename_file"].isEnabled() and window.actions["delete_file"].isEnabled()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("RENAMED", True))
    window.actions["rename_file"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or (window.worker is None and any(window.files.item(row, 0).text() == "RENAMED" for row in range(window.files.rowCount()))), timeout=15000)
    assert not errors
    select("RENAMED")
    extracted = tmp_path / "os9-extract"
    extracted.mkdir()
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **kw: str(extracted))
    window.actions["extract"].trigger()
    settle(qtbot, window)
    assert (extracted / "RENAMED").read_bytes() == source.read_bytes()
    select("RENAMED")
    window.actions["delete_file"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or (window.worker is None and window.files.rowCount() == 1), timeout=15000)
    assert not errors
    assert window.files.item(0, 0).text() == "HELLO"
    assert [file.name for file in OS9(window.session.image.volume(index)).list_dir()] == ["HELLO"]
    assert window.session.changes and card.read_bytes() == original
    window.close()


def test_gui_os9_invalid_allocation_keeps_extraction_available(qtbot, card, os9_volume, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from drivepak_manager.image import Image
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_volume)
        with image.transaction():
            image.volume(index).write(256, bytes(4))
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    window.open_path(card)
    settle(qtbot, window)
    item = next(window.tree.topLevelItem(row) for row in range(window.tree.topLevelItemCount())
                if tuple(window.tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)) == (index, None))
    window.tree.setCurrentItem(item)
    settle(qtbot, window)
    assert window.files.item(0, 0).text() == "HELLO"
    assert not window.actions["insert"].isEnabled()
    assert "File editing disabled:" in window.directory_status.text()
    window.files.selectRow(0)
    assert window.actions["extract"].isEnabled()
    assert not window.actions["rename_file"].isEnabled() and not window.actions["delete_file"].isEnabled()
    window.close()


def test_gui_os9_subdirectory_editing_stays_in_directory(qtbot, card, os9_nested_volume, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QInputDialog
    from drivepak_manager.filesystems.os9 import OS9
    from drivepak_manager.image import Image
    with Image(card, True) as image:
        index = image.create_partition("OS9", os9_path=os9_nested_volume)
    before = card.read_bytes()
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    monkeypatch.setattr(window, "can_replace_session", lambda: True)
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.open_path(card)
    settle(qtbot, window)
    beats = []
    timer = QTimer()
    timer.timeout.connect(lambda: beats.append(1))
    timer.start(5)
    validate = OS9.validate_for_edit
    def slow_validation(*args, **kw):
        time.sleep(0.1)
        return validate(*args, **kw)
    monkeypatch.setattr(OS9, "validate_for_edit", slow_validation)
    item = next(window.tree.topLevelItem(row) for row in range(window.tree.topLevelItemCount())
                if tuple(window.tree.topLevelItem(row).data(0, Qt.ItemDataRole.UserRole)) == (index, None))
    window.tree.setCurrentItem(item)
    settle(qtbot, window)
    assert len(beats) >= 4
    row = next(row for row in range(window.files.rowCount()) if window.files.item(row, 0).text() == "CMDS")
    window.files.selectRow(row)
    assert not window.actions["rename_file"].isEnabled()
    assert not window.actions["delete_file"].isEnabled()
    assert not window.actions["extract"].isEnabled()
    assert window.actions["insert"].isEnabled()
    window.file_activated(window.files.item(row, 0))
    settle(qtbot, window)
    assert window.os9_path == "/CMDS" and window.files.rowCount() == 0
    source = tmp_path / "PROGRAM"
    source.write_bytes(b"\x00\xff\r\n" * 25)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(source), ""))
    monkeypatch.setattr(QDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    window.actions["insert"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or (window.worker is None and window.files.rowCount() == 1), timeout=15000)
    assert not errors and window.os9_path == "/CMDS"
    assert window.files.item(0, 0).text() == "PROGRAM"
    window.files.selectRow(0)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **kw: ("UPDATED", True))
    window.actions["rename_file"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or (window.worker is None and window.files.item(0, 0) is not None and window.files.item(0, 0).text() == "UPDATED"), timeout=15000)
    assert not errors and window.os9_path == "/CMDS"
    window.files.selectRow(0)
    window.actions["delete_file"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or (window.worker is None and window.files.rowCount() == 0), timeout=15000)
    assert not errors and window.os9_path == "/CMDS"
    assert OS9(window.session.image.volume(index)).list_dir("/CMDS") == []
    assert card.read_bytes() == before
    timer.stop()
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
    monkeypatch.setattr(window, "backup_destination", lambda device, **kw: str(next(destinations)))
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


def test_direct_file_to_card_has_no_save_dialog_and_keeps_source(qtbot, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from drivepak_manager import gui
    from drivepak_manager.backup import SimulatedDevice
    source = tmp_path / "NEWIMAGE.img"
    source.write_bytes(b"\x47" * 512)
    target = tmp_path / "simulated-card.bin"
    before = b"\x19" * 4096
    target.write_bytes(before)
    sim = SimulatedDevice(target)
    device = SimpleNamespace(capacity=sim.capacity, model="Simulated USB reader", path="/dev/SIMULATED", serial="SIM-001", diskseq="1", wwn="")
    window = MainWindow()
    qtbot.addWidget(window)
    assert window.session is None and window.actions["write"].isEnabled()
    monkeypatch.setattr(window, "choose_device", lambda title: device)
    monkeypatch.setattr(window, "backup_destination", lambda *a, **kw: pytest.fail("Direct write asked to save a backup"))
    monkeypatch.setattr(gui, "backup_card", lambda *a, **kw: pytest.fail("Direct write started a backup"))
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **kw: pytest.fail("Direct write opened a Save dialog"))
    def choose_options(dialog):
        assert dialog.backup_first.isChecked()
        dialog.source.setText(str(source))
        dialog.backup_first.setChecked(False)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(gui.WriteOptionsDialog, "exec", choose_options)
    confirmations = []
    def confirm(dialog):
        if dialog.windowTitle().startswith("Final confirmation"):
            body = dialog.findChild(gui.QPlainTextEdit).toPlainText()
            assert str(source) in body and device.path in body
            assert "NO RECOVERY BACKUP WILL BE CREATED" in body
            confirmations.append(body)
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(gui.TextDialog, "exec", confirm)
    monkeypatch.setattr(gui, "write_card_direct", lambda d, source, progress, cancel, **kw: sim.write_direct(source, progress, cancel, **kw))
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.actions["write"].trigger()
    settle(qtbot, window)
    assert not errors and len(confirmations) == 1
    assert target.read_bytes() == source.read_bytes() + before[512:]
    assert source.read_bytes() == b"\x47" * 512
    assert "readback verified" in window.state_label.text()
    assert not list(tmp_path.glob("*.json"))
    window.close()


def test_gui_applies_and_recovers_image_edits_without_full_card_write(qtbot, card, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from drivepak_manager import gui, devices
    from drivepak_manager.backup import SimulatedDevice
    from drivepak_manager.filesystems.decb import DECB
    from drivepak_manager.image import Image
    sim = SimulatedDevice(card)
    before = card.read_bytes()
    original = tmp_path / "source-image.img"
    original.write_bytes(before)
    device = SimpleNamespace(identity=sim.identity, path="/dev/SIMULATED", model="Simulated reader", capacity=sim.capacity, serial="TEST", wwn="", diskseq="1")
    window = MainWindow()
    qtbot.addWidget(window)
    window.settings.setValue("work_dir", str(tmp_path / "work"))
    window.session = Session.open(original, tmp_path / "work")
    window.refresh()
    source = tmp_path / "GAME.BIN"
    source.write_bytes(b"\x04\xff\x00\x0d" * 100)
    DECB(window.session.image.volume(1, 0, editing=True)).insert(source)
    window.edited("Inserted GAME.BIN")
    monkeypatch.setattr(window, "choose_device", lambda title: device)
    monkeypatch.setattr(devices, "validate", lambda *a, **kw: device)
    monkeypatch.setattr(gui.TextDialog, "exec", lambda self: QDialog.DialogCode.Accepted)
    journal_paths = []
    def write(d, journal, receipt, progress, cancel, **kw):
        journal_paths.append(journal)
        return sim.apply_changes(journal, receipt, progress, cancel, **kw)
    monkeypatch.setattr(gui, "apply_card_changes", write)
    monkeypatch.setattr(gui, "write_card", lambda *a, **kw: pytest.fail("Edits attempted a full-card write"))
    errors = []
    monkeypatch.setattr(window, "fail", errors.append)
    window.actions["apply"].trigger()
    qtbot.waitUntil(lambda: bool(errors) or window.written_status.startswith("Card edits readback verified"), timeout=15000)
    settle(qtbot, window)
    assert not errors and not window.session.changes
    assert window.session.original != card and window.session.original.read_bytes() == card.read_bytes()
    with Image(card) as image:
        assert DECB(image.volume(1, 0)).files[0].name == "GAME.BIN"
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **kw: (str(journal_paths[0]), ""))
    window.actions["recover_edits"].trigger()
    settle(qtbot, window)
    assert not errors and card.read_bytes() == before
    assert original.read_bytes() == before
    assert "recovery readback verified" in window.written_status
    window.close()
