# DrivePak Manager

**Version 1.0.3 — manage CoCoNet MicroSD Drive Pak cards and images on Linux.**

A native Python/PySide6 desktop utility for Roger Taylor's **CoCoNet MicroSD
Drive Pak** proprietary card layout. Supports working images, Disk BASIC disk
and file management, OS-9 volume transfer and browsing, and authenticated Linux
card backup/write with independent verification. This is the older Drive Pak
layout, distinct from CoCo SDC.

The desktop runs as your normal user. Card access uses a separately installed,
root-owned helper with polkit authentication. Automated tests use regular files
and simulated devices. Complete physical readback and CoCoNet loading checks
still need confirmation on the intended card/reader; see the testing checklist.

## Run it now

If this checkout is already installed, open Konsole and run:

```bash
cd ~/CoCoPak/drivepak-manager
.venv/bin/drivepak-manager
```

The `.venv` command selects the project's Python environment even if your
terminal also displays a Conda `(base)` prompt. Run the GUI as your normal user.
If a card write is running, wait for **readback verification** before closing,
restarting or disconnecting the card. The copying phase alone is not the final
success indication.

## What it is for

DrivePak Manager provides a desktop route from an existing card or raw image to
a verified edited result. It backs up the whole card, opens a separate working
copy, imports and exports virtual floppy images, manages standard Disk BASIC
files, browses/extracts OS-9 files, transfers whole OS-9 volumes, manages supported
partitions and writes images back with verification. Blank images contain an
empty DOS bank, with no games or firmware supplied.

It is useful when adding a game disk, moving files between a CoCo disk and Linux,
organising banks, preserving an old card, preparing another card, or restoring
an earlier snapshot. Saving an edited image and writing a physical card are
separate actions, and their status is shown in the interface.

## A brief Drive Pak history

