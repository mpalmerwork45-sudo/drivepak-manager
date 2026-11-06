"""Image sessions, reversible mutations and payload-only transfer operations."""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile
import threading

from .errors import FormatError, SafetyError
from .format import (BLOCK, PAYLOAD, SLOT_BYTES, SLOT_SECTORS, MAX_PARTITIONS,
                     DEFAULTS, inspect, encode_name, p24)
from .streams import (atomic_output, checkpoint, copy_verified, distinct, read_exact,
                      report, write_all, need_space, hash_file, fingerprint)

RAW_DISK_SIZES = {161280: "35 tracks, single sided", 184320: "40 tracks, single sided",
                  322560: "35 tracks, double sided", 368640: "80 tracks single sided / 40 double sided",
                  737280: "full slot (80 tracks, double sided)"}


def read_disk_file(path):
    p = Path(path)
    if not p.is_file():
        raise FormatError("Disk imports require a regular raw sector image.")
    with p.open("rb", buffering=0) as f:
        initial = fingerprint(f)
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise FormatError("Disk imports require a regular raw sector image.")
        length = initial[2]
        if length not in RAW_DISK_SIZES:
            if 1 <= length % 256 <= 5:
                raise FormatError("This appears to be a JVC headered image. Only raw 256-byte-sector images are supported; export raw in your emulator first.")
            raise FormatError("Unsupported disk representation/length. Use a raw 161280, 184320, 322560, 368640 or 737280-byte sector image. DMK, HFE and VDK are unsupported.")
        b = read_exact(f, length)  # bounded even if another process grows the file
        if fingerprint(f) != initial:
            raise SafetyError("Disk import source changed while reading; retry with a stable source.")
    if b[:2] in (b"dk", b"HX") or b[:8] == b"HXCPICFE":
        raise FormatError("Headered VDK/HFE images are unsupported. Supply raw sector data.")
    if b[0] in (0, 255) and b[1] in (35, 40, 80) and 128 <= int.from_bytes(b[2:4], "little") <= 0x4000 and not any(b[5:12]):
        raise FormatError("Possible DMK image: raw sector representation is required.")
    return b


