"""Read-only classic OS-9/6809 RBF filesystem, using 256-byte sectors."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import stat

from ..errors import FormatError
from ..format import u24
from ..streams import atomic_output, checkpoint, read_exact, report, write_all, write_json


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
        current = self.descriptor(self.root, "/")
        parts = PurePosixPath(path).parts
        for name in parts:
            if name in ("/", "."):
                continue
            if name == "..":
                raise FormatError("Use an absolute OS-9 path without '..'.")
            matches = [x for x in self._entries(current) if x.name == name]
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
                if name in seen:
                    raise FormatError("Duplicate OS-9 directory name.")
                seen.add(name)
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
        with atomic_output(destination) as out:
            done = 0
            for b in self.chunks(f, cancel):
                write_all(out, b)
                done += len(b)
                report(progress, done, f.size, "Extracting OS-9 file")
        write_json(meta, {"format": "OS-9 RBF file", "path": path, "bytes": f.size,
                          "attributes": f.attributes, "descriptor": f.raw_fd.hex(),
                          "notice": "Content is verbatim; original owner, dates, attributes and segment metadata are recorded here."})
