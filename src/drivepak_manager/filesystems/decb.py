"""Disk BASIC 1.x: 35 tracks, 68 granules, 72 directory entries.

The 720 KiB DrivePak slot does not imply an extended BASIC filesystem. File
mutation is limited to validated standard metadata; remaining payload is retained.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from ..errors import FormatError, SafetyError
from ..format import SLOT_BYTES
from ..streams import atomic_output, write_all, write_json, distinct, fingerprint, read_exact

FAT_OFFSET = (17 * 18 + 1) * 256
DIR_OFFSET = (17 * 18 + 2) * 256
GRANULE_BYTES = 9 * 256


def granule_offset(number):
    if not 0 <= number < 68:
        raise FormatError("Granule index outside the standard 68-granule filesystem.")
    sector = number * 9 + (18 if number >= 34 else 0)
    return sector * 256


def disk_name(name):
    name = name.upper()
    if not re.fullmatch(r"[A-Z0-9!#$%&'()+,@^_{}~-]{1,8}(\.[A-Z0-9!#$%&'()+,@^_{}~-]{1,3})?", name):
        raise FormatError("Use a Disk BASIC 8.3 filename (letters, digits and simple punctuation).")
    base, _, ext = name.partition(".")
    return base.encode().ljust(8) + ext.encode().ljust(3)


def blank_disk():
    b = bytearray(SLOT_BYTES)
    b[FAT_OFFSET:FAT_OFFSET + 68] = b"\xff" * 68
    b[DIR_OFFSET:DIR_OFFSET + 9 * 256] = b"\xff" * (9 * 256)
    return bytes(b)


@dataclass(frozen=True)
class BasicFile:
    index: int
    name: str
    file_type: int
    ascii_flag: int
    size: int
    chain: tuple[int, ...]
    raw_entry: bytes


class DECB:
    def __init__(self, view):
        self.view = view
        if view.size < 161280:
            raise FormatError("Volume is shorter than a standard Disk BASIC floppy.")
        self.fat = bytearray(view.read(FAT_OFFSET, 68))
        self.directory = view.read(DIR_OFFSET, 9 * 256)
        self.files = []
        self.issues = []
        owned, names = set(), set()
        for idx in range(72):
            entry = self.directory[idx * 32:(idx + 1) * 32]
            if entry[0] == 255:
                break
            if entry[0] == 0:
                continue
            if any(x < 32 or x > 126 for x in entry[:11]):
                raise FormatError(f"Directory entry {idx} has an invalid filename; filesystem is not safely recognised.")
            base = entry[:8].decode().rstrip()
            ext = entry[8:11].decode().rstrip()
            name = base + ("." + ext if ext else "")
            if not base or name.upper() in names:
                raise FormatError("Empty/duplicate Disk BASIC filename; ambiguous directory.")
            names.add(name.upper())
            chain, sectors = self._chain(entry[13])
            if owned.intersection(chain):
                raise FormatError("Cross-linked granules detected. File operations are disabled.")
            owned.update(chain)
            last = int.from_bytes(entry[14:16], "big")
            if last > 256 or (sectors == 0 and last != 0):
                raise FormatError(f"Invalid final-sector byte count for {name}.")
            size = (len(chain) - 1) * GRANULE_BYTES + max(0, sectors - 1) * 256 + last
            if sectors == 0 and len(chain) != 1:
                raise FormatError("Noncanonical zero-sector final granule; mutation is unsafe.")
            if entry[11] > 3 or entry[12] not in (0, 255):
                self.issues.append(f"{name} has unsupported file type/format metadata.")
            self.files.append(BasicFile(idx, name, entry[11], entry[12], size, tuple(chain), entry))
        for i, value in enumerate(self.fat):
            if value != 255 and i not in owned:
                self.issues.append(f"Allocated granule {i} has no live directory owner.")

    def _chain(self, first):
        chain, current = [], first
        while True:
            if not 0 <= current < 68 or current in chain:
                raise FormatError("Invalid or cyclic Disk BASIC granule chain.")
            chain.append(current)
            value = self.fat[current]
            if 0xC0 <= value <= 0xC9:
                return chain, value & 15
            if value >= 68:
                raise FormatError("Granule chain refers to a free or invalid granule.")
            current = value

    @property
    def free_bytes(self):
        return self.fat.count(255) * GRANULE_BYTES

    def get(self, name):
        matches = [f for f in self.files if f.name.upper() == name.upper()]
        if not matches:
            raise FormatError(f"File not found: {name}")
        return matches[0]

    def read_file(self, name):
        f = self.get(name)
        b = b"".join(self.view.read(granule_offset(g), GRANULE_BYTES) for g in f.chain)
        return b[:f.size]

    def extract(self, name, destination):
        f = self.get(name)
        distinct(self.view.image.path, destination)
        meta = Path(str(destination) + ".drivepak.json")
        if Path(destination).exists() or meta.exists():
            raise SafetyError("Extraction output or its metadata sidecar already exists. Choose a new filename.")
        data = self.read_file(name)
        with atomic_output(destination) as out:
            write_all(out, data)
        write_json(meta, {"format": "Disk BASIC file", "name": f.name, "file_type": f.file_type,
                          "ascii_flag": f.ascii_flag, "directory_entry": f.raw_entry.hex(),
                          "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
                          "notice": "Bytes extracted verbatim; no text or binary header conversion."})

    def _mutable(self):
        state = fingerprint(self.view.image.file)
        self.view.image.layout().require_editable()
        # Another operation or filesystem object may have changed allocation.
        # Never allocate/free a granule using this object's old snapshot.
        self.__init__(self.view)
        if self.issues:
            raise FormatError("File editing disabled: " + "; ".join(self.issues[:5]))
        if fingerprint(self.view.image.file) != state:
            raise SafetyError("Working image changed during Disk BASIC validation. No file edits were written.")
        return state

    def insert(self, source, name=None, file_type=2, ascii_flag=0, replace=False):
        state = self._mutable()
        source = Path(source)
        distinct(self.view.image.path, source)
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(source, flags), "rb", buffering=0) as inp:
            initial = fingerprint(inp)
            if not stat.S_ISREG(os.fstat(inp.fileno()).st_mode) or initial[2] > 68 * GRANULE_BYTES:
                raise FormatError("Choose a regular file fitting the standard Disk BASIC filesystem.")
            data = read_exact(inp, initial[2])
            if fingerprint(inp) != initial:
                raise SafetyError("Disk BASIC import source changed while reading. No file edits were written.")
        reserved = None
        sidecar = Path(str(source) + ".drivepak.json")
        if sidecar.exists():
            try:
                if sidecar.stat().st_size > 16384:
                    raise ValueError("oversized metadata")
                meta = json.loads(sidecar.read_text())
                if meta["sha256"] != hashlib.sha256(data).hexdigest() or meta["bytes"] != len(data):
                    raise FormatError("File differs from its extraction metadata. Remove/update the sidecar or choose explicit metadata.")
                file_type, ascii_flag = meta["file_type"], meta["ascii_flag"]
                reserved = bytes.fromhex(meta["directory_entry"])[16:32]
                name = name or meta["name"]
            except (KeyError, ValueError, TypeError) as e:
                raise FormatError(f"Invalid extraction metadata: {e}") from e
        name = name or source.name
        encoded = disk_name(name)
        if file_type not in range(4) or ascii_flag not in (0, 255) or (reserved is not None and len(reserved) != 16):
            raise FormatError("Choose file type 0–3 and binary (0) or ASCII (255) metadata.")
        existing = next((f for f in self.files if f.name.upper() == name.upper()), None)
        if existing and not replace:
            raise FormatError("A file with that name already exists. Enable replacement after reviewing it.")
        fat = bytearray(self.fat)
        if existing:
            for g in existing.chain:
                fat[g] = 255
            idx = existing.index
        else:
            idx = next((i for i in range(72) if self.directory[i * 32] in (0, 255)), None)
        if idx is None:
            raise FormatError("All 72 directory entries are occupied.")
        if reserved is None:
            reserved = self.directory[idx * 32 + 16:idx * 32 + 32]
        needed = max(1, (len(data) + GRANULE_BYTES - 1) // GRANULE_BYTES)
        free = [g for g, value in enumerate(fat) if value == 255][:needed]
        if len(free) < needed:
            raise FormatError("Not enough free granules for this file.")
        for i, g in enumerate(free):
            fat[g] = free[i + 1] if i + 1 < needed else 0xC0 + ((len(data) - i * GRANULE_BYTES + 255) // 256)
        last_bytes = ((len(data) - 1) % 256 + 1) if data else 0
        entry = encoded + bytes([file_type, ascii_flag, free[0]]) + last_bytes.to_bytes(2, "big") + reserved
        with self.view.image.transaction():
            if fingerprint(self.view.image.file) != state:
                raise SafetyError("Working image changed while planning Disk BASIC insertion. Reopen the file list before editing.")
            for i, g in enumerate(free):
                # Only actual file bytes; retain unused sectors and slack bytes.
                piece = data[i * GRANULE_BYTES:(i + 1) * GRANULE_BYTES]
                if piece:
                    self.view.write(granule_offset(g), piece)
            self.view.write(FAT_OFFSET, fat)
            if self.directory[idx * 32] == 255 and idx < 71:
                # Preserve a directory terminator after inserting at end-of-list.
                self.view.write(DIR_OFFSET + (idx + 1) * 32, b"\xff")
            self.view.write(DIR_OFFSET + idx * 32, entry)
            DECB(self.view)._mutable()
        self.__init__(self.view)

    def rename(self, name, new_name):
        state = self._mutable()
        f = self.get(name)
        encoded = disk_name(new_name)
        if any(x.index != f.index and x.name.upper() == new_name.upper() for x in self.files):
            raise FormatError("A file with the new name already exists.")
        with self.view.image.transaction():
            if fingerprint(self.view.image.file) != state:
                raise SafetyError("Working image changed while planning Disk BASIC rename.")
            self.view.write(DIR_OFFSET + f.index * 32, encoded)
            DECB(self.view)._mutable()
        self.__init__(self.view)

    def delete(self, name):
        state = self._mutable()
        f = self.get(name)
        fat = bytearray(self.fat)
        for g in f.chain:
            fat[g] = 255
        with self.view.image.transaction():
            if fingerprint(self.view.image.file) != state:
                raise SafetyError("Working image changed while planning Disk BASIC deletion.")
            self.view.write(FAT_OFFSET, fat)
            self.view.write(DIR_OFFSET + f.index * 32, b"\0")
            DECB(self.view)._mutable()
        self.__init__(self.view)
