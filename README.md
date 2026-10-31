# DrivePak Manager

A native Python/PySide6 desktop utility for Roger Taylor's **CoCoNet MicroSD
Drive Pak** proprietary card layout. Supports working images, Disk BASIC disk
and file management, OS-9 volume transfer and browsing, and authenticated Linux
card backup/write with independent verification. This is the older Drive Pak
layout, distinct from CoCo SDC.

The desktop runs as your normal user. Card access uses a separately installed,
root-owned helper with polkit authentication. Development tests use regular
files and simulated devices; **physical-card compatibility has not yet been
validated**. No real device was written, erased or unmounted during development.

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

To produce the Linux distribution, desktop launcher and helper:

```bash
./tools/build_linux.sh
tar -xzf dist/drivepak-manager-1.0.0-linux.tar.gz -C /tmp
/tmp/drivepak-manager-1.0.0-linux/install-user.sh
```

The user installer creates a private virtualenv, an Applications menu entry and
`~/.local/bin/drivepak-manager` / `drivepak` launchers. It downloads the pinned Qt
dependencies; the archive contains the application's wheel, documentation and
helper, with no reference data. See [packaging details](docs/INSTALL.md), including
offline wheelhouse installation and uninstall commands.

**For actual Linux card access**, review the generated helper files, then run
this administrator installation yourself:

```bash
sudo /tmp/drivepak-manager-1.0.0-linux/install-helper.sh
```

This installs only the constrained helper and its authentication policy. It does
not grant passwordless device access. Never use `sudo drivepak-manager`.

## Quick start

1. **Open image** or **Back up card / Open card**. For a card, choose independent
   backup storage; all physical bytes are copied and verified before a separate
   working image opens.
2. Expand a partition and choose a numbered disk. **Import disk(s)** presents
   explicit bank/slot mappings and an overwrite preview. Use the Files menu for
   insertion, extraction, rename and deletion on validated Disk BASIC disks.
3. **Save image as** creates and verifies a new image. **Review changes** shows
   the original, working file, saved image and edit history. **Discard changes**
   recreates the working copy from the unchanged original.
4. **Write image to card** asks you to choose the target anew, takes a fresh
   verified complete backup of that target, presents one final write review,
   writes, flushes and reads the written range back. A shorter image preserves
   the remainder of a larger card; partitions do not expand.

[User guide](docs/USER_GUIDE.md) · [Safety and recovery](docs/SAFETY.md) ·
[Format and limitations](docs/FORMAT.md) · [Testing and spare-card checklist](docs/TESTING.md)

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
`write-card` requires a verified **current-target** full backup, independently
checks correspondence, and requires an interactive exact-device confirmation
or `--confirm-device /dev/EXACT_DEVICE` for automation. See SAFETY.md before use.

## Development and tests

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
python3 tools/download_references.py
DRIVEPAK_REFERENCE=1 .venv/bin/python -m pytest -q -m reference
.venv/bin/python tools/desktop_smoke.py
./tools/build_linux.sh
```

Normal tests are offline and use small, independently constructed fixtures.
Reference integration is opt-in and needs about 8 GiB of temporary free space.
Downloads are streamed into `data/reference/`, safely extracted and locally
hashed; [provenance.json](docs/provenance.json) records actual sizes and SHA-256.
Those hashes establish reproducibility, not independent publisher authentication.
The 2 GB-labelled reference contains **1,866,210,304** image bytes.

No account, network service, database, cloud dependency, or telemetry is used by
the application. Network access is used only for explicit dependency/reference
downloads. [License and attribution](THIRD_PARTY.md).
