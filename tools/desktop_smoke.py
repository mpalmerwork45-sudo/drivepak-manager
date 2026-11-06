#!/usr/bin/env python3
"""Launch actual Widgets, exercise browsing, save a screenshot; no device access."""
import argparse
import os
from pathlib import Path
import sys
import time
import subprocess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
args = argparse.ArgumentParser(description=__doc__)
args.add_argument("--offscreen", action="store_true")
args.add_argument("--keep-open", action="store_true")
args.add_argument("--write-options", action="store_true", help="inspect the source/backup dialog only; never selects or opens a card")
args.add_argument("--os9", action="store_true", help="inspect OS-9 browsing and insertion permissions using a generated volume; no card access")
args.add_argument("--installed-launcher", help="also check startup of this installed desktop entrypoint")
args.add_argument("--device-picker", action="store_true", help="inspect read-only device enumeration; never opens, backs up or unmounts a card")
options = args.parse_args()
if options.os9 and (options.device_picker or options.write_options):
    args.error("--os9 is a separate synthetic-image inspection; omit --device-picker and --write-options")
if options.offscreen:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = str(ROOT / "data/work/smoke-config")
os.environ["XDG_DATA_HOME"] = str(ROOT / "data/work/smoke-data")
if options.installed_launcher:
    process = subprocess.Popen([options.installed_launcher], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        time.sleep(2)
        if process.poll() is not None:
            raise SystemExit("Installed GUI failed: " + process.stderr.read().decode(errors="replace"))
        print("Installed desktop entrypoint started successfully:", options.installed_launcher, flush=True)
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        process.stderr.close()

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QComboBox, QDialog, QDialogButtonBox
from drivepak_manager.gui import MainWindow, DeviceDialog, WriteOptionsDialog
from drivepak_manager.image import Image, create_blank
from drivepak_manager.filesystems.decb import DECB

work = ROOT / "data/work"
work.mkdir(parents=True, exist_ok=True)
if options.device_picker:
    app = QApplication(["DrivePak Manager reader inspection"])
    picker = DeviceDialog("Select card to back up and open")
    picker.show()
    def inspect_picker():
        if picker.worker:
            return
        for row, device in enumerate(picker.devices):
            if device.capacity <= 0:
                picker.table.setCurrentCell(row, 0)
                assert not picker.buttons.button(QDialogButtonBox.StandardButton.Ok).isEnabled()
                assert "0 bytes" in picker.reason.text()
        assert picker.table.horizontalHeaderItem(0).text() == "Path"
        picker.grab().save(str(work / "reader-status.png"))
        print(f"Read-only device selector inspected: {len(picker.devices)} devices; zero-capacity readers unavailable.", flush=True)
        picker.reject()
        app.quit()
    timer = QTimer()
    timer.timeout.connect(inspect_picker)
    timer.start(100)
    QTimer.singleShot(30000, lambda: app.exit(1))
    raise SystemExit(app.exec())
image = work / ("os9-desktop-original.img" if options.os9 else "desktop-demo-original.img")
if options.os9 and not image.exists():
    # Explicit classic RBF bytes, independently generated; no downloaded data.
    raw = bytearray(32 * 256)
    raw[:3] = (32).to_bytes(3, "big")
    raw[3] = 18
    raw[4:6] = (4).to_bytes(2, "big")
    raw[6:8] = (1).to_bytes(2, "big")
    raw[8:11] = (3).to_bytes(3, "big")
    raw[31:35] = b"DEM\xcf"
    raw[256] = 0xFE  # sectors 0..6 allocated, MSB first
    raw[3 * 256] = 0xBF
    raw[3 * 256 + 8] = 1
    raw[3 * 256 + 9:3 * 256 + 13] = (96).to_bytes(4, "big")
    raw[3 * 256 + 16:3 * 256 + 19] = (4).to_bytes(3, "big")
    raw[3 * 256 + 19:3 * 256 + 21] = (1).to_bytes(2, "big")
    for n, (name, descriptor) in enumerate(((b"\xae", 3), (b".\xae", 3), (b"HELL\xcf", 5))):
        position = 4 * 256 + n * 32
        raw[position:position + len(name)] = name
        raw[position + 29:position + 32] = descriptor.to_bytes(3, "big")
    content = b"DrivePak Manager OS-9 transfer demonstration.\r\n"
    raw[5 * 256] = 0x1B
    raw[5 * 256 + 8] = 1
    raw[5 * 256 + 9:5 * 256 + 13] = len(content).to_bytes(4, "big")
    raw[5 * 256 + 16:5 * 256 + 19] = (6).to_bytes(3, "big")
    raw[5 * 256 + 19:5 * 256 + 21] = (1).to_bytes(2, "big")
    raw[6 * 256:6 * 256 + len(content)] = content
    volume = work / "os9-desktop-volume.os9"
    volume.write_bytes(raw)
    create_blank(image, 16 * 1024**2, 1)
    with Image(image, True) as source:
        source.create_partition("OS9-DEMO", os9_path=volume)
elif not image.exists():
    create_blank(image, 16 * 1024**2, 4)
    hello = work / "HELLO.BAS"
    hello.write_bytes(b'10 PRINT "HELLO FROM DRIVEPAK MANAGER"\r20 END\r')
    with Image(image, True) as source:
        DECB(source.volume(0, 0)).insert(hello, file_type=0, ascii_flag=255)
app = QApplication(["DrivePak Manager smoke inspection"])
window = MainWindow()
window.settings.setValue("work_dir", str(work / "desktop-work"))
window.show()
window.open_path(image)
start = time.monotonic()
state = [0]


def inspect():
    if time.monotonic() - start > 30 and state[0] < 2:
        print("Desktop smoke inspection timed out", flush=True)
        app.exit(1)
    if window.worker:
        return
    if state[0] == 0 and window.session:
        if options.os9:
            index = next(p.index for p in window.session.image.layout().partitions if p.kind == 3)
            node = next(window.tree.topLevelItem(n) for n in range(window.tree.topLevelItemCount())
                        if tuple(window.tree.topLevelItem(n).data(0, Qt.ItemDataRole.UserRole)) == (index, None))
            window.tree.setCurrentItem(node)
        else:
            window.tree.topLevelItem(0).setExpanded(True)
            window.tree.setCurrentItem(window.tree.topLevelItem(0).child(0))
        state[0] = 1
    elif state[0] == 1:
        assert window.files.rowCount() == 1
        assert window.files.item(0, 0).text() == ("HELLO" if options.os9 else "HELLO.BAS")
        assert window.actions["insert"].isEnabled()
        assert window.actions["write"].isEnabled()
        screenshot = work / ("os9-desktop.png" if options.os9 else "desktop.png")
        window.grab().save(str(screenshot))
        print(f"Desktop inspected: {app.platformName()}, screenshot {screenshot}", flush=True)
        state[0] = 2
        if options.os9:
            assert "bytes free" in window.directory_status.text()
            window.files.selectRow(0)
            assert window.actions["rename_file"].isEnabled()
            assert window.actions["delete_file"].isEnabled()
            candidate = work / "TRANSFER"
            candidate.write_bytes(b"Synthetic OS-9 insertion dialog inspection.\r\n")
            captured = []
            def inspect_os9_dialog():
                dialog = next(d for d in window.findChildren(QDialog)
                              if d.isVisible() and d.windowTitle() == "Insert OS-9 file")
                permissions = dialog.findChild(QComboBox)
                assert permissions.itemData(1) == 0x1B and permissions.itemData(2) == 0x3F
                permissions.setCurrentIndex(2)
                dialog.grab().save(str(work / "os9-insert.png"))
                captured.append(True)
                print("OS-9 insertion dialog inspected: executable/data permission presets; synthetic image only.", flush=True)
                if not options.keep_open:
                    dialog.reject()
            QTimer.singleShot(150, inspect_os9_dialog)
            window.insert_os9_file(candidate, window.current[0], "/")
            assert captured
            if not options.keep_open:
                window.close()
                app.quit()
            return
        if options.write_options:
            window.write_inspection = WriteOptionsDialog(image, window)
            window.write_inspection.show()
            def inspect_options():
                dialog = window.write_inspection
                assert dialog.backup_first.isChecked()
                dialog.grab().save(str(work / "write-backup-first.png"))
                dialog.backup_first.setChecked(False)
                def capture_direct():
                    dialog.grab().save(str(work / "write-direct.png"))
                    print("Source/backup-first/direct dialog inspected; no card selected or opened.", flush=True)
                    if not options.keep_open:
                        dialog.reject()
                        window.close()
                        app.quit()
                QTimer.singleShot(150, capture_direct)
            QTimer.singleShot(150, inspect_options)
            return
        if not options.keep_open:
            window.close()
            app.quit()

timer = QTimer()
timer.timeout.connect(inspect)
timer.start(100)
raise SystemExit(app.exec())