class Image:
    def __init__(self, path, writable=False):
        self.path = Path(path).resolve()
        if not stat.S_ISREG(self.path.stat().st_mode):
            raise SafetyError("Image access requires a regular file. Select cards through the card workflow.")
        self.file = self.path.open("r+b" if writable else "rb", buffering=0)
        opened = os.fstat(self.file.fileno())
        if not stat.S_ISREG(opened.st_mode):
            self.file.close()
            raise SafetyError("Image access requires a regular file.")
        self.size = opened.st_size
        self.writable = writable
        self.failure_reason = None
        self._io_lock = threading.RLock()
        self._journal = None
        self._records = []

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def read(self, offset, count):
        self.require_usable()
        if offset < 0 or count < 0 or offset + count > self.size:
            raise FormatError(f"Read outside image at byte {offset}, length {count}.")
        with self._io_lock:
            self.file.seek(offset)
            return read_exact(self.file, count)

    def write(self, offset, data):
        self.require_usable()
        if not self.writable or self._journal is None:
            raise SafetyError("Writes require a transaction on a separate working image.")
        if offset < 0 or offset + len(data) > self.size:
            raise FormatError("Write outside working image.")
        with self._io_lock:
            original = self.read(offset, len(data))
            if original == data:
                return
            position = self._journal.tell()
            write_all(self._journal, original)
            self._records.append((offset, len(data), position))
            self.file.seek(offset)
            write_all(self.file, data)

    @contextmanager
    def transaction(self):
        self.require_usable()
        if self._journal is not None:
            raise SafetyError("Nested image transaction.")
        need_space(self.path.parent, 0)
        # Undo bytes must reach the file before changing the image. A buffered
        # journal can defer ENOSPC until rollback seeks, losing its own undo.
        with tempfile.TemporaryFile(dir=self.path.parent, buffering=0) as journal:
            self._journal, self._records = journal, []
            try:
                yield self
                self.file.flush()
                os.fsync(self.file.fileno())
            except BaseException as error:
                try:
                    for offset, size, pos in reversed(self._records):
                        with self._io_lock:
                            journal.seek(pos)
                            self.file.seek(offset)
                            write_all(self.file, read_exact(journal, size))
                    self.file.flush()
                    os.fsync(self.file.fileno())
                except BaseException as rollback_error:
                    self.failure_reason = (
                        f"Working image rollback failed ({rollback_error}) after: {error}. "
                        "This working image cannot be saved, edited or written to a card. "
                        "Discard it and reopen an unchanged original or verified backup.")
                    self.writable = False
                    raise SafetyError(self.failure_reason) from rollback_error
                raise
            finally:
                self._journal, self._records = None, []

    def require_usable(self):
        if self.failure_reason:
            raise SafetyError(self.failure_reason)

    def layout(self):
        return inspect(self)

    def volume(self, index, disk=None, editing=False):
        layout = self.layout()
        if not layout.addressing_known:
            raise FormatError("Logical volume access disabled because card addressing/table termination is ambiguous. Complete raw backup, image copying and diagnostics remain available.")
        if editing:
            layout.require_editable()
        p = layout.partition(index)
        if p.start <= 0 or p.end * BLOCK > self.size or p.count == 0:
            raise FormatError("Partition extent cannot be accessed safely.")
        if disk is None:
            return PayloadView(self, p.start, p.count)
        if p.kind != 2 or not 0 <= disk < p.disks:
            raise FormatError(f"Disk number is outside partition {index}'s complete slots.")
        return PayloadView(self, p.start + disk * SLOT_SECTORS, SLOT_SECTORS)

    def export_disk(self, index, disk, destination, size=SLOT_BYTES, progress=None, cancel=None):
        if size not in RAW_DISK_SIZES:
            raise FormatError("Choose a documented explicit raw export size, or the full slot.")
        distinct(self.path, destination)
        self.volume(index, disk).export(destination, size, progress, cancel)

    def import_disks(self, mappings, tail="zero", progress=None, cancel=None):
        """mappings: [(partition_index, zero_based_slot, path)]. One rollback unit."""
        self.layout().require_editable()
        if tail not in ("zero", "ff", "preserve"):
            raise FormatError("Choose unused tail policy: zero, ff or preserve.")
        if not mappings or len({(p, d) for p, d, _ in mappings}) != len(mappings):
            raise FormatError("Batch mapping is empty or maps two sources to the same slot.")
        # Validate all sources and destinations before changing a byte.
        for p, d, path in mappings:
            distinct(self.path, path)
            self.volume(p, d, editing=True)
            read_disk_file(path)
        need_space(self.path.parent, len(mappings) * SLOT_SECTORS * BLOCK)
        with self.transaction():
            for i, (p, d, path) in enumerate(mappings):
                checkpoint(cancel)
                b = read_disk_file(path)
                if tail != "preserve":
                    b = b.ljust(SLOT_BYTES, b"\0" if tail == "zero" else b"\xff")
                self.volume(p, d, editing=True).write(0, b)
                report(progress, i + 1, len(mappings), f"Importing {Path(path).name} → partition {p}, disk {d}")
            checkpoint(cancel)

    def rename_partition(self, index, name):
        layout = self.layout()
        layout.require_editable()
        p = layout.partition(index)
        self._unique_name(name, index)
        with self.transaction():
            self.write(p.entry_sector * BLOCK + 1, encode_name(name))

    def _unique_name(self, name, excluding=None):
        encode_name(name)
        if any(p.index != excluding and p.name.casefold() == name.casefold() for p in self.layout().partitions):
            raise FormatError("A partition with that name already exists.")

    def delete_partition(self, index, replacements=None):
        layout = self.layout()
        layout.require_editable()
        p = layout.partition(index)
        affected = [key for key, value in layout.defaults.items() if value == p.start]
        # Unassigned zero is documented explicitly; never silently point to a
        # different disk. Caller can select compatible replacement partition IDs.
        replacements = replacements or {}
        with self.transaction():
            for key in affected:
                target = replacements.get(key)
                address = 0
                if target is not None:
                    new = layout.partition(target)
                    if target == index or (key == "DOS" and new.kind != 2) or (key != "DOS" and new.kind not in (3, 4)):
                        raise FormatError(f"Invalid replacement for default {key}.")
                    address = new.start
                self.write(DEFAULTS[key], p24(address))
            self.write(p.entry_sector * BLOCK, b"\0")

    def set_default(self, key, index=None):
        layout = self.layout()
        layout.require_editable()
        if key not in DEFAULTS:
            raise FormatError("Unknown default drive.")
        address = 0
        if index is not None:
            p = layout.partition(index)
            if (key == "DOS" and p.kind != 2) or (key != "DOS" and p.kind not in (3, 4)):
                raise FormatError("Default drive and partition types differ.")
            address = p.start
        with self.transaction():
            self.write(DEFAULTS[key], p24(address))

    def create_partition(self, name, disks=1, os9_path=None, progress=None, cancel=None):
        from .filesystems.decb import blank_disk
        from .filesystems.os9 import OS9, RawVolume
        layout = self.layout()
        layout.require_editable()
        self._unique_name(name)
        if os9_path:
            distinct(self.path, os9_path)
            with RawVolume(os9_path) as raw:
                source_fingerprint = fingerprint(raw.file)
                fs = OS9(raw)
                fs.list_dir()
                count = raw.sectors
                if fingerprint(raw.file) != source_fingerprint:
                    raise SafetyError("OS-9 volume changed during validation.")
            kind = 3
        else:
            if not 1 <= disks <= 256:
                raise FormatError("Choose between 1 and 256 floppy slots.")
            count, kind = disks * SLOT_SECTORS, 2
        free = next(((s, n) for s, n in layout.free_regions() if n >= count), None)
        if free is None:
            raise FormatError("No contiguous free region is large enough. Existing partitions are never moved automatically.")
        start = free[0]
        idx = layout.deleted[0] if layout.deleted else layout.terminator - layout.table
        if idx >= MAX_PARTITIONS:
            raise FormatError("Partition table is full.")
        sec = layout.table + idx
        extending = not layout.deleted
        if extending:
            following = sec + 1
            if following * BLOCK >= self.size or following == start or any(p.start <= following < p.end for p in layout.partitions):
                raise FormatError("No safe space to extend the partition table.")
            # Do not erase unknown data following an existing table.
            next_type = self.read(following * BLOCK, 1)[0]
            if next_type not in (0, 255) and not next_type & 128:
                raise FormatError("Unknown data follows the table. Cannot extend it safely.")
        need_space(self.path.parent, count * BLOCK * 2)
        with self.transaction():
            if extending:
                self.write((sec + 1) * BLOCK, b"\xff")
            # Only documented entry fields are touched, even in reused entries.
            self.write(sec * BLOCK, bytes([kind]))
            self.write(sec * BLOCK + 1, encode_name(name))
            self.write(sec * BLOCK + 32, p24(start) + p24(count))
            # Initialize newly allocated blocks; old unallocated data is explicitly
            # replaced by partition creation. Existing partitions/padding untouched.
            for n in range(0, count, 512):
                checkpoint(cancel)
                amount = min(512, count - n)
                self.write((start + n) * BLOCK, b"\xff" * (amount * BLOCK))
                report(progress, n + amount, count, "Initializing new partition")
            view = PayloadView(self, start, count)
            if kind == 2:
                blank = blank_disk()
                for disk in range(disks):
                    checkpoint(cancel)
                    view.write(disk * SLOT_BYTES, blank)
            else:
                view.import_raw(os9_path, progress, cancel, expected_source=source_fingerprint)
                OS9(view).list_dir()
            if kind == 2 and not layout.defaults["DOS"] and not any(p.kind == 2 and p.disks for p in layout.partitions):
                self.write(DEFAULTS["DOS"], p24(start))
            checkpoint(cancel)
            self.layout().require_editable()
        return idx

    def import_os9(self, index, source, progress=None, cancel=None):
        from .filesystems.os9 import OS9, RawVolume
        layout = self.layout()
        layout.require_editable()
        p = layout.partition(index)
        if p.kind not in (3, 4):
            raise FormatError("Select an OS-9 partition.")
        distinct(self.path, source)
        with RawVolume(source) as raw:
            source_fingerprint = fingerprint(raw.file)
            fs = OS9(raw)
            fs.list_dir()
            if raw.sectors != p.count:
                raise FormatError("Replacement OS-9 volume must exactly match the partition's sector count. Add it as a new partition for a different size.")
            if fingerprint(raw.file) != source_fingerprint:
                raise SafetyError("OS-9 volume changed during validation.")
        need_space(self.path.parent, p.count * BLOCK)
        with self.transaction():
            view = self.volume(index, editing=True)
            view.import_raw(source, progress, cancel, expected_source=source_fingerprint)
            OS9(view).list_dir()
            checkpoint(cancel)