Roger Taylor's Drive Pak family brought virtual disks and OS-9 storage to the
Tandy Color Computer through MicroSD, USB and cable/serial devices. CoCoNet
extended the Disk BASIC environment with commands for named partitions and
numbered virtual disks. The surviving 2011-era Quick Docs describe a MicroSD
generation with CoCoNet 1.27 and 4D Systems R24 firmware, and mention CoCoPak.exe
as a PC utility for replacing card contents.
[Original Quick Docs](https://colorcomputerarchive.com/repo/Documents/Manuals/Hardware/MicroSD%20Drive%20Pak%20Quick%20Docs%20%28Roger%20Taylor%29.pdf).

Guillaume Major's DrivePak Explorer offered an on-CoCo browser. Its 1.00 source
archive is preserved with a December 2017 directory date; its source header also
acknowledges earlier Sidekick work by Luis Antoniosi. It remains useful evidence
for understanding card tables and disk detection.
[Explorer source archive](https://colorcomputerarchive.com/repo/Programming/Source/DrivePak%20Explorer/).

The Color Computer Archive also preserves 2 GB- and 4 GB-labelled image downloads
dated 2022-02-11. DrivePak Manager was developed in 2026 using these historical
references, independently constructed fixtures and byte-level checks against
the 2 GB-labelled image. It is a new implementation; archived Explorer code,
games and firmware are not embedded in the application.
[Reference image directory](https://colorcomputerarchive.com/repo/Disks/DrivePak/Image/),
[attribution and licensing](THIRD_PARTY.md).

## Banks, numbered disks and files

A raw `.img` represents card bytes. Inside a recognised DrivePak layout, a
partition may contain a **bank of virtual floppies** or an OS-9 volume. Banks
such as **DOS** and **T&D** have their own numbered slots, starting at zero.
The application shows each bank's actual number of complete slots, up to 256.

Expand a bank using its arrow in **Partitions and virtual disks**, then select
a numbered child such as **Disk 031**. That is disk 31 within the selected bank;
another bank's disk 31 is a different disk. On a CoCo using the documented
CoCoNet commands, the corresponding selection looks like:

```text
DRIVE# "T&D"
DRIVE 0,#31
DIR
```

Enter those commands on the CoCo, not in a Linux terminal. The named bank must
exist on the card. See the original manual for firmware-specific commands.

A `.dsk` contains one floppy's sector data. A file such as `GAME.BIN` or
`MENU.BAS` lives inside a floppy filesystem. **Import disk(s)** replaces a whole
slot; **Insert file** adds a file to the selected standard BASIC disk.

Linux does not need to recognise or mount the proprietary card filesystem for
a raw backup. The reader must still detect the card's physical capacity.

## Install and launch on Kubuntu

Use Python 3.11 or newer. From this project directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps -e .
.venv/bin/drivepak-manager
```

On a minimal Kubuntu installation, Qt may need `libxcb-cursor0` and the normal
XCB/Wayland runtime libraries. Install missing system packages deliberately
through Discover/apt; the project does not change your system configuration.
Image operations work immediately without installing the card helper.

To enable card access directly from a source installation:

```bash
./tools/build_linux.sh
sudo ./dist/drivepak-manager-1.0.3-linux/install-helper.sh
```

The helper is installed once, then updated when a release requires it. Device
operations authenticate through polkit. Keep the GUI unprivileged, and finish
any running backup/write before changing the installation.

To produce the Linux distribution, desktop launcher and helper:

```bash
./tools/build_linux.sh
tar -xzf dist/drivepak-manager-1.0.3-linux.tar.gz -C /tmp
/tmp/drivepak-manager-1.0.3-linux/install-user.sh
```

The user installer creates a private virtualenv, an Applications menu entry and
`~/.local/bin/drivepak-manager` / `drivepak` launchers. It downloads the pinned Qt
dependencies; the archive contains the application's wheel, documentation and
helper, with no reference data. See [packaging details](docs/INSTALL.md), including
offline wheelhouse installation and uninstall commands.

**For actual Linux card access**, review the generated helper files, then run
this administrator installation yourself:

```bash
sudo /tmp/drivepak-manager-1.0.3-linux/install-helper.sh
```

This installs only the constrained helper and its authentication policy. It does
not grant passwordless device access. Never use `sudo drivepak-manager`.

## Quick start

Connect the SD card reader after the app opens. In the selector, check the device
path, actual capacity and identity. A multi-slot reader may show one empty device
and another populated one; device names can change after reconnection. Store
backups on your computer or another independent persistent device, with room
for several card-sized files.

1. **Open image** or **Back up card / Open card**. For a card, choose independent
   backup storage; all physical bytes are copied and verified before a separate
   working image opens.
2. Expand a partition and choose a numbered disk. **Import disk(s)** presents
   explicit bank/slot mappings and an overwrite preview. Use the Files menu for
   insertion, extraction, rename and deletion on validated Disk BASIC disks.
3. **Save image as** creates and verifies a new image. **Review changes** shows
   the original, working file, saved image and edit history. **Discard changes**
   recreates the working copy from the unchanged original.
4. **Write image to card** shows the source image to read and asks you to choose
   the target anew. By default it creates a fresh verified complete backup first.
   You can explicitly turn that backup off. Review one final write confirmation,
   then wait for writing, flushing and independent device readback. A shorter
   image preserves the remainder of a larger card; partitions do not expand.

## Write an existing image directly to an SD card

1. Choose **Write image to card**, even with no image open in the editor.
2. Under **Image file to read from**, choose your existing `NEWIMAGE.img`.
   This file is opened read-only; the app will write its bytes to the card.
3. To skip creating a backup, clear **Create and verify a backup of the card first**.
   Direct mode overwrites existing card data without creating a recovery snapshot.
4. Choose the destination card by its model, capacity and identity, then review
   the single final **READ SOURCE IMAGE → WRITE DESTINATION CARD** confirmation.
5. Authenticate and wait for **Card write verified** before removing the card.

With the backup option enabled, the Save dialog is for a **new backup of the
card's current contents**, not the source image. Its suggested name begins
`BACKUP-before-NEWIMAGE-`. Use a different filename and keep existing backups.
Direct mode has no Save dialog. Both modes retain device protection, size checks,
source stability checks and mandatory readback verification. Direct writes require
the 1.0.3 helper; update it using the administrator command above after any running
card operation has finished.

[User guide](docs/USER_GUIDE.md) · [Safety and recovery](docs/SAFETY.md) ·
[Format and limitations](docs/FORMAT.md) · [Testing and spare-card checklist](docs/TESTING.md)

## Import a disk into T&D

1. Put the raw `.dsk` file on Linux. Extract it first if it arrived in a ZIP.
2. Open the card's verified working image, or use **Open image** on an existing
   backup. Reopening that backup does not require another physical-card backup.
3. Expand **T&D** and select the desired slot, for example **Disk 031**. Check
   its current contents before choosing to replace it.
4. Click **Import disk(s)** and select one or more disk-image files.
5. In the preview, check **destination partition = T&D**, the intended slot
   number for every source, and the contents being replaced. Multiple sources
   need distinct destination slots.
6. For a smaller disk, the default **clear unused logical tail to 0x00** option
   gives the unused part of the slot a defined value. FF-fill and preserving the
   destination's tail are explicit alternatives. Physical padding is retained.
7. Click **Import mapped disks**, then use **Save image as** for a named result.
8. When ready to transfer the result, use **Write image to card**, select the
   exact destination again, take its fresh verified current-state backup, review
   the final operation, and wait for write/readback verification.

Slot 31 is just an example. Choose an unused slot or deliberately replace
contents you have backed up. A full-slot import replaces that virtual disk's
logical payload in the working image.

If import is greyed out, wait for the current operation and read the layout
notice above the browser. Version 1.0.2 fixed an overly strict legacy default-drive
check; it preserves those references while allowing safe disk imports. Real
structural problems such as overlapping extents still prevent unsafe edits.

## Export disks, manage files and work with OS-9

Use **Export disk(s)** on selected disk rows. Ctrl/Shift-select several rows for
batch export, then review the destination mapping and choose an explicit export
size. Existing outputs are protected.

On a validated standard BASIC disk, the **Files** menu supports extraction,
insertion, rename and deletion. Extraction includes a `.drivepak.json` sidecar
for metadata preservation. Insertion asks for an 8.3 name and file type/ASCII
metadata; it never changes line endings or strips binary load records. The
directory filter and **Search disks** help find names.

For supported OS-9 partitions, double-click directories and use **Up** to return.
Files can be extracted, and complete volumes can be exported or replaced. A
replacement must match the existing partition's sector count; add a new OS-9
partition for a different size. Individual-file modification inside OS-9 is
outside this release's scope.

The **Partitions** menu creates empty BASIC banks or adds whole OS-9 volumes,
renames/deletes entries and assigns defaults. Other disk numbers stay in place.
Deletion retains old data but frees its space for later allocation; the dialog
lets you reassign or clear affected default-drive references.

## Discard changes and recover

**Discard changes** recreates the working copy from its unchanged original.
Working files remain on disk after closing so they can be recovered. Remove old
copies manually only after checking that the backups and saved results you need
are retained.

To restore an earlier complete snapshot:

1. Use **Verify backup**, then **Open image** on the earlier backup.
2. Choose **Write image to card** and explicitly select the destination.
3. Take a **new** backup of that card's present contents under another filename.
4. Confirm restoration of the earlier image and wait for readback verification.

Keep the earlier recovery snapshot throughout. Physical writes are not atomic;
cancellation, disconnection or a verification error can leave incomplete data.
The app retains the backup and does not automatically retry a failed destructive
write. [Complete safety and recovery details](docs/SAFETY.md).

## Supported formats and restrictions

* Card parsing implements the documented 24-bit, 512-byte-block profile with
  256 payload bytes per block. Reserved fields and original padding are retained.
  Ambiguous addressing and overlapping layouts disable unsafe edits.
* Raw floppy imports support 161280, 184320, 322560, 368640 and 737280 bytes.
  Headered JVC, DMK, VDK and HFE representations are rejected with an explanation.
* BASIC file editing supports the standard **68-granule** filesystem. Larger raw
  images can be transferred without assuming an extended BASIC filesystem.
* The OS-9 browser targets classic 256-byte-sector RBF volumes in supported OS-9
  partition entries. OS-9 floppies in type-2 banks can still be transferred as raw
  slots, and their existing default references are retained. Individual OS-9 file
  modification and OS-9/68k extended-sector variants are unsupported.
* The original floppy length/geometry is not stored in a slot. Full-slot export
  retains all 737280 logical bytes; smaller sizes explicitly omit the tail.
  Trimming trailing zero/FF bytes is not used to claim the original length.
* An inconsistent OS-9 extent in the reference image is reported rather than
  silently expanded into a neighbouring partition or repaired.
* Ordinary image operations are portable. Real card access targets Linux;
  Windows device access and the 4 GB reference are not validated release targets.

See [FORMAT.md](docs/FORMAT.md) for evidence, offsets, variants and uncertainties.

## Troubleshooting

| Symptom | What to check |
|---|---|
| Reader reports 0 bytes | Reseat the card, reconnect the reader and Refresh. Choose the populated device by capacity. Proprietary formatting does not prevent capacity detection; do not format an existing DrivePak card to make Linux mount it. |
| Numbered disks are missing | Expand a bank such as DOS or T&D, then select a numbered child row. |
| Import is greyed out | Wait for the current operation, then read the visible restriction. Use 1.0.2 or newer for the legacy default-label case. |
| A `.dsk` is rejected | Extract archives first and supply a supported raw sector representation. The extension alone does not identify its format. |
| Helper/authentication error | Follow the helper installation instructions and use a normal Kubuntu desktop session with a polkit agent. Keep the GUI unprivileged. |
| A backup no longer matches a card | Create a fresh complete snapshot of the selected target under a new name; retain older recovery images. |
| Write incomplete/unverified | Keep backups, read the stated error and use the explicit restoration workflow. |

## Command line

The CLI shares the engine; no GUI or server is required for image automation.
An image edit always names a separate output, and existing outputs are protected.

```bash
.venv/bin/drivepak --help
.venv/bin/drivepak new data/work/blank.img --capacity-mib 64 --disks 16
.venv/bin/drivepak inspect data/work/blank.img
.venv/bin/drivepak disk-export data/work/blank.img exports/slot.dsk --partition 0 --disk 0 --size 737280
.venv/bin/drivepak disk-import data/work/blank.img data/work/edited.img --map '0:3:/path/to/game.dsk' --tail zero
.venv/bin/drivepak insert data/work/edited.img data/work/with-file.img /path/to/GAME.BIN --partition 0 --disk 3 --type 2
.venv/bin/drivepak files data/work/with-file.img --partition 0 --disk 3
.venv/bin/drivepak extract data/work/with-file.img GAME.BIN exports/extracted.bin --partition 0 --disk 3
```

Full-slot exports are 737,280 logical bytes. Smaller explicit export sizes are
available; the original floppy length is not encoded in the slot. Raw standard
sector images are supported; headered JVC/DMK/VDK/HFE imports are rejected with
an explanation. File contents and metadata are preserved without text conversion.

Card commands are `devices`, `backup-card`, `verify-backup`, and `write-card`.
`write-card DEVICE IMAGE --backup SNAPSHOT` requires a verified **current-target**
full backup and independently checks correspondence. Alternatively, explicit
`--no-backup` selects direct mode and accepts the absence of a recovery snapshot.
Both require an interactive exact-device confirmation or
`--confirm-device /dev/EXACT_DEVICE` for automation. Both independently read back
the written card range. See SAFETY.md before use.

## Development and tests

The source separates card format (`format.py`), filesystems (`filesystems/`),
working images (`image.py`), streaming backups (`backup.py`), Linux discovery and
helper/client access (`devices.py`, `helper.py`, `linux.py`), GUI and CLI.

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
python3 tools/download_references.py
DRIVEPAK_REFERENCE=1 .venv/bin/python -m pytest -q -m reference
.venv/bin/python tools/desktop_smoke.py
./tools/build_linux.sh
.venv/bin/python tools/verify_package.py
```

Normal tests are offline and use small, independently constructed fixtures.
Reference integration is opt-in and needs about 8 GiB of temporary free space.
Downloads are streamed into `data/reference/`, safely extracted and locally
hashed; [provenance.json](docs/provenance.json) records actual sizes and SHA-256.
Those hashes establish reproducibility, not independent publisher authentication.
The 2 GB-labelled reference contains **1,866,210,304** image bytes.

Recorded 1.0.3 validation: **104 offline tests passed and 2 reference integration
tests passed**, with desktop startup and read-only selector inspection. This
includes a synthetic T&D slot-31 import that preserves defaults, padding and
unrelated data. Automated results are not physical-hardware proof; complete
readback and CoCoNet loading validation still need confirmation. See the
[spare-card checklist](docs/TESTING.md).

No account, network service, database, cloud dependency, or telemetry is used by
the application. Network access is used only for explicit dependency/reference
downloads. [License and attribution](THIRD_PARTY.md).
