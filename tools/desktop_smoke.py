#!/usr/bin/env python3
"""Launch actual Widgets, exercise browsing, save a screenshot; no device access."""
import argparse
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
args = argparse.ArgumentParser(description=__doc__)
args.add_argument("--offscreen", action="store_true")
args.add_argument("--keep-open", action="store_true")
options = args.parse_args()
if options.offscreen:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["XDG_CONFIG_HOME"] = str(ROOT / "data/work/smoke-config")
os.environ["XDG_DATA_HOME"] = str(ROOT / "data/work/smoke-data")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from drivepak_manager.gui import MainWindow
from drivepak_manager.image import Image, create_blank
from drivepak_manager.filesystems.decb import DECB

work = ROOT / "data/work"
work.mkdir(parents=True, exist_ok=True)
image = work / "desktop-demo-original.img"
if not image.exists():
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
        window.tree.topLevelItem(0).setExpanded(True)
        window.tree.setCurrentItem(window.tree.topLevelItem(0).child(0))
        state[0] = 1
    elif state[0] == 1:
        assert window.files.rowCount() == 1
        assert window.files.item(0, 0).text() == "HELLO.BAS"
        assert window.actions["insert"].isEnabled()
        assert window.actions["write"].isEnabled()
        window.grab().save(str(work / "desktop.png"))
        print(f"Desktop inspected: {app.platformName()}, screenshot {work / 'desktop.png'}", flush=True)
        state[0] = 2
        if not options.keep_open:
            window.close()
            app.quit()

timer = QTimer()
timer.timeout.connect(inspect)
timer.start(100)
raise SystemExit(app.exec())