class PayloadView:
    def __init__(self, image, start, sectors):
        self.image, self.start, self.sectors = image, start, sectors
        self.size = sectors * PAYLOAD

    def read(self, offset, count):
        if offset < 0 or count < 0 or offset + count > self.size:
            raise FormatError("Logical payload read exceeds volume.")
        result = bytearray()
        while count:
            sector, within = divmod(offset, PAYLOAD)
            n = min(count, PAYLOAD - within)
            result.extend(self.image.read((self.start + sector) * BLOCK + within, n))
            offset, count = offset + n, count - n
        return bytes(result)

    def write(self, offset, data):
        if offset < 0 or offset + len(data) > self.size:
            raise FormatError("Logical payload write exceeds volume.")
        # Merge a bounded run of physical blocks, retaining every padding byte.
        pos = 0
        while pos < len(data):
            sector, within = divmod(offset + pos, PAYLOAD)
            amount = min(len(data) - pos, 256 * PAYLOAD - within)
            sectors = (within + amount + PAYLOAD - 1) // PAYLOAD
            physical = bytearray(self.image.read((self.start + sector) * BLOCK, sectors * BLOCK))
            done = 0
            while done < amount:
                s, w = divmod(within + done, PAYLOAD)
                n = min(amount - done, PAYLOAD - w)
                physical[s * BLOCK + w:s * BLOCK + w + n] = data[pos + done:pos + done + n]
                done += n
            self.image.write((self.start + sector) * BLOCK, physical)
            pos += amount

    def export(self, destination, length=None, progress=None, cancel=None):
        length = self.size if length is None else length
        if not 0 <= length <= self.size:
            raise FormatError("Export length exceeds volume.")
        need_space(Path(destination).parent, length)
        with atomic_output(destination) as f:
            for pos in range(0, length, 256 * PAYLOAD):
                checkpoint(cancel)
                n = min(256 * PAYLOAD, length - pos)
                write_all(f, self.read(pos, n))
                report(progress, pos + n, length, "Exporting logical sectors")

    def import_raw(self, source, progress=None, cancel=None, *, expected_source=None):
        with open(source, "rb", buffering=0) as f:
            before = fingerprint(f)
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode) or (expected_source is not None and before != expected_source):
                raise SafetyError("Raw volume source changed after validation; no replacement data was imported.")
            size = before[2]
            if size != self.size:
                raise FormatError("Raw volume size must match destination exactly.")
            for pos in range(0, size, 256 * PAYLOAD):
                checkpoint(cancel)
                n = min(256 * PAYLOAD, size - pos)
                self.write(pos, read_exact(f, n))
                report(progress, pos + n, size, "Importing logical sectors (preserving card padding)")
            if before != fingerprint(f):
                raise SafetyError("Import source changed while reading.")


