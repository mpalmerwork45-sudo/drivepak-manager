"""CoCoNet card structures. All stored addresses are card sectors, never bytes."""
from dataclasses import dataclass, field

from .errors import FormatError

BLOCK = 512
PAYLOAD = 256
SLOT_SECTORS = 2880
SLOT_BYTES = SLOT_SECTORS * PAYLOAD
MAX24 = 0xFFFFFF
MAX_PARTITIONS = 1023
DEFAULTS = {"DOS": 0x15, "DD": 0x19, "U0": 0x1D, "U1": 0x21, "U2": 0x25, "U3": 0x29}


def u24(b):
    if len(b) != 3:
        raise FormatError("A 24-bit field was truncated.")
    return int.from_bytes(b, "big")


def p24(n):
    if not 0 <= n <= MAX24:
        raise FormatError("Value exceeds the supported 24-bit format.")
    return n.to_bytes(3, "big")


def name_text(b):
    return b.split(b"\0", 1)[0].rstrip(b" \xff").decode("ascii", "replace")


def encode_name(name, length=16):
    limit = 15 if length == 16 else length
    try:
        b = name.encode("ascii")
    except UnicodeEncodeError as e:
        raise FormatError("Use printable ASCII for card/partition names.") from e
    if not b or len(b) > limit or any(x < 32 or x > 126 for x in b) or name != name.strip():
        raise FormatError(f"Name must contain 1–{limit} printable ASCII characters with no surrounding spaces.")
    return b.ljust(15, b" ") + b"\0" if length == 16 else b.ljust(length, b" ")


@dataclass(frozen=True)
class Partition:
    index: int
    entry_sector: int
    kind: int
    raw_name: bytes
    start: int
    count: int

    @property
    def name(self):
        return name_text(self.raw_name)

    @property
    def end(self):
        return self.start + self.count

    @property
    def disks(self):
        return min(256, self.count // SLOT_SECTORS) if self.kind == 2 else 0

    @property
    def label(self):
        return {2: "Disk BASIC", 3: "OS-9", 4: "OS-9"}.get(self.kind, f"Unknown type {self.kind}")


@dataclass
class Layout:
    name: str = "Unrecognised"
    size: int = 0
    table: int = 0
    addressing_known: bool = False
    terminator: int | None = None
    partitions: list[Partition] = field(default_factory=list)
    deleted: list[int] = field(default_factory=list)
    defaults: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def editable(self):
        return not self.errors and self.terminator is not None

    def require_editable(self):
        if not self.editable:
            raise FormatError("Image editing disabled: " + "; ".join(self.errors or ["No valid table terminator"]))

    def partition(self, index):
        for part in self.partitions:
            if part.index == index:
                return part
        raise FormatError(f"No live partition at table index {index}.")

    def free_regions(self):
        self.require_editable()
        # The manual reserves the first 1024 sectors. For relocated tables retain
        # at least the whole table through its terminator; no speculative clearing.
        occupied = [(0, min(1024, self.size // BLOCK)), (self.table, self.terminator + 1)]
        occupied += [(p.start, p.end) for p in self.partitions if p.count]
        result, pos = [], 0
        for start, end in sorted(occupied):
            if start > pos:
                result.append((pos, start - pos))
            pos = max(pos, end)
        if pos < min(self.size // BLOCK, MAX24 + 1):
            result.append((pos, min(self.size // BLOCK, MAX24 + 1) - pos))
        return result


def inspect(reader):
    """Return diagnostics even for unknown data. No guessed fixed signature."""
    out = Layout(size=reader.size)
    if reader.size < BLOCK or reader.size % BLOCK:
        out.errors.append("Image is truncated or is not a whole number of 512-byte card blocks.")
        return out
    h = reader.read(0, BLOCK)
    out.name = name_text(h[:8])
    out.table = u24(h[0x11:0x14])
    out.defaults = {key: u24(h[off:off + 3]) for key, off in DEFAULTS.items()}
    if any(h[off - 1] != 0 for off in [0x11, *DEFAULTS.values()]):
        out.errors.append("Nonzero reserved address extension; possible 32-bit variant is unsupported.")
    if not 0 < out.table < reader.size // BLOCK:
        out.errors.append("Partition table pointer is outside the image or points at the header.")
        return out
    for idx in range(MAX_PARTITIONS + 1):
        sec = out.table + idx
        if sec >= reader.size // BLOCK:
            out.errors.append("Partition table runs beyond the image without a terminator.")
            break
        entry = reader.read(sec * BLOCK, BLOCK)
        kind = entry[0]
        if kind & 0x80:
            out.terminator = sec
            break
        if idx == MAX_PARTITIONS:
            out.errors.append("Table exceeds the manual's 1023-entry limit.")
            break
        if kind == 0:
            out.deleted.append(idx)
            continue
        out.partitions.append(Partition(idx, sec, kind, entry[1:17], u24(entry[32:35]), u24(entry[35:38])))
    if out.terminator is None:
        out.errors.append("Missing partition table terminator.")
    out.addressing_known = out.terminator is not None and all(h[off - 1] == 0 for off in [0x11, *DEFAULTS.values()])
    table_end = (out.terminator + 1) if out.terminator is not None else out.table + MAX_PARTITIONS + 1
    ranges = []
    for p in out.partitions:
        if (p.count and p.start == 0) or p.end > reader.size // BLOCK or p.end > MAX24 + 1:
            out.errors.append(f"Partition {p.index} ({p.name}) is out of bounds.")
        if p.count and p.start < table_end and p.end > out.table:
            out.errors.append(f"Partition {p.index} overlaps the partition table.")
        if p.count:
            ranges.append(p)
        else:
            out.warnings.append(f"Partition {p.index} is zero length; raw inspection only for that partition.")
        if p.kind == 2 and (p.count % SLOT_SECTORS or p.count > 256 * SLOT_SECTORS):
            out.warnings.append(f"Partition {p.index}: only {p.disks} complete, firmware-addressable floppy slots exposed; remainder retained.")
    for a, b in zip(sorted(ranges, key=lambda p: p.start), sorted(ranges, key=lambda p: p.start)[1:]):
        if a.end > b.start:
            out.errors.append(f"Partitions {a.index} and {b.index} overlap. Shared-data editing is disabled.")
    for key, value in out.defaults.items():
        if value and value not in [p.start for p in out.partitions]:
            out.errors.append(f"Default {key} points to {value}, which is not a live partition start.")
        elif value:
            matches = [p for p in out.partitions if p.start == value]
            if len(matches) != 1:
                out.errors.append(f"Default {key} has an ambiguous partition reference.")
            elif (key == "DOS" and matches[0].kind != 2) or (key != "DOS" and matches[0].kind not in (3, 4)):
                out.errors.append(f"Default {key} references an incompatible partition type.")
    return out
