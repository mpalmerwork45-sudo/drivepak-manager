"""Independent small fixtures; no downloaded content in normal tests."""
import os
from pathlib import Path
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest


@pytest.fixture(autouse=True)
def isolated_preferences(tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    import drivepak_manager.gui as gui
    settings = tmp_path / "preferences/drivepak.ini"
    monkeypatch.setattr(gui, "QSettings", lambda *args: QSettings(str(settings), QSettings.Format.IniFormat))


def put24(b, offset, n):
    b[offset:offset + 3] = bytes([(n >> 16) & 255, (n >> 8) & 255, n & 255])


def synthetic_card(path, slots=2, start=1024, table=3):
    # Explicit byte layout, intentionally independent of the production creator.
    b = bytearray(b"\xa5" * ((start + slots * 2880 + 2048) * 512))
    b[:256] = bytes(256)
    b[:8] = b"TESTCARD"
    b[96:112] = b"unknown-header!!"
    put24(b, 17, table)
    put24(b, 21, start)
    b[table * 512] = 0  # deleted entry before a live entry
    e = (table + 1) * 512
    b[e] = 2
    b[e + 1:e + 17] = b"GAMES          \0"
    put24(b, e + 32, start)
    put24(b, e + 35, slots * 2880)
    b[(table + 2) * 512] = 0xFE
    for d in range(slots):
        for s in range(2880):
            off = (start + d * 2880 + s) * 512
            b[off:off + 256] = bytes(256)
            b[off + 256:off + 512] = bytes([(s + 19 * d) % 251]) * 256
        off = (start + d * 2880 + 307) * 512
        b[off:off + 68] = b"\xff" * 68
        for s in range(308, 317):
            off = (start + d * 2880 + s) * 512
            b[off:off + 256] = b"\xff" * 256
    path.write_bytes(b)
    return path


@pytest.fixture
def card(tmp_path):
    return synthetic_card(tmp_path / "card.img")


@pytest.fixture
def os9_volume(tmp_path):
    b = bytearray(32 * 256)
    put24(b, 0, 32)
    b[3] = 18
    b[4:6] = (4).to_bytes(2, "big")
    b[6:8] = (1).to_bytes(2, "big")
    put24(b, 8, 3)
    b[31:35] = b"TES\xd4"
    root = 3 * 256
    b[root] = 0xBF
    b[root + 9:root + 13] = (96).to_bytes(4, "big")
    put24(b, root + 16, 4)
    b[root + 19:root + 21] = (1).to_bytes(2, "big")
    for idx, (name, lsn) in enumerate(((b"\xae", 3), (b".\xae", 3), (b"HELL\xcf", 5))):
        off = 4 * 256 + idx * 32
        b[off:off + len(name)] = name
        put24(b, off + 29, lsn)
    fd = 5 * 256
    b[fd] = 0x1B
    b[fd + 3:fd + 8] = bytes([126, 10, 31, 9, 30])
    content = b"OS9\x00\xff\x0d\x0aBINARY"
    b[fd + 9:fd + 13] = len(content).to_bytes(4, "big")
    put24(b, fd + 16, 6)
    b[fd + 19:fd + 21] = (1).to_bytes(2, "big")
    b[6 * 256:6 * 256 + len(content)] = content
    p = tmp_path / "test.os9"
    p.write_bytes(b)
    return p