def create_blank(path, capacity, disks=1, name="DRIVEPAK", progress=None, cancel=None):
    if capacity % BLOCK or capacity < (1024 + disks * SLOT_SECTORS) * BLOCK or capacity > (0x1000000 * BLOCK):
        raise FormatError("Capacity must be 512-byte aligned, fit the bank, and be at most 8 GiB for 24-bit addressing.")
    encode_name(name, 8)
    if not 1 <= disks <= 256:
        raise FormatError("Choose 1–256 disk slots.")
    need_space(Path(path).parent, capacity + disks * SLOT_SECTORS * BLOCK)
    with atomic_output(path) as f:
        block = b"\xff" * (4 * 1024**2)
        for pos in range(0, capacity, len(block)):
            checkpoint(cancel)
            write_all(f, block[:min(len(block), capacity - pos)])
            report(progress, min(pos + len(block), capacity), capacity, "Creating blank card image")
        f.seek(0)
        header = bytearray(256)
        header[:8] = encode_name(name, 8)
        header[17:20] = p24(1)
        write_all(f, header)
        f.seek(BLOCK)
        write_all(f, b"\xff")
        f.flush()
        # Finish the bank and validate while the image still has a partial name.
        # Only the complete new image is published by atomic_output.
        with Image(f.partial_path, True) as image:
            image.create_partition("DOS", disks, progress=progress, cancel=cancel)


class Session:
    """A durable, separate working copy; original never opened for writing."""
    def __init__(self, original, working, image, original_digest):
        self.original, self.working, self.image = Path(original).resolve(), Path(working).resolve(), image
        self.original_digest = original_digest
        self.changes = []
        self.saved_as = None

    @classmethod
    def open(cls, original, work_dir, progress=None, cancel=None):
        work_dir = Path(work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        working = work_dir / (next(tempfile._get_candidate_names()) + ".working.img")
        digest = copy_verified(original, working, progress, cancel)
        return cls(original, working, Image(working, True), digest)

    def changed(self, description):
        self.changes.append(description)
        self.saved_as = None

    def save_as(self, destination, progress=None, cancel=None):
        self.image.require_usable()
        distinct(self.original, self.working, destination)
        digest = copy_verified(self.working, destination, progress, cancel)
        self.saved_as = str(Path(destination).resolve())
        return digest

    def discard(self, progress=None, cancel=None):
        # Re-copy into a new working path; never rely on in-place rollback of
        # several completed user actions or on a possibly changed original.
        if hash_file(self.original, progress, cancel) != self.original_digest:
            raise SafetyError("Original has changed outside this application. Reopen a verified backup to recover.")
        fresh = Session.open(self.original, self.working.parent, progress, cancel)
        if fresh.original_digest != self.original_digest:
            fresh.close()
            raise SafetyError("Original changed while discarding edits. The current working image was retained; reopen a verified backup to recover.")
        self.image.close()
        self.working, self.image = fresh.working, fresh.image
        self.changes, self.saved_as = [], None

    def close(self):
        self.image.close()
