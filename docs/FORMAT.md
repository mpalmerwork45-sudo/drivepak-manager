# Supported CoCoNet Drive Pak profile

This document describes observations and the conservative profile implemented
by DrivePak Manager 1.0.0. It is not a universal Drive Pak specification. CoCo SDC,
FAT32-hosted disk collections, and undocumented future 32-bit addressing are
outside this profile. Unsupported images can still be copied, completely backed
up from a safe card, restored as raw bytes, and inspected diagnostically.

## Evidence

Primary references, retrieved 2026-10-31:

* Roger Taylor's [Quick Docs](https://colorcomputerarchive.com/repo/Documents/Manuals/Hardware/MicroSD%20Drive%20Pak%20Quick%20Docs%20%28Roger%20Taylor%29.pdf),
  pages 6–8: table capacity, low-level commands, header and partition entry maps.
* Guillaume Major's [Explorer 1.00 archive](https://colorcomputerarchive.com/repo/Programming/Source/DrivePak%20Explorer/DrivePak%20Explorer%201.00%20Source%20Code%20%28Guillaume%20Major%29.zip):
  `dpx.asm` READPART (2093–2224), GETPARTDSK (2023–2068), TYPELBL (2073–2088),
  RENPART/WRITENAME (2229–2305), SETDRV (2317–2399), and DIRTRACK/DIRSECT constants.
