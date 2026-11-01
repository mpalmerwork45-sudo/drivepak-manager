"""Native desktop workflows. All long operations run outside the GUI thread."""
import json
import os
import stat
from pathlib import Path
import re
import sys
import threading
import tempfile
from datetime import datetime

from PySide6.QtCore import Qt, QSettings, QStandardPaths, QThread, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDockWidget, QFileDialog, QFormLayout, QHBoxLayout,
    QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QSplitter, QStyle,
    QTableWidget, QTableWidgetItem, QToolBar, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget, QAbstractItemView)

from . import __version__
from .backup import verify_backup
from .devices import discover, unmount
from .errors import DrivePakError, FormatError
from .filesystems.decb import DECB
from .filesystems.os9 import OS9
from .format import BLOCK, DEFAULTS, SLOT_BYTES
from .image import RAW_DISK_SIZES, Session, create_blank, read_disk_file
from .linux import backup_card, write_card, write_card_direct, apply_card_changes
from .patches import prepare_changes
from .streams import checkpoint, report, write_json, distinct, copy_verified, fingerprint


def human(n):
    return f"{n / 1024**3:.2f} GiB" if n >= 1024**3 else f"{n / 1024**2:.2f} MiB"


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9._-]", "_", name)


class Worker(QThread):
    progress = Signal(object, object, str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn, self.cancel = fn, threading.Event()
        self.result, self.error = None, None

    def run(self):
        try:
            self.result = self.fn(lambda d, t, p: self.progress.emit(d, t, p), self.cancel)
        except Exception as e:
            self.error = str(e) or type(e).__name__


class TextDialog(QDialog):
    def __init__(self, title, text, parent=None, confirm=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(760, 460)
        layout = QVBoxLayout(self)
        body = QPlainTextEdit()
        body.setReadOnly(True)
        body.setPlainText(text)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel if confirm else QDialogButtonBox.StandardButton.Close)
        if confirm:
            action = buttons.addButton(confirm, QDialogButtonBox.ButtonRole.AcceptRole)
            action.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class DeviceDialog(QDialog):
    def __init__(self, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 450)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Select an external USB reader or removable MMC card. System/internal disks are protected."))
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Path", "Device / model", "Actual capacity", "Serial / stable ID", "Mount / use status"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.setWordWrap(False)
        layout.addWidget(self.table)
        self.reason = QLabel("Refresh the list, then select a card.")
        self.reason.setWordWrap(True)
        layout.addWidget(self.reason)
        row = QHBoxLayout()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        self.unmount_button = QPushButton("Unmount selected card volumes")
        self.unmount_button.clicked.connect(self.unmount_selected)
        row.addWidget(refresh)
        row.addWidget(self.unmount_button)
        row.addStretch()
        layout.addLayout(row)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.table.itemSelectionChanged.connect(self.update_selection)
        self.worker = None
        self.devices = []
        self.refresh_button = refresh
        self.refresh()

    def refresh(self):
        self.inspect_async("Discovering devices…", lambda p, c: discover())

    def inspect_async(self, message, operation):
        if self.worker:
            return
        self.reason.setText(message)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        self.unmount_button.setEnabled(False)
        self.refresh_button.setEnabled(False)
        worker = Worker(operation, self)
        self.worker = worker
        worker.finished.connect(self._inspection_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()

    @Slot()
    def _inspection_finished(self):
        worker = self.sender()
        if worker is not self.worker:
            return
        self.worker = None
        self.refresh_button.setEnabled(True)
        if worker.cancel.is_set():
            QDialog.reject(self)
        elif worker.error is not None:
            self.reason.setText(worker.error)
        else:
            self.devices = worker.result
            self.table.setRowCount(len(self.devices))
            self.table.clearSelection()
            self.table.setCurrentCell(-1, -1)
            for row, d in enumerate(self.devices):
                values = [d.path, d.model, f"{d.capacity:,} bytes ({human(d.capacity)})", d.serial or d.wwn or f"diskseq {d.diskseq}", d.status]
                for col, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    item.setToolTip(value)
                    self.table.setItem(row, col, item)
            self.update_selection()
        worker.deleteLater()

    def reject(self):
        if self.worker:
            self.worker.cancel.set()
            self.reason.setText("Finishing device inspection/unmount, then closing…")
        else:
            super().reject()

    def accept(self):
        device = self.selected()
        if not self.worker and device and device.usable:
            super().accept()
        elif device:
            self.reason.setText(device.status)

    def closeEvent(self, event):
        if self.worker:
            self.reject()
            event.ignore()
        else:
            super().closeEvent(event)

    def selected(self):
        row = self.table.currentRow()
        return self.devices[row] if 0 <= row < len(self.devices) else None

    def update_selection(self):
        d = self.selected()
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(d and d.usable and not self.worker))
        self.unmount_button.setEnabled(bool(d and d.mounts and not d.problems and not self.worker))
        self.reason.setText(("Available. Identity and use status will be checked again before device access." if d.usable else d.status) if d else "Select a card. No write destination is remembered.")

    def unmount_selected(self):
        d = self.selected()
        if not d:
            return
        # A deliberate user action through UDisks, never called during discovery.
        def operation(progress, cancel):
            checkpoint(cancel)
            unmount(d)
            return discover()
        self.inspect_async("Unmounting selected volumes through UDisks…", operation)


class MappingDialog(QDialog):
    def __init__(self, image, sources, selected=None, parent=None):
        super().__init__(parent)
        self.image, self.sources = image, sources
        self.parts = [p for p in image.layout().partitions if p.kind == 2 and p.disks]
        if not self.parts:
            raise FormatError("Create a Disk BASIC partition before importing disks.")
        self.setWindowTitle("Import disks — destination and overwrite preview")
        self.resize(1000, 500)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Each row replaces one complete virtual disk's logical payload. Card padding is retained. Review the destination slots."))
        self.table = QTableWidget(len(sources), 5)
        self.table.setHorizontalHeaderLabels(["Source", "Raw data size", "Destination partition", "Disk number (0 based)", "Currently contains"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.combos, self.spins = [], []
        for row, source in enumerate(sources):
            b = read_disk_file(source)
            self.table.setItem(row, 0, QTableWidgetItem(str(source)))
            self.table.setItem(row, 1, QTableWidgetItem(f"{len(b):,} bytes"))
            combo = QComboBox()
            for p in self.parts:
                combo.addItem(f"{p.index}: {p.name} ({p.disks} slots)", p.index)
            if selected:
                combo.setCurrentIndex(max(0, combo.findData(selected[0])))
            spin = QSpinBox()
            spin.setRange(0, self.parts[combo.currentIndex()].disks - 1)
            spin.setValue(min((selected[1] if selected and selected[1] is not None else 0) + row, spin.maximum()))
            self.combos.append(combo)
            self.spins.append(spin)
            self.table.setCellWidget(row, 2, combo)
            self.table.setCellWidget(row, 3, spin)
            combo.currentIndexChanged.connect(lambda _, r=row: self.preview(r))
            spin.valueChanged.connect(lambda _, r=row: self.preview(r))
            self.preview(row)
        layout.addWidget(self.table)
        self.tail = QComboBox()
        self.tail.addItem("Clear unused logical tail to 0x00 (recommended for a smaller disk)", "zero")
        self.tail.addItem("Fill unused logical tail with 0xFF", "ff")
        self.tail.addItem("Retain the destination's unused logical tail", "preserve")
        layout.addWidget(self.tail)
        note = QLabel("Original floppy geometry is not stored in a slot. A full-slot export always contains 737,280 payload bytes. Smaller imports do not change any disk numbers.")
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Import mapped disks", QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def preview(self, row):
        p = self.parts[self.combos[row].currentIndex()]
        spin = self.spins[row]
        spin.setMaximum(p.disks - 1)
        try:
            fs = DECB(self.image.volume(p.index, spin.value()))
            value = ", ".join(f.name for f in fs.files[:5]) or "Empty Disk BASIC disk"
            if len(fs.files) > 5:
                value += f" … ({len(fs.files)} files)"
        except DrivePakError:
            value = "Unrecognised filesystem — raw slot will be replaced"
        item = QTableWidgetItem(value)
        item.setToolTip(value)
        self.table.setItem(row, 4, item)

    def mappings(self):
        result = [(self.combos[r].currentData(), self.spins[r].value(), source) for r, source in enumerate(self.sources)]
        if len(set((p, d) for p, d, _ in result)) != len(result):
            raise FormatError("Two imports map to the same slot. Give each source a distinct destination.")
        return result


class WriteOptionsDialog(QDialog):
    def __init__(self, source="", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Write image file to SD card")
        self.resize(760, 280)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Read an image file → write the selected SD card.\nThe source image is opened read-only."))
        row = QHBoxLayout()
        self.source = QLineEdit(str(source))
        self.source.setPlaceholderText("Choose the .img file to read, for example NEWIMAGE.img")
        browse = QPushButton("Choose image…")
        browse.clicked.connect(self.choose_image)
        row.addWidget(self.source, 1)
        row.addWidget(browse)
        layout.addWidget(QLabel("Image file to read from:"))
        layout.addLayout(row)
        self.backup_first = QCheckBox("Create and verify a backup of the card first (recommended)")
        self.backup_first.setChecked(True)
        layout.addWidget(self.backup_first)
        warning = QLabel("Direct write: no recovery backup will be created. The card's existing contents will be overwritten. An interrupted or failed write may leave incomplete data. Readback verification still runs.")
        warning.setWordWrap(True)
        warning.setVisible(False)
        self.backup_first.toggled.connect(lambda checked: warning.setVisible(not checked))
        layout.addWidget(warning)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Choose SD card…", QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choose_image(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose SOURCE image to read", self.source.text(), "Raw card images (*.img *.bin);;All files (*)")
        if path:
            self.source.setText(path)

    def accept(self):
        try:
            p = Path(self.source.text()).resolve()
            s = p.stat()
            if not stat.S_ISREG(s.st_mode) or not s.st_size or s.st_size % 512:
                raise FormatError("Choose an existing, nonempty raw image file with a length divisible by 512 bytes.")
            self.source.setText(str(p))
        except (OSError, DrivePakError) as e:
            QMessageBox.warning(self, "Choose source image", str(e))
            return
        super().accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("DrivePak", "DrivePak Manager")
        self.session = None
        self.worker = None
        self.current = None
        self.basic_editable = False
        self.filesystem_readable = False
        self.os9_path = "/"
        self.backup_status = "No card backup associated with this session"
        self.written_status = "No card write in this session"
        self.setWindowTitle(f"DrivePak Manager {__version__}")
        self.resize(1250, 790)
        self.actions = {}
        self.build_ui()
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self.refresh()

    @property
    def work_dir(self):
        base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
        path = Path(self.settings.value("work_dir", str(Path(base) / "work")))
        path.mkdir(parents=True, exist_ok=True)
        return path

    def action(self, key, label, handler, shortcut=None, icon=None):
        act = QAction(self.style().standardIcon(icon) if icon else self.windowIcon(), label, self)
        act.triggered.connect(handler)
        if shortcut:
            act.setShortcut(shortcut)
        self.actions[key] = act
        return act

    def build_ui(self):
        S = QStyle.StandardPixmap
        self.action("open", "Open image…", self.open_image, QKeySequence.StandardKey.Open, S.SP_DialogOpenButton)
        self.action("new", "Create blank image…", self.new_image, QKeySequence.StandardKey.New, S.SP_FileIcon)
        self.action("save", "Save image as…", self.save_image, QKeySequence.StandardKey.SaveAs, S.SP_DialogSaveButton)
        self.action("discard", "Discard changes", self.discard_changes, icon=S.SP_ArrowBack)
        self.action("backup", "Back up card / Open card…", self.backup_action, icon=S.SP_DriveHDIcon)
        self.action("verify", "Verify backup…", self.verify_action, icon=S.SP_DialogApplyButton)
        self.action("write", "Write image to card…", self.write_action, icon=S.SP_ArrowForward)
        self.action("apply", "Apply image edits to card…", self.apply_changes_action, icon=S.SP_DialogApplyButton)
        self.action("recover_edits", "Recover card edits…", self.recover_edits_action)
        self.action("import_disk", "Import disk(s)…", self.import_disks, icon=S.SP_ArrowDown)
        self.action("export_disk", "Export disk(s)…", self.export_disks, icon=S.SP_ArrowUp)
        self.action("insert", "Insert file…", self.insert_file)
        self.action("extract", "Extract file(s)…", self.extract_files)
        self.action("rename_file", "Rename file…", self.rename_file)
        self.action("delete_file", "Delete file", self.delete_file)
        self.action("create_part", "Create partition / Add OS-9 volume…", self.create_partition)
        self.action("rename_part", "Rename partition…", self.rename_partition)
        self.action("delete_part", "Delete partition…", self.delete_partition)
        self.action("default", "Set default drive…", self.set_default)
        self.action("export_os9", "Export OS-9 volume…", self.export_os9)
        self.action("import_os9", "Replace OS-9 volume…", self.import_os9)
        self.action("review", "Review changes", self.review_changes)
        self.action("diagnostics", "Save diagnostics…", self.save_diagnostics)
        self.action("preferences", "Working image folder…", self.choose_work_dir)
        self.action("guide", "Quick start and recovery", self.guide)
        menus = [("&File", ["open", "new", "save", "discard", "review", "diagnostics", "preferences"]),
                 ("&Card", ["backup", "verify", "write", "apply", "recover_edits"]),
                 ("&Disks", ["import_disk", "export_disk", "export_os9", "import_os9"]),
                 ("&Files", ["insert", "extract", "rename_file", "delete_file"]),
                 ("&Partitions", ["create_part", "rename_part", "delete_part", "default"]),
                 ("&Help", ["guide"])]
        for label, keys in menus:
            menu = self.menuBar().addMenu(label)
            for key in keys:
                menu.addAction(self.actions[key])
        bar = QToolBar("Main actions")
        bar.setMovable(False)
        bar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(bar)
        for key in ("open", "backup", "save", "import_disk", "export_disk", "write"):
            bar.addAction(self.actions[key])
        center = QWidget()
        root = QVBoxLayout(center)
        title = QLabel("DrivePak Manager")
        title.setStyleSheet("font-size: 25px; font-weight: 600; padding: 5px 0;")
        root.addWidget(title)
        self.identity_label = QLabel()
        self.identity_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.identity_label.setWordWrap(True)
        root.addWidget(self.identity_label)
        self.state_label = QLabel()
        self.state_label.setWordWrap(True)
        self.state_label.setStyleSheet("padding: 8px; border: 1px solid palette(mid); border-radius: 4px;")
        root.addWidget(self.state_label)
        self.layout_notice = QLabel()
        self.layout_notice.setWordWrap(True)
        self.layout_notice.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.layout_notice.hide()
        root.addWidget(self.layout_notice)
        search_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter the current directory by name")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.filter_files)
        self.search_all = QPushButton("Search disks…")
        self.search_all.clicked.connect(self.search_disks)
        search_row.addWidget(self.search)
        search_row.addWidget(self.search_all)
        root.addLayout(search_row)
        split = QSplitter()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabel("Partitions and virtual disks")
        self.tree.setMinimumWidth(290)
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.tree.itemSelectionChanged.connect(self.selection_changed)
        split.addWidget(self.tree)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        browse = QHBoxLayout()
        self.up = QPushButton("Up")
        self.up.clicked.connect(self.go_up)
        self.breadcrumb = QLabel("Select a disk or an OS-9 volume")
        browse.addWidget(self.up)
        browse.addWidget(self.breadcrumb, 1)
        right_layout.addLayout(browse)
        self.files = QTableWidget(0, 3)
        self.files.setHorizontalHeaderLabels(["Name", "Bytes", "Kind"])
        self.files.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.files.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.files.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.files.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.files.itemDoubleClicked.connect(self.file_activated)
        self.files.itemSelectionChanged.connect(self.update_actions)
        right_layout.addWidget(self.files)
        self.directory_status = QLabel()
        self.directory_status.setWordWrap(True)
        right_layout.addWidget(self.directory_status)
        split.addWidget(right)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)
        self.setCentralWidget(center)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        dock = QDockWidget("Technical details", self)
        dock.setWidget(self.details)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)
        dock.hide()
        self.menuBar().addMenu("&View").addAction(dock.toggleViewAction())
        self.progress = QProgressBar()
        self.progress.setMaximumWidth(260)
        self.progress.hide()
        self.cancel_button = QPushButton("Cancel operation")
        self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.cancel_operation)
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().addPermanentWidget(self.cancel_button)

    def fail(self, message):
        QMessageBox.warning(self, "DrivePak Manager", message)

    def run_task(self, title, fn, done=None, failed=None):
        if self.worker:
            return
        self.worker = Worker(fn, self)
        worker = self.worker
        worker.on_done, worker.on_failed = done, failed
        self.progress.setRange(0, 0)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.statusBar().showMessage(title)
        self.update_actions()
        self.tree.setEnabled(False)
        self.files.setEnabled(False)

        worker.progress.connect(self._task_progress, Qt.ConnectionType.QueuedConnection)
        worker.finished.connect(self._task_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()

    @Slot(object, object, str)
    def _task_progress(self, done, total, phase):
        if self.sender() is not self.worker:
            return
        self.progress.setRange(0, 1000 if total else 0)
        if total:
            self.progress.setValue(int(done * 1000 / total))
        self.statusBar().showMessage(f"{phase} — {done:,} / {total:,} bytes or items" if total else phase)

    @Slot()
    def _task_finished(self):
        worker = self.sender()
        if worker is not self.worker:
            return
        self.worker = None
        self.progress.hide()
        self.cancel_button.hide()
        self.tree.setEnabled(True)
        self.files.setEnabled(True)
        self.update_actions()
        self.statusBar().showMessage("Ready")
        if worker.error is not None:
            (worker.on_failed or self.fail)(worker.error)
        elif worker.on_done:
            try:
                worker.on_done(worker.result)
            except Exception as e:
                self.fail(str(e))
        worker.deleteLater()

    def cancel_operation(self):
        if self.worker:
            self.worker.cancel.set()
            self.cancel_button.setEnabled(False)
            self.statusBar().showMessage("Cancelling safely between I/O chunks. A started card write may leave incomplete contents.")

    def update_actions(self):
        busy = self.worker is not None
        if busy:
            # Never seek the shared image for metadata while a worker is reading
            # or changing it. Controls may emit selection signals during refresh.
            for action in self.actions.values():
                action.setEnabled(False)
            self.search_all.setEnabled(False)
            self.search.setEnabled(False)
            self.up.setEnabled(False)
            self.actions["import_disk"].setToolTip("Wait for the current operation to finish.")
            return
        loaded = self.session is not None
        layout = self.session.image.layout() if loaded else None
        valid = bool(layout and layout.editable)
        known = bool(layout and layout.addressing_known)
        part = None
        if loaded and self.current:
            try:
                part = self.session.image.layout().partition(self.current[0])
            except DrivePakError:
                pass
        disk = bool(part and part.kind == 2 and self.current[1] is not None)
        os9 = bool(part and part.kind in (3, 4))
        selected = bool(self.files.selectionModel().selectedRows())
        always = {"open", "new", "backup", "verify", "write", "recover_edits", "preferences", "guide"}
        edits = {"create_part", "rename_part", "delete_part", "default", "import_disk"}
        for key, action in self.actions.items():
            enabled = key in always or loaded
            if key in edits:
                enabled = valid and (key in ("create_part", "import_disk") or part is not None)
            if key in ("insert", "rename_file", "delete_file"):
                enabled = valid and disk and self.basic_editable and (key == "insert" or selected)
            if key == "extract":
                enabled = selected and (disk or os9) and self.filesystem_readable
            if key == "export_disk":
                enabled = disk and known
            if key in ("import_os9", "export_os9"):
                enabled = os9 and known and (valid or key == "export_os9")
            if key == "apply":
                enabled = valid and bool(self.session.changes)
            action.setEnabled(enabled and not busy)
        if busy:
            import_tip = "Wait for the current operation to finish."
        elif not loaded:
            import_tip = "Open an image or verified card backup first."
        elif not valid:
            import_tip = "Import is unavailable: " + "; ".join(layout.errors)
        else:
            import_tip = "Choose raw disk images, then review the destination partition and numbered slot for each disk."
        self.actions["import_disk"].setToolTip(import_tip)
        self.actions["import_disk"].setStatusTip(import_tip)
        self.search_all.setEnabled(known and not busy)
        self.search_all.setToolTip("Search filenames across validated Disk BASIC banks" if known else "Card addressing must be recognised before searching logical disks")
        self.search.setEnabled(not busy)
        self.up.setEnabled(os9 and self.os9_path != "/" and not busy)

    def refresh(self, preserve=None):
        old = preserve or self.current
        self.tree.blockSignals(True)
        self.tree.clear()
        self.files.setRowCount(0)
        self.current = None
        self.basic_editable = False
        self.filesystem_readable = False
        chosen = None
        if self.session:
            s = self.session
            layout = s.image.layout()
            notices = layout.errors or layout.warnings
            self.layout_notice.setText(("Image editing is unavailable: " if layout.errors else "Layout notes: ") + "; ".join(notices[:3]))
            self.layout_notice.setVisible(bool(notices))
            for p in layout.partitions:
                node = QTreeWidgetItem([f"{p.index}: {p.name} · {p.label}" + (f" · {p.disks} disks" if p.kind == 2 else "")])
                node.setData(0, Qt.ItemDataRole.UserRole, (p.index, None))
                node.setToolTip(0, f"{p.count:,} card sectors; {human(p.count * BLOCK)} physical")
                self.tree.addTopLevelItem(node)
                if old == (p.index, None):
                    chosen = node
                if p.kind == 2:
                    for d in range(p.disks):
                        child = QTreeWidgetItem([f"Disk {d:03d}"])
                        child.setData(0, Qt.ItemDataRole.UserRole, (p.index, d))
                        node.addChild(child)
                        if old == (p.index, d):
                            chosen = child
            self.identity_label.setText(f"Original: {s.original}\nWorking copy: {s.working}")
            state = f"{len(s.changes)} recorded change(s) · " + (f"Saved image: {s.saved_as}" if s.saved_as else ("Unsaved working changes" if s.changes else "Working copy matches original"))
            startup = next((p.name for p in layout.partitions if p.start == layout.defaults.get("DOS") and p.kind == 2), "unassigned or unrecognised")
            state += f"\nStartup Disk BASIC bank: {startup}. Disk numbers belong to the selected bank."
            free = f" · {human(sum(n for _, n in layout.free_regions()) * BLOCK)} free physical space" if layout.editable else " · layout needs inspection"
            self.state_label.setText(f"{layout.name} · {layout.size:,} bytes ({human(layout.size)}){free}\n{state}\n{self.backup_status} · {self.written_status}")
            self.details.setPlainText(json.dumps({"name": layout.name, "table_sector": layout.table, "terminator_sector": layout.terminator,
                "default_drives": layout.defaults, "errors": layout.errors, "warnings": layout.warnings,
                "free_regions_sectors": layout.free_regions() if layout.editable else "Unknown: invalid/ambiguous layout",
                "original_sha256": s.original_digest}, indent=2))
            self.directory_status.setText("; ".join(layout.errors) or f"{len(layout.partitions)} partitions. Select a disk to browse; select multiple disks for batch export.")
            self.setWindowTitle(f"DrivePak Manager — {s.original.name}" + (" *" if s.changes and not s.saved_as else ""))
        else:
            self.layout_notice.hide()
            self.identity_label.setText("Open an image, create a blank image, or select a card to back it up and begin editing.")
            self.state_label.setText(f"{self.written_status}\n{self.backup_status}" if self.written_status.startswith("Card write readback verified") else "Card workflow: Select card → verified complete backup → separate working image → edit → review → write → readback verify")
            self.directory_status.setText("Image files and card backups are kept unchanged. All edits use a separate working image.")
        if chosen:
            self.tree.setCurrentItem(chosen)
            chosen.setSelected(True)
        self.tree.blockSignals(False)
        if chosen:
            self.selection_changed()
        self.update_actions()

    def selection_changed(self):
        item = self.tree.currentItem()
        if not self.session or not item or self.worker:
            return
        selection = tuple(item.data(0, Qt.ItemDataRole.UserRole))
        if selection == self.current:
            return
        self.current = selection
        self.os9_path = "/"
        self.browse_current()

    def browse_current(self):
        if not self.current or not self.session:
            return
        pidx, disk = self.current
        self.basic_editable = False
        self.filesystem_readable = False
        part = self.session.image.layout().partition(pidx)
        self.files.setRowCount(0)
        self.breadcrumb.setText(f"{part.name} / " + (f"Disk {disk:03d}" if disk is not None else self.os9_path))
        if disk is not None:
            self.breadcrumb.setText(f'{part.name} / Disk {disk:03d} · CoCo: DRIVE# "{part.name}" then DRIVE 0,#{disk}')
        if disk is None and part.kind == 2:
            self.directory_status.setText(f"{part.disks} complete virtual floppy slots. Expand this partition and select a disk.")
            self.update_actions()
            return
        if part.kind not in (2, 3, 4):
            self.directory_status.setText(f"{part.label}: raw backup and diagnostic inspection are available. Its filesystem is unsupported.")
            self.update_actions()
            return
        path = self.os9_path

        def load(progress, cancel):
            checkpoint(cancel)
            if disk is not None:
                fs = DECB(self.session.image.volume(pidx, disk))
                return (fs.files, f"{len(fs.files)} files · {fs.free_bytes:,} bytes free in standard 68 granules. " + "; ".join(fs.issues), False, not fs.issues)
            fs = OS9(self.session.image.volume(pidx))
            return (fs.list_dir(path), f"OS-9: {fs.name} · File browsing/extraction; volume import/export. Individual file modification is outside this release.", True, False)

        def show(result):
            rows, status, is_os9, can_edit = result
            self.basic_editable = can_edit
            self.filesystem_readable = True
            self.files.setRowCount(len(rows))
            for row, f in enumerate(rows):
                kind = ("Directory" if f.is_dir else "OS-9 file") if is_os9 else {0: "BASIC program", 1: "BASIC data", 2: "Machine code", 3: "Text/editor source"}.get(f.file_type, f"Type {f.file_type}") + " · " + ("ASCII" if f.ascii_flag else "Binary")
                metadata = (f"Descriptor sector {f.lsn}" if is_os9 else f"Directory {f.index}; {len(f.chain)} granules")
                for col, value in enumerate((f.name, str(f.size), kind)):
                    item = QTableWidgetItem(value)
                    item.setToolTip(metadata)
                    if col == 0:
                        item.setData(Qt.ItemDataRole.UserRole, f)
                    self.files.setItem(row, col, item)
            self.directory_status.setText(status)
            self.filter_files()
            self.update_actions()
        def unreadable(message):
            self.directory_status.setText(message + " Raw disk/partition export and image backup remain available.")
            self.update_actions()
        self.run_task("Reading directory", load, show, unreadable)

    def filter_files(self):
        term = self.search.text().casefold()
        for row in range(self.files.rowCount()):
            item = self.files.item(row, 0)
            self.files.setRowHidden(row, bool(item and term not in item.text().casefold()))

    def file_activated(self, item):
        f = self.files.item(item.row(), 0).data(Qt.ItemDataRole.UserRole)
        if hasattr(f, "is_dir") and f.is_dir:
            self.os9_path = self.os9_path.rstrip("/") + "/" + f.name
            self.browse_current()

    def go_up(self):
        self.os9_path = self.os9_path.rsplit("/", 1)[0] or "/"
        self.browse_current()

    def can_replace_session(self):
        if self.session and self.session.changes and not self.session.saved_as:
            return QMessageBox.question(self, "Unsaved working changes", "Leave this working image? Its separate file remains available for recovery. Save image as first if you want a named result.") == QMessageBox.StandardButton.Yes
        return True

    def adopt(self, session):
        if self.session:
            self.session.close()
        self.session = session
        self.current = None
        self.written_status = "No card write in this session"
        self.refresh()

    def open_path(self, path):
        work = self.work_dir
        def opening(progress, cancel):
            manifest_path = Path(str(path) + ".json")
            manifest = None
            if manifest_path.is_file() and manifest_path.stat().st_size <= 65536:
                try:
                    header = json.loads(manifest_path.read_text())
                    if header.get("kind") == "complete-physical-backup":
                        manifest = verify_backup(path, progress, cancel)
                except (ValueError, AttributeError) as e:
                    raise FormatError(f"Cannot read image sidecar: {e}") from e
            return Session.open(path, work, progress, cancel), manifest
        def opened(result):
            session, manifest = result
            self.backup_status = f"Verified saved backup: {path} (current-card correspondence checked before writing)" if manifest else "Image file · no current-card backup established"
            self.adopt(session)
        self.run_task("Creating verified separate working image", opening, opened)

    def open_image(self):
        if not self.can_replace_session():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Open image or recovery backup", self.settings.value("last_open", ""), "Raw card images (*.img *.bin);;All files (*)")
        if path:
            self.settings.setValue("last_open", str(Path(path).parent))
            self.open_path(path)

    def new_image(self):
        if not self.can_replace_session():
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Create blank DrivePak image")
        form = QFormLayout(dialog)
        name, size, disks = QLineEdit("DRIVEPAK"), QSpinBox(), QSpinBox()
        size.setRange(4, 8192)
        size.setValue(64)
        size.setSuffix(" MiB physical image")
        disks.setRange(1, 256)
        disks.setValue(16)
        form.addRow("Card name (1–8 ASCII characters)", name)
        form.addRow("Image capacity", size)
        form.addRow("Empty Disk BASIC slots", disks)
        note = QLabel("Creates a DOS bank, no games or firmware. Capacity must fit 512 KiB reserved space plus 1,474,560 bytes per disk slot.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Create new blank image", "blank-drivepak.img", "Raw card image (*.img)")
        if not path:
            return
        capacity, slots, card_name, work = size.value() * 1024**2, disks.value(), name.text(), self.work_dir

        def create(p, c):
            create_blank(path, capacity, slots, card_name, p, c)
            return Session.open(path, work, p, c)
        self.backup_status = "New blank image · no current-card backup established"
        self.run_task("Creating blank image", create, self.adopt)

    def save_image(self):
        if not self.session:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save image under a new name", str(self.session.original.with_name(self.session.original.stem + "-edited.img")), "Raw card image (*.img)")
        if path:
            self.run_task("Saving and verifying image", lambda p, c: self.session.save_as(path, p, c), lambda _: self.refresh())

    def discard_changes(self):
        if self.session:
            self.run_task("Restoring working copy from unchanged original", lambda p, c: self.session.discard(p, c), lambda _: self.refresh())

    def edited(self, description):
        self.session.changed(description)
        self.refresh()

    def import_disks(self):
        if not self.session:
            return
        paths, _ = QFileDialog.getOpenFileNames(self, "Import raw floppy images", self.settings.value("last_disk", ""), "Disk images (*.dsk *.raw);;All files (*)")
        if not paths:
            return
        self.settings.setValue("last_disk", str(Path(paths[0]).parent))
        try:
            dialog = MappingDialog(self.session.image, paths, self.current, self)
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            mapping, tail = dialog.mappings(), dialog.tail.currentData()
        except Exception as e:
            self.fail(str(e))
            return
        description = "; ".join(f"Imported {Path(path).name} → partition {p}, disk {d} (unused tail: {tail})" for p, d, path in mapping)
        self.run_task("Importing mapped disks", lambda p, c: self.session.image.import_disks(mapping, tail, p, c), lambda _: self.edited(description))

    def selected_disks(self):
        return sorted({tuple(i.data(0, Qt.ItemDataRole.UserRole)) for i in self.tree.selectedItems() if i.data(0, Qt.ItemDataRole.UserRole)[1] is not None})

    def export_size(self):
        choices = [f"{n:,} bytes — {RAW_DISK_SIZES[n]}" for n in reversed(RAW_DISK_SIZES)]
        label, ok = QInputDialog.getItem(self, "Choose explicit export size", "The original floppy length is not stored. Choose full slot for all payload bytes; smaller sizes intentionally omit the logical tail.", choices, 0, False)
        return list(reversed(RAW_DISK_SIZES))[choices.index(label)] if ok else None

    def export_disks(self):
        selected = self.selected_disks()
        if not selected:
            return
        size = self.export_size()
        if size is None:
            return
        mapping = []
        if len(selected) == 1:
            p, d = selected[0]
            path, _ = QFileDialog.getSaveFileName(self, "Export raw floppy", f"partition-{p}-disk-{d:03d}.dsk", "Raw floppy image (*.dsk)")
            if path:
                mapping.append((p, d, path))
        else:
            directory = QFileDialog.getExistingDirectory(self, "Batch export destination (existing files are protected)")
            if not directory:
                return
            for p, d in selected:
                part = self.session.image.layout().partition(p)
                mapping.append((p, d, str(Path(directory) / f"P{p}_{safe_name(part.name)}-D{d:03d}.dsk")))
            text = "\n".join(f"Partition {p}, disk {d} → {path}" for p, d, path in mapping)
            if TextDialog("Batch export mapping", text + f"\n\nEach output: {size:,} raw bytes. Existing outputs are never overwritten.", self, "Export mapped disks").exec() != QDialog.DialogCode.Accepted:
                return
        if not mapping:
            return

        def export(progress, cancel):
            for _, _, path in mapping:
                if Path(path).exists() or Path(str(path) + ".json").exists():
                    raise FormatError(f"Output or metadata exists: {path}. Choose a new destination.")
            for p, d, path in mapping:
                self.session.image.export_disk(p, d, path, size, progress, cancel)
                write_json(str(path) + ".json", {"representation": "raw 256-byte logical sectors", "partition": p, "disk": d, "bytes": size,
                    "notice": "Explicit export length chosen by user; original floppy geometry/length cannot be recovered from a slot."})
            return len(mapping)
        self.run_task("Exporting disks", export, lambda n: self.statusBar().showMessage(f"Exported {n} disk(s)"))

    def basic_fs(self):
        if not self.current or self.current[1] is None:
            raise FormatError("Select a Disk BASIC disk.")
        return DECB(self.session.image.volume(*self.current, editing=True))

    def selected_files(self):
        return [self.files.item(i.row(), 0).data(Qt.ItemDataRole.UserRole) for i in self.files.selectionModel().selectedRows() if not self.files.isRowHidden(i.row())]

    def insert_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Insert file bytes without text conversion")
        if not path:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Insert Disk BASIC file")
        form = QFormLayout(dialog)
        name = QLineEdit(Path(path).name.upper())
        kind = QComboBox()
        for label in ("0 — BASIC program", "1 — BASIC data", "2 — Machine code", "3 — Text/editor source"):
            kind.addItem(label)
        kind.setCurrentIndex(2)
        ascii_flag, replace = QCheckBox("ASCII metadata (bytes remain unchanged)"), QCheckBox("Replace existing file with this name")
        form.addRow("8.3 filename", name)
        form.addRow("File type", kind)
        form.addRow(ascii_flag)
        form.addRow(replace)
        form.addRow(QLabel("A matching .drivepak.json extraction sidecar restores original type, format and reserved metadata."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        filename, ftype, flag, replacing = name.text(), kind.currentIndex(), 255 if ascii_flag.isChecked() else 0, replace.isChecked()
        self.run_task("Inserting file", lambda p, c: self.basic_fs().insert(path, filename, ftype, flag, replacing), lambda _: self.edited(f"Inserted {filename} in partition {self.current[0]}, disk {self.current[1]}"))

    def extract_files(self):
        files = self.selected_files()
        if not files:
            return
        directory = QFileDialog.getExistingDirectory(self, "Extract selected file(s) and metadata sidecars")
        if not directory:
            return
        pidx, disk = self.current
        os9path = self.os9_path

        def extract(progress, cancel):
            for f in files:
                if getattr(f, "is_dir", False):
                    raise FormatError("Select files for extraction. Export the complete OS-9 volume for directories.")
                dest = Path(directory) / safe_name(f.name)
                if dest.exists() or Path(str(dest) + ".drivepak.json").exists():
                    raise FormatError(f"Extraction output already exists: {dest}")
            for n, f in enumerate(files):
                checkpoint(cancel)
                dest = Path(directory) / safe_name(f.name)
                if disk is not None:
                    DECB(self.session.image.volume(pidx, disk)).extract(f.name, dest)
                else:
                    OS9(self.session.image.volume(pidx)).extract(os9path.rstrip("/") + "/" + f.name, dest, progress, cancel)
                report(progress, n + 1, len(files), "Extracting files")
        self.run_task("Extracting files", extract, lambda _: self.statusBar().showMessage(f"Extracted {len(files)} file(s) with metadata"))

    def rename_file(self):
        files = self.selected_files()
        if len(files) != 1:
            self.fail("Select one file to rename.")
            return
        name, ok = QInputDialog.getText(self, "Rename Disk BASIC file", "New 8.3 filename", text=files[0].name)
        if ok:
            self.run_task("Renaming file", lambda p, c: self.basic_fs().rename(files[0].name, name), lambda _: self.edited(f"Renamed {files[0].name} → {name}"))

    def delete_file(self):
        files = self.selected_files()
        if len(files) != 1:
            self.fail("Select one file to delete.")
            return
        self.run_task("Deleting file in working image", lambda p, c: self.basic_fs().delete(files[0].name), lambda _: self.edited(f"Deleted {files[0].name} from disk {self.current}"))

    def create_partition(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("Create partition")
        form = QFormLayout(dialog)
        name = QLineEdit("GAMES")
        disks = QSpinBox()
        disks.setRange(1, 256)
        disks.setValue(16)
        os9file = QLineEdit()
        choose = QPushButton("Choose raw OS-9 volume…")
        choose.clicked.connect(lambda: os9file.setText(QFileDialog.getOpenFileName(self, "Raw OS-9 volume", "", "OS-9 volumes (*.os9 *.dsk *.raw);;All files (*)")[0]))
        form.addRow("Partition name (1–15 ASCII; ≤8 for Explorer)", name)
        form.addRow("Disk BASIC slots (if no OS-9 file)", disks)
        form.addRow("Or import a complete OS-9 volume", os9file)
        form.addRow(choose)
        free = self.session.image.layout().free_regions()
        form.addRow(QLabel(f"Free physical space: {human(sum(n for _, n in free) * BLOCK)}. Existing partitions and disk numbers stay in place."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        partname, slots, source = name.text(), disks.value(), os9file.text() or None
        self.run_task("Creating partition", lambda p, c: self.session.image.create_partition(partname, slots, source, p, c), lambda idx: self.edited(f"Created partition {idx}: {partname}"))

    def rename_partition(self):
        idx = self.current[0]
        part = self.session.image.layout().partition(idx)
        name, ok = QInputDialog.getText(self, "Rename partition", "New name (1–15 ASCII; use ≤8 for Explorer)", text=part.name)
        if ok:
            self.run_task("Renaming partition", lambda p, c: self.session.image.rename_partition(idx, name), lambda _: self.edited(f"Renamed partition {idx}: {part.name} → {name}"))

    def delete_partition(self):
        idx = self.current[0]
        layout = self.session.image.layout()
        part = layout.partition(idx)
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Delete partition {idx}: {part.name}")
        form = QFormLayout(dialog)
        form.addRow(QLabel("Removes this table entry. Data bytes stay intact; the space becomes available for new partitions. Other disk numbers do not change."))
        combos = {}
        for key, start in layout.defaults.items():
            if start != part.start:
                continue
            combo = QComboBox()
            combo.addItem("Unassigned (zero pointer)", None)
            for p in layout.partitions:
                if p.index != idx and ((key == "DOS" and p.kind == 2) or (key != "DOS" and p.kind in (3, 4))):
                    combo.addItem(f"{p.index}: {p.name}", p.index)
            combos[key] = combo
            form.addRow(f"Affected default {key}", combo)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        buttons.addButton("Delete partition in working image", QDialogButtonBox.ButtonRole.AcceptRole).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        form.addRow(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            replacements = {key: combo.currentData() for key, combo in combos.items()}
            self.run_task("Deleting partition", lambda p, c: self.session.image.delete_partition(idx, replacements), lambda _: self.edited(f"Deleted partition {idx}: {part.name}; defaults {replacements}"))

    def set_default(self):
        key, ok = QInputDialog.getItem(self, "Set default drive", "Assign selected partition to", list(DEFAULTS), 0, False)
        if ok:
            idx = self.current[0]
            self.run_task("Updating default drive", lambda p, c: self.session.image.set_default(key, idx), lambda _: self.edited(f"Default {key} → partition {idx}"))

    def export_os9(self):
        idx = self.current[0]
        part = self.session.image.layout().partition(idx)
        try:
            OS9(self.session.image.volume(idx))
        except DrivePakError as e:
            if TextDialog("OS-9 extent warning", str(e) + "\n\nYou may export the stored partition payload for diagnosis. This may be an incomplete OS-9 volume.", self, "Export stored partition payload").exec() != QDialog.DialogCode.Accepted:
                return
        path, _ = QFileDialog.getSaveFileName(self, "Export complete stored OS-9 partition payload", safe_name(part.name) + ".os9", "Raw OS-9 volume (*.os9)")
        if path:
            self.run_task("Exporting OS-9 partition", lambda p, c: self.session.image.volume(idx).export(path, progress=p, cancel=c))

    def import_os9(self):
        idx = self.current[0]
        path, _ = QFileDialog.getOpenFileName(self, "Replace selected OS-9 volume (exact size required)", "", "OS-9 volumes (*.os9 *.dsk *.raw);;All files (*)")
        if path and TextDialog("Replace OS-9 volume", f"Replace all logical sectors in partition {idx} with:\n{path}\n\nThe imported volume must match the stored partition size. Card padding is retained.", self, "Replace volume in working image").exec() == QDialog.DialogCode.Accepted:
            self.run_task("Replacing OS-9 volume", lambda p, c: self.session.image.import_os9(idx, path, p, c), lambda _: self.edited(f"Replaced OS-9 volume {idx} from {Path(path).name}"))

    def search_disks(self):
        term, ok = QInputDialog.getText(self, "Search Disk BASIC banks", "Filename contains (case insensitive)", text=self.search.text())
        if not ok or not term:
            return

        def search(progress, cancel):
            matches, skipped = [], []
            slots = [(p, d) for p in self.session.image.layout().partitions if p.kind == 2 for d in range(p.disks)]
            for n, (p, d) in enumerate(slots):
                checkpoint(cancel)
                try:
                    fs = DECB(self.session.image.volume(p.index, d))
                    matches.extend(f"{p.index}: {p.name} / disk {d:03d} / {f.name} ({f.size:,} bytes)" for f in fs.files if term.casefold() in f.name.casefold())
                except DrivePakError:
                    skipped.append(f"{p.name}/{d}")
                report(progress, n + 1, len(slots), "Searching disk filenames")
            return "\n".join(matches or ["No matching filenames."]) + f"\n\n{len(skipped)} unrecognised disks skipped." + ("\n" + ", ".join(skipped[:30]) if skipped else "")
        self.run_task("Searching banks", search, lambda text: TextDialog("Search results", text, self).exec())

    def choose_device(self, title):
        dialog = DeviceDialog(title, self)
        return dialog.selected() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def backup_destination(self, device, source=None):
        base = self.settings.value("backup_dir", str(Path.home() / "DrivePak-backups"))
        prefix = f"BACKUP-before-{safe_name(Path(source).stem)}-" if source else "CARD-BACKUP-"
        default = Path(base) / f"{prefix}{safe_name(device.model)}-{datetime.now():%Y%m%d-%H%M%S}.img"
        title = f"Save BACKUP of {device.path}'s current contents before writing {Path(source).name}" if source else f"Save complete BACKUP of {device.path} on independent storage"
        path, _ = QFileDialog.getSaveFileName(self, title, str(default), "Complete card backup (*.img)", options=QFileDialog.Option.DontConfirmOverwrite)
        if path:
            self.settings.setValue("backup_dir", str(Path(path).parent))
        return path

    def backup_action(self):
        if not self.can_replace_session():
            return
        device = self.choose_device("Select card to back up and open")
        if not device:
            return
        destination = self.backup_destination(device)
        if not destination:
            return
        work = self.work_dir

        def backup(progress, cancel):
            backup_card(device, destination, work, progress, cancel)
            return Session.open(destination, work, progress, cancel)

        def done(session):
            self.backup_status = f"Verified complete backup: {destination} · {device.path} · {device.serial or device.diskseq}"
            self.adopt(session)
        self.run_task("Backing up the complete card, then preparing a working image", backup, done)

    def verify_action(self):
        path, _ = QFileDialog.getOpenFileName(self, "Verify a backup and its .json manifest", self.settings.value("backup_dir", ""), "Complete raw backup (*.img);;All files (*)")
        if path:
            self.run_task("Verifying backup", lambda p, c: verify_backup(path, p, c), lambda m: TextDialog("Backup verified", json.dumps(m, indent=2) + "\n\nThis verifies the saved backup. Correspondence with a current card is checked again before any write.", self).exec())

    def review_text(self):
        s = self.session
        return f"Original (unchanged): {s.original}\nOriginal SHA-256: {s.original_digest}\nWorking image: {s.working}\nSaved image: {s.saved_as or 'Not saved under a new name'}\n{self.backup_status}\n{self.written_status}\n\nRecorded working changes:\n" + "\n".join(s.changes or ["No edits recorded in this session."])

    def review_changes(self):
        TextDialog("Review working image", self.review_text(), self).exec()

    def apply_changes_action(self):
        if self.worker or not self.session or not self.session.changes:
            return
        session = self.session
        device = self.choose_device("Select destination for changed sectors only")
        if not device:
            return
        name = next(tempfile._get_candidate_names())
        work = self.work_dir
        baseline = work / f"{name}.card-checkpoint.img"
        journal = work / f"{name}.card-edits.dpp"
        def prepare(progress, cancel):
            from .devices import validate
            validate(device.identity, [session.original, session.working, work])
            state = fingerprint(session.image.file)
            copy_verified(session.working, baseline, progress, cancel)
            info = prepare_changes(session.original, baseline, journal, session.original_digest, progress, cancel)
            return info, state
        def ready(result):
            info, state = result
            if not info["sectors"]:
                TextDialog("No changed sectors", "The working image is byte-for-byte unchanged. No card access is needed.", self).exec()
                return
            text = (f"DESTINATION CARD: {device.path}\n{device.model} · {device.serial or device.wwn or device.diskseq}\nActual capacity: {device.capacity:,} bytes\n\n"
                    f"Editor original: {session.original}\nPrepared image: {baseline}\nChanged physical sectors: {info['sectors']:,} ({info['bytes_changed']:,} bytes)\n\n"
                    f"Recovery journal: {journal}\nThe journal stores before/after bytes for affected sectors only. It is NOT a complete card backup. Keep any full backup you already have.\n\n"
                    "The card's entire image-sized range must match the editor original before any edit is written. Only changed sectors will be overwritten and independently read back; other regions and larger-card tails stay intact.\n\n"
                    "This is a non-atomic in-place update. Interruption can leave partial edits. Recovery uses the journal and its .json receipt.\n\n" + self.review_text())
            if TextDialog("Final confirmation — apply image edits", text, self, f"Apply {info['sectors']:,} changed sectors to {device.path}").exec() != QDialog.DialogCode.Accepted:
                return
            def apply(progress, cancel):
                if fingerprint(session.image.file) != state:
                    raise DrivePakError("Working image changed after review. No card edits started; prepare the changes again.")
                return apply_card_changes(device, journal, str(journal) + ".json", progress, cancel,
                                          expected_journal=info["journal_sha256"])
            def applied(receipt):
                session.original, session.original_digest = baseline, info["modified_sha256"]
                session.changes, session.saved_as = [], str(baseline)
                self.written_status = f"Card edits readback verified: {receipt['bytes_written']:,} bytes → {device.path}"
                self.backup_status = f"Changed-sector recovery journal (not a full backup): {journal}"
                self.refresh()
                TextDialog("Card edits verified", json.dumps(receipt, indent=2) + f"\n\nKeep the recovery journal and its manifest:\n{journal}", self).exec()
            def failed(message):
                self.fail(f"{message}\n\nPrepared image retained: {baseline}\nEdit journal retained: {journal}\nIf a verified .json recovery receipt exists, Recover card edits can restore the journaled before sectors. A complete backup is needed if sectors no longer match either version.")
            self.run_task("Applying only changed card sectors", apply, applied, failed)
        self.run_task("Preparing immutable image checkpoint and edit recovery journal", prepare, ready)

    def recover_edits_action(self):
        if self.worker:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Select card-edit recovery journal and its .json receipt", str(self.work_dir), "Card edit journal (*.dpp)")
        if not path:
            return
        device = self.choose_device("Select exact card for edit recovery")
        if not device:
            return
        text = (f"RESTORE JOURNALED SECTORS ON: {device.path}\n{device.model} · {device.serial or device.wwn or device.diskseq}\nCapacity: {device.capacity:,} bytes\n\nJournal: {path}\n\n"
                "Restore the before bytes of all journaled sectors. The complete image-sized card range is checked first: affected sectors must match their before or after bytes, and all other sectors must match the original. Later unrelated changes or unknown contents are rejected before writing. This is not a full-card restoration.\n\nRecovery is non-atomic; keep the journal and complete backups. Written sectors are independently read back.")
        if TextDialog("Final confirmation — recover card edits", text, self, f"Recover journaled sectors on {device.path}").exec() != QDialog.DialogCode.Accepted:
            return
        receipt_path = self.work_dir / f"{next(tempfile._get_candidate_names())}.edit-recovery.json"
        def done(result):
            self.written_status = f"Card edit recovery readback verified: {device.path}"
            self.refresh()
            TextDialog("Card edit recovery verified", json.dumps(result, indent=2), self).exec()
        self.run_task("Restoring and verifying journaled card sectors", lambda p, c: apply_card_changes(device, path, receipt_path, p, c, recovery=True), done)

    def write_action(self):
        if self.worker:
            return
        default = ""
        if self.session:
            default = self.session.saved_as or (self.session.working if self.session.changes else self.session.original)
        options = WriteOptionsDialog(default, self)
        if options.exec() != QDialog.DialogCode.Accepted:
            return
        source = Path(options.source.text()).resolve()
        try:
            selected_stat = source.stat()
            selected_identity = (selected_stat.st_dev, selected_stat.st_ino, selected_stat.st_size, selected_stat.st_mtime_ns, selected_stat.st_ctime_ns)
            size = selected_stat.st_size
            if not source.is_file() or not size or size % 512:
                raise FormatError("Select an existing, nonempty raw image with a length divisible by 512 bytes.")
        except (OSError, DrivePakError) as e:
            self.fail(str(e))
            return
        device = self.choose_device("Select exact destination for image write")
        if not device:
            return
        if size > device.capacity:
            self.fail("The selected card is smaller than this image. The application never truncates an image to fit.")
            return
        destination = None
        if options.backup_first.isChecked():
            destination = self.backup_destination(device, source=source)
            if not destination:
                return
            try:
                distinct(source, destination)
            except DrivePakError:
                self.fail("The backup must have a different filename from the SOURCE image. Choose a new backup name; the source image will be read to write the card.")
                return
        def ready(_):
            backup_text = f"Verified current-card backup:\n{destination}" if destination else "DIRECT WRITE — NO RECOVERY BACKUP WILL BE CREATED.\nExisting card contents will be overwritten. Recovery will require an existing backup or another known-good image."
            text = (f"READ SOURCE IMAGE: {source}\n\nWRITE DESTINATION CARD: {device.path}\nModel: {device.model}\nSerial / stable ID: {device.serial or device.wwn or device.diskseq}\nActual capacity: {device.capacity:,} bytes\n\n{backup_text}\n\nWrite range: {size:,} bytes from offset zero.\nBytes after that range: {device.capacity-size:,}, preserved. Partitions do not expand.\n\nWriting is not atomic. Interruption can leave incomplete contents. Independent readback verification is required.")
            if TextDialog("Final confirmation — image → SD card", text, self, f"Write {source.name} to {device.path} and verify").exec() != QDialog.DialogCode.Accepted:
                return

            def write(progress, cancel):
                current = source.stat()
                if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != selected_identity:
                    raise DrivePakError("The source image changed after selection. Select it again and review the new write range. No card write started.")
                if destination:
                    return write_card(device, source, destination, progress, cancel)
                return write_card_direct(device, source, progress, cancel, acknowledge_no_backup=True)

            def written(result):
                self.written_status = f"Card write readback verified: {source.name} → {device.path} at {result['verified_utc']}"
                self.backup_status = f"Recovery backup retained: {destination}" if destination else "Direct write: no recovery backup created"
                self.refresh()
                TextDialog("Card write verified", json.dumps(result, indent=2) + f"\n\n{self.backup_status}", self).exec()
            self.run_task("Writing card and independently reading it back", write, written)
        if destination:
            work = self.work_dir if self.session else None
            self.run_task("Creating and verifying the selected card's safety backup", lambda p, c: backup_card(device, destination, work, p, c), ready)
        else:
            ready(None)

    def choose_work_dir(self):
        directory = QFileDialog.getExistingDirectory(self, "Working image storage (independent of target cards)", str(self.work_dir))
        if directory:
            self.settings.setValue("work_dir", directory)

    def save_diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save read-only layout diagnostics", "drivepak-diagnostics.json", "JSON (*.json)")
        if path:
            data = {"technical_details": self.details.toPlainText(), "session": self.review_text(), "first_128_bytes": self.session.image.read(0, min(128, self.session.image.size)).hex(" ")}
            self.run_task("Saving diagnostics", lambda p, c: write_json(path, data))

    def guide(self):
        TextDialog("DrivePak Manager quick start", "Open image: selects a raw card image, verifies a separate working copy, then enables editing. Expand a partition and select a numbered disk.\n\nBack up card / Open card: select an external reader, deliberately unmount its volumes if needed, then choose independent backup storage. Every physical byte is copied and independently verified before a working image opens.\n\nImport disk(s): choose raw sector images, review each destination bank/slot and choose how a smaller disk fills the unused logical tail. Export disk(s): select one or several disk rows and choose an explicit size. The original floppy length is not stored.\n\nFiles menu: extract exact bytes plus metadata sidecars; insert, rename and delete files in validated standard Disk BASIC disks. OS-9 supports browsing/extraction and whole-volume import/export. Double-click an OS-9 directory; use Up to return.\n\nPartitions menu: add empty BASIC banks or complete OS-9 volumes; rename/delete entries and assign defaults. Deleting an entry leaves data intact and lets you reassign affected defaults.\n\nApply image edits to card (Card menu): prepare an immutable checkpoint and changed-sector recovery journal, select the exact card, review one final confirmation, then compare the full image-sized original range, write only changed sectors and independently read them back. A mismatched card is rejected. Keep the .dpp journal and its .json receipt; Recover card edits restores its before sectors if they still match either before/after bytes. A journal is not a complete card backup. This workflow uses a working image; it does not browse a physical card on demand.\n\nSave image as: saves and verifies a new image. Discard changes recreates the working copy from its unchanged original. Working files remain available after closing the app; their paths are shown above.\n\nWrite image to card: choose the source image file to READ, then select the destination SD card to WRITE. A fresh verified backup is enabled by default. Clear the backup checkbox for an explicit direct write without creating a backup; no Save dialog appears in direct mode. Review one final confirmation, authenticate the helper, and wait for independent device readback verification. Bytes beyond a shorter image are preserved.\n\nRecovery after an interrupted write: keep the earlier backup. Open that backup as an image; choose Write image to card. Take a new backup of the selected card's present state, then explicitly confirm restoring the earlier image. Never delete the only recoverable snapshot.\n\nUnsupported layouts still allow complete backup, saved copies and diagnostics. Normal development uses simulated devices; spare-card hardware validation is a separate manual step.", self).exec()

    def closeEvent(self, event):
        if self.worker:
            self.fail("An operation is running. Use Cancel operation and wait for its result before closing. A card write may require recovery after cancellation.")
            event.ignore()
            return
        if not self.can_replace_session():
            event.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        if self.session:
            self.session.close()
        event.accept()


def main():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        print("Launch DrivePak Manager as your normal desktop user. Install the helper for authenticated card access.", file=sys.stderr)
        return 1
    app = QApplication(sys.argv)
    app.setApplicationName("DrivePak Manager")
    app.setApplicationVersion(__version__)
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        window.open_path(sys.argv[1])
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
