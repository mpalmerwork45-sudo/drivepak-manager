"""Classic OS-9/6809 RBF browsing and validated, transactional file editing."""
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat

from ..errors import FormatError, SafetyError
from ..format import u24, p24
from ..streams import (atomic_output, checkpoint, distinct, fingerprint, hash_stream,
                       need_space, read_exact, report, write_all, write_json)


def os9_name(raw):
    out = bytearray()
    for x in raw:
        c = x & 127
        if c < 32 or c == 127 or c in (47, 92):
            raise FormatError("Invalid OS-9 filename encoding.")
        out.append(c)
        if x & 128:
            return out.decode("ascii")
    raise FormatError("OS-9 name lacks a high-bit terminator.")


class RawVolume:
    def __init__(self, path):
        if not stat.S_ISREG(Path(path).stat().st_mode):
            raise FormatError("OS-9 imports require regular raw volume files, not devices.")
        self.file = Path(path).open("rb", buffering=0)
        self.size = Path(path).stat().st_size
        self.sectors = self.size // 256
        if not self.size or self.size % 256:
            self.close()
            raise FormatError("OS-9 volume must contain raw 256-byte sectors, with no header.")

    def read(self, offset, length):
        if offset < 0 or length < 0 or offset + length > self.size:
            raise FormatError("OS-9 read exceeds the volume.")
        self.file.seek(offset)
        return read_exact(self.file, length)

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


@dataclass(frozen=True)
class OS9File:
    name: str
    lsn: int
    size: int
    attributes: int
    raw_fd: bytes
    segments: tuple[tuple[int, int], ...]

    @property
    def is_dir(self):
        return bool(self.attributes & 128)