* The untouched [2022-02-11 2 GB-labelled image](https://colorcomputerarchive.com/repo/Disks/DrivePak/Image/DrivePak%202GB%20Image%20%282022-02-11%29.zip).
  Locally measured sizes/digests, including every extracted member, are in
  [provenance.json](provenance.json). No downloaded content is distributed here.

The archive is 106,435,653 bytes; its raw image is **1,866,210,304 bytes**, or
3,644,942 card blocks. Its SHA-256 is
`8f46cf246b6b625efc171b3e25b805bdca89496c2a34b8491fcf4600dd8cf2c2`.
An index within the archive carries an older internal date; the URL date is not
assumed to date every contained file. The 4 GB image has not been tested. The
inconsistent OS-9 extent described below is safely bounded without assuming a
different image resolves it.

## Card and table

Each card-addressed sector occupies **512 physical bytes**. Stored 24-bit values
are unsigned big endian. The header is the first block; its documented fields
are within its first 256 bytes:

| Offset, hex | Meaning |
|---|---|
| 00–07 | Eight-byte card name; `DRIVEPAK` is an observed name, not a fixed magic |
| 08–0B | Mounted floppy values, documented as unused at the time |
| 0C–0F | Reserved |
| 11–13 | Partition-table sector address |
| 15–17 | Default DOS bank start sector |
| 19–1B | Default OS-9 /DD start sector |
| 1D–1F, 21–23, 25–27, 29–2B | Default OS-9 /U0–/U3 start sectors |
| 10, 14, 18, 1C, 20, 24, 28 | Reserved high bytes of possible future addresses |

Nonzero address-extension bytes disable edits: the implementation does not guess
whether they are fillers or future address bits. Other reserved bytes survive
unchanged. Zero defaults are retained as unassigned; the reference has all six
zero. Nonzero defaults must identify one compatible live partition start.

Follow the table pointer. Each successive table entry is in its **own card
sector**, rather than packed into a single sector:

| Entry offset, hex | Meaning |
|---|---|
| 00 | Type: 0 deleted; high bit set terminates scan; 2 Disk BASIC; 3/4 OS-9 |
| 01–10 | Sixteen-byte name storage |
| 20–22 | Partition starting card sector |
| 23–25 | Partition length in addressed sectors |

Deleted entries do not terminate enumeration. Unknown live types retain their
extent and metadata. The manual's upper bound is 1023 entries; Explorer scans
only 128 sectors and stores only the first eight name characters. Manager scans
to a terminator within the documented maximum and actual image bounds.

Names are read without modifying their storage. Observed partition names have
15 printable/space-padded bytes and a final NUL. New/renamed names use that
representation and are limited to 15 printable ASCII characters; **eight or
fewer is recommended for Explorer compatibility**. Longer existing names and
unusual storage remain unchanged unless explicitly renamed.

The sample table starts at sector 1, has 65 live entries, and terminates at sector
66. The first DOS bank starts at sector 1024 and spans 737280 sectors. New images
reserve sectors 0–1023; relocated existing tables are followed and also reserved
through their terminator. No header pointer or table sector is hardcoded when
opening an existing image.

All extents, additions, table termination, defaults and metadata/data overlaps
are checked before mutation. Although the manual allows overlays, Manager
disables editing an overlapping layout because byte ownership is ambiguous.
Zero-length entries may be inspected, renamed or deleted but have no browseable
volume. Counts outside complete firmware-addressable slots are reported and
retained. Python integers do not wrap; 24-bit serialization is explicitly checked.

## Logical payload and floppy slots

The sample stores **256 payload bytes followed by 256 padding bytes** in each
512-byte block. This applies to the sampled BASIC and OS-9 regions. Manager uses
this supported profile; it does not autodetect a packed-512 payload variant.
Padding is retained verbatim, including non-FF bytes. Initializing an explicitly
new partition replaces that allocated region with FF, then builds its payload.

Explorer's GETPARTDSK divides the length by 64 and 45, giving 2880 addressed
sectors per virtual floppy slot, capped at 256 selectable floppies per bank.
For zero-based disk `d` and logical sector `s`, the payload offset is:

```text
(partition.start + d * 2880 + s) * 512
```

The next 256 bytes are the payload. A slot has 737280 logical bytes and a
1474560-byte physical stride. Independent reference checks locate `DPX.BAS`,
`ASTRO.BIN`, and `BIRDS.BIN` in DOS slots 0, 1 and 2 respectively. Directories
are found at logical sector `17*18 + 2`, using zero-based sector numbering.

Disk imports support raw 256-byte-sector lengths 161280, 184320, 322560, 368640,
and 737280. The 368640-byte size is not uniquely descriptive of sides/tracks.
JVC headers (common 1–5-byte prefixes), VDK, HFE and possible DMK headers are
rejected; unsupported lengths never get rounded/truncated into a slot. Valid raw
data replaces payload only. Smaller imports explicitly choose a logical tail:
zero-fill (default), FF-fill, or preserve existing payload. Physical padding is
always retained. Batch mappings must name a distinct partition/slot per source;
all mappings validate before mutation, and a failed batch rolls back.

Exports offer those same explicit sizes plus the default full-slot size. A
smaller export deliberately omits the logical tail. **No original file length
or geometry is inferred by trimming zeros/FF**, and byte-exact recovery of an
arbitrary original floppy image is not claimed. GUI disk export sidecars record
the selected representation and size.

## Disk BASIC files

The file implementation supports the validated standard 35-track, 18-sector,
256-byte, **68-granule** Disk BASIC filesystem within a larger slot. Sector two
of track 17 holds the FAT; its first 68 on-disk bytes describe granules (the six
control bytes in the ROM's RAM FAT are not an on-disk prefix). Directory entries
are 32 bytes in sectors 3–11. Granules contain nine sectors and skip track 17.

FF means free; 0–67 links to another granule; C0–C9 encodes the final granule's
used sector count. Directory byte 13 gives the first granule, bytes 14–15 the
big-endian final-sector byte count, 0–256. C0 with zero bytes represents an empty
file. File length follows metadata, never a content scan. Cycles, cross-links,
invalid counts, names, and duplicate entries are rejected. Orphan allocation or
unknown type/format metadata permits safe browsing/extraction but disables file
mutation. A zero directory-name byte is deleted, FF terminates the directory.

Insertion checks space before writing and writes actual content bytes, required
FAT links and directory metadata only. Rename changes only the 11 name bytes;
delete marks the entry and frees the FAT chain, retaining old data. File type,
ASCII/binary flag and reserved directory bytes are retained. Extraction writes
verbatim content with a `.drivepak.json` metadata sidecar; reinsertion verifies
the sidecar digest and restores its metadata. It never tokenizes, detokenizes,
transcodes line endings or strips machine-code load records. Larger raw floppy
images can be transferred, but extended/nonstandard BASIC allocation is not
silently interpreted as an extended filesystem.

## OS-9

The read-only browser implements classic 6809 RBF's 256-byte sectors, DD.TOT and
DD.DIR, 29-byte high-bit-terminated names, 32-byte directory entries, descriptors
with a four-byte file size and up to 48 five-byte segments. Extents, descriptor
pointers, segment overlap, directory sizes and declared lengths are validated.
File extraction streams segment data and records owner/date/attribute descriptor
metadata separately. Traversal uses explicit paths and does not follow `..`.
OS-9/68k/extended sector variants are rejected.

Entire partition payloads can be exported. A replacement must be a structurally
recognised raw OS-9 volume exactly matching the partition sector count; different
sizes are added as new partitions. Newly created OS-9 entries use type 3. Type 4
is recognised as Explorer's OS-9 classification, with actual RBF metadata still
validated. Individual OS-9 file modification is outside this release's scope.

The reference's SIERRA entry (index 36) contains **56518 sectors**, but its DD.TOT
declares **73728**. Browsing it is disabled with those exact counts. An explicitly
warned export retains only the stored partition extent and is labelled a
diagnostic partition payload, not a claimed complete healthy volume. The app
does not extend it into the following partition or repair it automatically.

## Partition mutation and newly created images

Entries are never compacted and live disk numbering is never shifted. Creation
uses a deleted table entry first, or extends the terminator only when safe. It
uses a checked contiguous free range without moving other partitions. Rename
touches name storage only. Deletion changes the type to zero, retains data and
reserved bytes, and explicitly reassigns or clears affected defaults. Zero
defaults remain unchanged when adding a second bank; a brand-new first BASIC
bank gets the DOS pointer. The GUI previews affected references.

New images contain a validated header, table, empty DOS bank, zero-filled empty
logical floppy payloads with FF FAT/directory metadata, and FF physical padding.
No games, firmware or boot files are supplied. Whole OS-9 volumes may be added by
the user. Existing unrecognised regions are never normalised during ordinary
edits or saves. Working-image operations journal old bytes for exception/
cancellation rollback; crash recovery always retains the separate original.