class OS9:
    def __init__(self, view):
        self.view = view
        h = view.read(0, 256)
        self.header = h
        self.sectors = u24(h[:3])
        self.root = u24(h[8:11])
        if not 0 < self.sectors <= view.sectors or not 0 < self.root < self.sectors:
            raise FormatError(f"OS-9 declares {self.sectors:,} sectors in a {view.sectors:,}-sector extent, or has an invalid root pointer. Browsing is disabled; full-partition export remains available.")
        cluster = int.from_bytes(h[6:8], "big")
        bitmap = int.from_bytes(h[4:6], "big")
        if not cluster or not bitmap or bitmap > (self.sectors - 1) * 256:
            raise FormatError("Invalid OS-9 allocation map parameters.")
        # Classic 6809 does not define the 68k extended-sector fields. A sync
        # signature or explicit non-256 size identifies an unsupported variant.
        if h[0x60:0x64] == bytes.fromhex("4372757a"):
            raise FormatError("OS-9/68k extended-sector volumes are unsupported.")
        if h[0x68] not in (0, 1):
            raise FormatError("OS-9 volume declares non-256-byte sectors; this layout is unsupported.")
        self.name = os9_name(h[31:63])
        if not self.descriptor(self.root, "/").is_dir:
            raise FormatError("OS-9 root descriptor is not a directory.")

    def sector(self, lsn):
        if not 0 <= lsn < self.sectors:
            raise FormatError("OS-9 sector pointer is outside the declared volume.")
        return self.view.read(lsn * 256, 256)

    def descriptor(self, lsn, name=""):
        b = self.sector(lsn)
        size = int.from_bytes(b[9:13], "big")
        segments = []
        ended = False
        for pos in range(16, 256, 5):
            start, count = u24(b[pos:pos + 3]), int.from_bytes(b[pos + 3:pos + 5], "big")
            if start == count == 0:
                ended = True
                continue
            if ended or not count or not start or start + count > self.sectors or start <= lsn < start + count:
                raise FormatError(f"Invalid OS-9 segment in {name or lsn}.")
            if any(start < a + n and start + count > a for a, n in segments):
                raise FormatError("Overlapping OS-9 file segments.")
            segments.append((start, count))
        if size > sum(n for _, n in segments) * 256:
            raise FormatError("OS-9 file size exceeds its allocated segments.")
        return OS9File(name, lsn, size, b[0], b, tuple(segments))

    def chunks(self, f, cancel=None):
        remaining = f.size
        for start, sectors in f.segments:
            n = min(remaining, sectors * 256)
            for offset in range(0, n, 65536):
                checkpoint(cancel)
                size = min(65536, n - offset)
                yield self.view.read(start * 256 + offset, size)
            remaining -= n
            if remaining == 0:
                return

    def resolve(self, path="/"):
        if not str(path).startswith("/") or ".." in PurePosixPath(path).parts:
            raise FormatError("Use an absolute OS-9 path without '..'.")
        current = self.descriptor(self.root, "/")
        parts = PurePosixPath(path).parts
        for name in parts:
            if name in ("/", "."):
                continue
            if name == "..":
                raise FormatError("Use an absolute OS-9 path without '..'.")
            matches = [x for x in self._entries(current) if x.name.casefold() == name.casefold()]
            if len(matches) != 1:
                raise FormatError(f"OS-9 path not found or ambiguous: {path}")
            current = matches[0]
        return current

    def _entries(self, directory):
        if not directory.is_dir or directory.size % 32 or directory.size > 16 * 1024**2:
            raise FormatError("Invalid or excessively large OS-9 directory.")
        result, seen = [], set()
        for chunk in self.chunks(directory):
            for pos in range(0, len(chunk), 32):
                b = chunk[pos:pos + 32]
                if b[0] == 0:
                    continue
                name = os9_name(b[:29])
                if name in (".", ".."):
                    continue
                if name.casefold() in seen:
                    raise FormatError("Duplicate OS-9 directory name.")
                seen.add(name.casefold())
                result.append(self.descriptor(u24(b[29:32]), name))
        return result

    def list_dir(self, path="/"):
        return self._entries(self.resolve(path))

    def extract(self, path, destination, progress=None, cancel=None):
        f = self.resolve(path)
        if f.is_dir:
            raise FormatError("Select an OS-9 file for extraction. Whole-volume export includes all directories.")
        meta = Path(str(destination) + ".drivepak.json")
        if meta.exists():
            raise FormatError("Metadata sidecar already exists. Choose a different output.")
        digest = hashlib.sha256()
        with atomic_output(destination) as out:
            done = 0
            for b in self.chunks(f, cancel):
                write_all(out, b)
                digest.update(b)
                done += len(b)
                report(progress, done, f.size, "Extracting OS-9 file")
        write_json(meta, {"format": "OS-9 RBF file", "path": path, "bytes": f.size,
                          "sha256": digest.hexdigest(),
                          "attributes": f.attributes, "descriptor": f.raw_fd.hex(),
                          "notice": "Content is verbatim; original owner, dates, attributes and segment metadata are recorded here."})

    @staticmethod
    def encoded_name(name):
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,28}", name):
            raise FormatError("Use an OS-9 name of 1–29 ASCII characters: start with a letter or underscore, then letters, digits, '.', '_' or '-'.")
        b = bytearray(name.encode("ascii"))
        b[-1] |= 128
        return bytes(b).ljust(29, b"\0")

    def _directory_records(self, directory):
        if not directory.is_dir or directory.size % 32 or directory.size > 16 * 1024**2:
            raise FormatError("Invalid or excessively large OS-9 directory.")
        remaining = directory.size
        for start, count in directory.segments:
            length = min(remaining, count * 256)
            for pos in range(0, length, 65536):
                raw = self.view.read(start * 256 + pos, min(65536, length - pos))
                for at in range(0, len(raw), 32):
                    yield start * 256 + pos + at, raw[at:at + 32]
            remaining -= length
            if not remaining:
                return

    def validate_for_edit(self, progress=None, cancel=None):
        """Validate every reachable owner before trusting free bitmap bits.

        Unreferenced allocated sectors can be boot/reserved/bad sectors. They
        remain allocated; this validator never performs filesystem repair.
        """
        h = self.header
        if any(h[0x64:0x68]) or h[0x69]:
            raise FormatError("OS-9 allocation-map/sector-size extensions are ambiguous for classic file editing.")
        cluster, map_bytes = int.from_bytes(h[6:8], "big"), int.from_bytes(h[4:6], "big")
        if cluster != 1:
            raise FormatError("OS-9 file editing requires one-sector allocation clusters; this volume remains available for browsing and whole-volume transfer.")
        if map_bytes * 8 < self.sectors:
            raise FormatError("OS-9 allocation bitmap is too short for the declared volume.")
        # Classic CoCo header extensions may contain disk-driver information.
        # Only the known extended-format sync signature is interpreted above.
        bitmap = bytearray(self.view.read(256, map_bytes))
        owned = bytearray(self.sectors)  # <= 65535 * 8 with this supported profile
        def claim(start, count):
            if not 0 <= start < start + count <= self.sectors:
                raise FormatError("OS-9 allocation extent is outside the volume.")
            if any(owned[start:start + count]):
                raise FormatError("OS-9 cross-linked data, descriptors or reserved sectors; file editing disabled.")
            for n in range(start, start + count):
                if not bitmap[n // 8] & (128 >> (n % 8)):
                    raise FormatError(f"OS-9 referenced sector {n} is marked free in its allocation bitmap.")
            owned[start:start + count] = b"\1" * count
        claim(0, 1 + (map_bytes + 255) // 256)
        boot_start, boot_bytes = u24(h[21:24]), int.from_bytes(h[24:26], "big")
        boot_count = (boot_bytes + 255) // 256
        if bool(boot_start) != bool(boot_bytes) or boot_start + boot_count > self.sectors:
            raise FormatError("OS-9 boot extent is ambiguous or outside the volume.")
        for n in range(boot_start, boot_start + boot_count):
            if owned[n] or not bitmap[n // 8] & (128 >> (n % 8)):
                raise FormatError("OS-9 boot extent overlaps reserved metadata or is marked free.")
        todo, visited = [(self.root, self.root, "/")], set()
        while todo:
            checkpoint(cancel)
            lsn, parent, label = todo.pop()
            if lsn in visited:
                raise FormatError("OS-9 linked descriptors or directory cycles are unsupported for file editing.")
            visited.add(lsn)
            if len(visited) > 65536:
                raise FormatError("OS-9 write validation is limited to 65,536 reachable descriptors.")
            f = self.descriptor(lsn, label)
            if f.raw_fd[8] != 1:
                raise FormatError("OS-9 descriptor link count must be one for file editing.")
            if boot_start <= lsn < boot_start + boot_count:
                raise FormatError("OS-9 file descriptor overlaps the boot data.")
            claim(lsn, 1)
            for start, count in f.segments:
                claim(start, count)
                if f.is_dir and start < boot_start + boot_count and boot_start < start + count:
                    raise FormatError("OS-9 directory overlaps the boot data.")
            if f.is_dir:
                seen, dots = set(), {}
                for _, entry in self._directory_records(f):
                    checkpoint(cancel)
                    if not entry[0]:
                        continue
                    name, child = os9_name(entry[:29]), u24(entry[29:32])
                    if name.casefold() in seen:
                        raise FormatError("Duplicate OS-9 directory name (case insensitive).")
                    seen.add(name.casefold())
                    if name in (".", ".."):
                        dots[name] = child
                    else:
                        todo.append((child, lsn, name))
                if dots != {".": lsn, "..": parent}:
                    raise FormatError("OS-9 directory '.' or '..' reference is missing or inconsistent.")
            if len(visited) % 128 == 0:
                report(progress, len(visited), 0, "Checking OS-9 allocation and directories")
        self.bitmap = bitmap
        self.boot_range = (boot_start, boot_count)
        free = sum(not bitmap[n // 8] & (128 >> (n % 8)) for n in range(self.sectors))
        return {"free_bytes": free * 256, "files_checked": len(visited)}

    def _mutable(self, progress=None, cancel=None):
        image = getattr(self.view, "image", None)
        if image is None or not image.writable:
            raise SafetyError("OS-9 edits require a separate writable working image.")
        image.layout().require_editable()
        self.__init__(self.view)  # never reuse stale allocation after an earlier edit
        self.validate_for_edit(progress, cancel)

    def _file_entry(self, path):
        if not str(path).startswith("/") or ".." in PurePosixPath(path).parts:
            raise FormatError("Use an absolute OS-9 file path without '..'.")
        path = PurePosixPath(path)
        parent = self.resolve(str(path.parent))
        matches = [(offset, b) for offset, b in self._directory_records(parent)
                   if b[0] and os9_name(b[:29]).casefold() == path.name.casefold()]
        if len(matches) != 1:
            raise FormatError(f"OS-9 file not found or ambiguous: {path}")
        offset, entry = matches[0]
        f = self.descriptor(u24(entry[29:32]), os9_name(entry[:29]))
        if f.is_dir:
            raise FormatError("Select a regular OS-9 file. Directory modification is not supported.")
        return parent, offset, f

    def _protect_boot(self, f):
        start, count = self.boot_range
        if count and any(a < start + count and start < a + n for a, n in f.segments):
            raise FormatError("This file contains boot data referenced by the OS-9 header. Use a prepared whole-volume image to change boot files.")

    @staticmethod
    def _bit(bitmap, lsn, used):
        mask = 128 >> (lsn % 8)
        if used:
            bitmap[lsn // 8] |= mask
        else:
            bitmap[lsn // 8] &= ~mask

    def _allocate(self, bitmap, count, cancel=None):
        segments = []
        for n in range(self.sectors):
            if not count:
                break
            if n % 2048 == 0:
                checkpoint(cancel)
            if bitmap[n // 8] & (128 >> (n % 8)):
                continue
            self._bit(bitmap, n, True)
            # Keep each allocated run within a 6x09 bitmap-sector boundary.
            if segments and segments[-1][0] + segments[-1][1] == n and n % 2048:
                a, length = segments[-1]
                segments[-1] = (a, length + 1)
            else:
                segments.append((n, 1))
            count -= 1
        if count:
            raise FormatError("Not enough free OS-9 sectors for this file and directory metadata.")
        if len(segments) > 48:
            raise FormatError("OS-9 allocation needs more than 48 segments; the free space is too fragmented or the file too large for this allocation profile.")
        return segments

    @staticmethod
    def _fd_bytes(raw, size, segments):
        if len(segments) > 48:
            raise FormatError("OS-9 directory/file needs more than 48 segments.")
        result = bytearray(raw)
        result[9:13] = size.to_bytes(4, "big")
        result[16:] = bytes(240)
        for i, (start, count) in enumerate(segments):
            result[16 + i * 5:21 + i * 5] = p24(start) + count.to_bytes(2, "big")
        return result

    def _new_entry(self, directory, bitmap, cancel):
        for offset, raw in self._directory_records(directory):
            if not raw[0]:
                return offset, None
        segments = list(directory.segments)
        capacity = sum(n for _, n in segments) * 256
        if directory.size == capacity:
            extra = self._allocate(bitmap, 1, cancel)
            if (segments and segments[-1][0] + segments[-1][1] == extra[0][0]
                    and extra[0][0] % 2048 and segments[-1][1] < 65535):
                a, n = segments[-1]
                segments[-1] = (a, n + 1)
            else:
                segments += extra
        remaining = directory.size
        for start, count in segments:
            if remaining < count * 256:
                return start * 256 + remaining, self._fd_bytes(directory.raw_fd, directory.size + 32, segments)
            remaining -= count * 256
        raise FormatError("Cannot extend OS-9 directory.")

    def insert(self, source, directory="/", name=None, replace=False, attributes=None,
               owner=None, progress=None, cancel=None):
        self._mutable(progress, cancel)
        source = Path(source)
        distinct(self.view.image.path, source)
        if not stat.S_ISREG(source.stat().st_mode):
            raise SafetyError("OS-9 file insertion requires a regular source file.")
        parent = self.resolve(directory)
        if not parent.is_dir:
            raise FormatError("Select an OS-9 directory for insertion.")
        with source.open("rb", buffering=0) as inp:
            initial = fingerprint(inp)
            size = initial[2]
            if size > self.sectors * 256 or size > 0xFFFFFFFF:
                raise FormatError("File is too large for this OS-9 volume.")
            digest = hash_stream(inp, size, progress, cancel, "Checking OS-9 import source")
            metadata = None
            sidecar = Path(str(source) + ".drivepak.json")
            if sidecar.exists():
                try:
                    if sidecar.stat().st_size > 16384:
                        raise ValueError("oversized metadata")
                    meta = json.loads(sidecar.read_text())
                    metadata = bytes.fromhex(meta["descriptor"])
                    if meta["format"] != "OS-9 RBF file" or meta["bytes"] != size or meta["sha256"] != digest:
                        raise ValueError("content checksum/length does not match")
                    if len(metadata) != 256 or metadata[0] & 128 or metadata[8] != 1:
                        raise ValueError("not a supported regular-file descriptor")
                    name = PurePosixPath(meta["path"]).name if name is None else name
                except (KeyError, TypeError, ValueError) as e:
                    raise FormatError(f"Invalid or unverified OS-9 extraction sidecar: {e}. Preserve it separately if intentionally importing bytes with new metadata.") from e
            name = source.name if name is None else name
            encoded = self.encoded_name(name)
            existing = next((f for f in self._entries(parent) if f.name.casefold() == name.casefold()), None)
            bitmap = bytearray(self.bitmap)
            if existing:
                if not replace:
                    raise FormatError("A file with that name already exists. Enable replacement explicitly.")
                _, entry_offset, existing = self._file_entry(directory.rstrip("/") + "/" + existing.name)
                self._protect_boot(existing)
                fd_lsn, raw = existing.lsn, bytearray(existing.raw_fd)
                for start, count in existing.segments:
                    for n in range(start, start + count):
                        self._bit(bitmap, n, False)
                directory_fd = None
            else:
                fd_lsn = self._allocate(bitmap, 1, cancel)[0][0]
                raw = bytearray(256)
                now = datetime.now()
                if not 0 <= now.year - 1900 <= 255:
                    raise FormatError("Current date does not fit OS-9's year-since-1900 byte.")
                raw[0], raw[8] = 0x1B, 1
                raw[3:8] = bytes((now.year - 1900, now.month, now.day, now.hour, now.minute))
                raw[13:16] = raw[3:6]
            if metadata is not None:
                raw[:8], raw[13:16] = metadata[:8], metadata[13:16]
            if attributes is not None:
                if type(attributes) is not int or not 0 <= attributes < 128:
                    raise FormatError("OS-9 regular-file attributes must be an integer from 0 to 127.")
                raw[0] = attributes
            if owner is not None:
                if type(owner) is not int or not 0 <= owner <= 65535:
                    raise FormatError("OS-9 owner must be an integer from 0 to 65535.")
                raw[1:3] = owner.to_bytes(2, "big")
            segments = self._allocate(bitmap, (size + 255) // 256, cancel)
            if not existing:
                entry_offset, directory_fd = self._new_entry(parent, bitmap, cancel)
            fd = self._fd_bytes(raw, size, segments)
            if fingerprint(inp) != initial:
                raise SafetyError("OS-9 import source changed before writing.")
            need_space(self.view.image.path.parent, size * 2 + 131072)
            with self.view.image.transaction():
                inp.seek(0)
                written, copied = 0, hashlib.sha256()
                for start, count in segments:
                    length = min(count * 256, size - written)
                    for offset in range(0, length, 65536):
                        checkpoint(cancel)
                        piece = read_exact(inp, min(65536, length - offset))
                        self.view.write(start * 256 + offset, piece)
                        copied.update(piece)
                        written += len(piece)
                        report(progress, written, size, "Inserting OS-9 file")
                checkpoint(cancel)
                if copied.hexdigest() != digest or fingerprint(inp) != initial:
                    raise SafetyError("OS-9 import source changed during writing; changes rolled back.")
                self.view.write(fd_lsn * 256, fd)
                if directory_fd is not None:
                    self.view.write(parent.lsn * 256, directory_fd)
                self.view.write(entry_offset, encoded + p24(fd_lsn))
                self.view.write(256, bitmap)
                OS9(self.view).validate_for_edit(progress, cancel)

    def rename(self, path, new_name, progress=None, cancel=None):
        self._mutable(progress, cancel)
        parent, offset, f = self._file_entry(path)
        encoded = self.encoded_name(new_name)
        if any(x.lsn != f.lsn and x.name.casefold() == new_name.casefold() for x in self._entries(parent)):
            raise FormatError("An OS-9 file with the new name already exists.")
        with self.view.image.transaction():
            checkpoint(cancel)
            self.view.write(offset, encoded)
            OS9(self.view).validate_for_edit(progress, cancel)

    def delete(self, path, progress=None, cancel=None):
        self._mutable(progress, cancel)
        _, offset, f = self._file_entry(path)
        self._protect_boot(f)
        bitmap = bytearray(self.bitmap)
        self._bit(bitmap, f.lsn, False)
        for start, count in f.segments:
            for n in range(start, start + count):
                self._bit(bitmap, n, False)
        with self.view.image.transaction():
            checkpoint(cancel)
            self.view.write(offset, b"\0")
            self.view.write(256, bitmap)
            OS9(self.view).validate_for_edit(progress, cancel)
