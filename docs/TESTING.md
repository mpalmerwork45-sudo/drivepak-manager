# Verification and hardware status

Automated tests use regular files. **No real device was written, erased,
formatted, mounted/unmounted or used for destructive tests.** Read-only Linux
enumeration was exercised. The actual PySide6 Widgets interface was launched on
the X desktop, browsed and visually inspected. The helper was staged under `/tmp`,
with no system installation or configuration change.

Recorded results (2026-10-31): **129 offline tests passed, 2 reference tests
skipped by default; both opt-in reference integration tests passed**.

## Reproduce

After the README's source installation, run from the repository root:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python -m pytest -q
python3 tools/download_references.py
DRIVEPAK_REFERENCE=1 .venv/bin/python -m pytest -q -m reference
.venv/bin/python tools/desktop_smoke.py
./tools/build_linux.sh
.venv/bin/python tools/verify_package.py
.venv/bin/python -m pip check
```

Normal tests need no internet, card, privilege, account or downloaded media.
Explicit-byte builders in `tests/conftest.py` generate small synthetic cards and
an 8 KiB OS-9 volume independently of production creation/offset functions.

The authentication-denial and helper descriptor tests use local Unix sockets.
A restrictive sandbox denying `AF_UNIX` binding must explicitly permit local
IPC or run the offline suite outside that sandbox. This does not authorize real
card I/O. Headless GUI tests use Qt's offscreen plugin and substitute native
dialogs with controlled responses.

Full-image integration is opt-in. Keep about 8 GiB free for extraction and
disposable working/saved images. `pytest --basetemp` can select another test
filesystem. The 2 GB-labelled image actually contains 1,866,210,304 bytes. Its
original is read-only and SHA-256 checked before/after integration. The tests
copy/save it unchanged, round-trip a full slot, check three independently
calculated directories and compare every byte outside a renamed directory field.

Measured with `/usr/bin/time -v`, that complete reference run took about 13
seconds and used **85,064 KiB peak resident memory** (about 83 MiB). Copy/hash
buffers are 4 MiB; payload transfers use bounded sector batches. These are
observations from this desktop, not performance guarantees for other systems.

## Coverage

| Area | Checks |
|---|---|
| Format | Non-magic names, relocated tables, deleted-before-live entries, terminators, counts around 128 and 1023, truncation, extension ambiguity, bounds, overlaps, defaults, zero-length entries |
| Preservation | Byte-identical copies, independent offsets, non-FF padding, reserved header/entry bytes, unaffected partitions and logical tails |
| Disks | Every explicit export size, round trips, smaller imports/tail choices, duplicate mapping rejection, prevalidated batch failures and rollback |
| BASIC files | Exact binary data and metadata, empty/full-sector/full-granule/max lengths, allocation across track 17, sidecars, rename/delete, deleted entries, orphan/cyclic allocation and disk full |
| OS-9 | Root/descriptors/segments/names, extraction, complete volume add/export/replace, invalid extents and explicitly acknowledged diagnostic export |
| Backups/write | Complete simulated snapshots, durable reopen/hash, restore/readback, explicit no-backup writes/readback/cancellation, bounded sync batches and final-sync percentage order, larger-target tail, stale/foreign/modified provenance, second target and racing output protection |
| I/O failures | Short reads/writes, EOF/EIO, zero writes, ENOSPC/space checks, batch flush failure in both write modes, cancellation before/during writing or blocked flushing, replacement, source instability, readback mismatch and missing privilege/authentication |
| Device protection | USB with false removable flag, empty/zero-capacity slots, media removal after selection, internal/system/root/home/swap paths, parent/partition/mapper relationships, backing conflicts, mounts, disk sequence and other raw opens |
| Helper protocol | Real SCM_RIGHTS transport, descriptor restrictions, exclusive flags, snapshot/backup-first/direct dispatch and readback using a regular-file adapter; direct acknowledgement and old-helper rejection |
| GUI | Open/browse/import/export/save, file insert/rename/extract/delete, partition create/rename/delete, OS-9 browse/extract, simulated backup-first and standalone direct write, one final warning, no direct-mode Save dialog, unchanged source, cancellation/errors, heartbeat during blocked flushing, queued main-thread progress/completions and fast chained tasks, progress above 4 GiB |
| Packaging | No media/private data, fixed helper module inventory, syntax/licenses/auth policy, offline clean user installation and staged helper installation |

The shared engine and helper protocol are exercised, but kernel/reader behavior
is not reproduced perfectly. Privileged access to an actual card and CoCoNet
firmware compatibility have not been tested. Hardware compatibility and loss
prevention are not claimed to be proven.

Changed-sector tests independently compare complete simulated card contents
after disk/file/partition/default edits, including unrelated regions and larger
tails. They cover malformed/overlapping/truncated/out-of-range journals, a stale
baseline, failed recovery receipt publication, cancellation, readback mismatch,
mixed before/after recovery, foreign targets and unknown/torn sector rejection.
GUI and CLI flows run through preparation, partial application and recovery; the
real helper SCM_RIGHTS test exercises its durable-receipt handshake with fake
block open/stat/ioctl adapters. No actual card is opened by these tests.

## Installation verification

The Linux package was installed offline into a fresh temporary virtualenv using
the four pinned cached Qt wheels. Application/bin/desktop/icon/doc paths were
redirected to `/tmp` with `DRIVEPAK_PREFIX`, `DRIVEPAK_BIN_DIR` and
`DRIVEPAK_DATA_DIR`. Installed CLI startup and image inspection were checked.
The helper installer used `DESTDIR=/tmp/drivepak-helper-stage`. Build products
are under ignored `dist/`; no endpoint was installed system-wide.
The installed desktop entrypoint was also launched on the actual X desktop;
the smoke tool supports `--installed-launcher /absolute/path/to/drivepak-manager`
to repeat that startup check.

## Later manual spare-card checklist

Perform these only when you separately choose and authorize the specific
hardware test. Use a recoverable spare card. Record Kubuntu/kernel/Python
versions, reader model/serial, card brand/capacity, USB port and results.

- [ ] Review and install the helper yourself. Launch the GUI unprivileged and
  check the polkit prompt identifies the expected helper.
- [ ] Check that system/internal disks are protected. Select the exact spare
  reader and check identity, actual bytes and use status. If needed, deliberately
  unmount its volumes through UDisks and wait for reinspection.
- [ ] Take a complete initial backup on another persistent device and use
  **Verify backup**. Keep this snapshot and manifest throughout testing.
- [ ] Reinsert the spare and check media-change detection: an old selection must
  not silently authorize another card. Refresh/select again.
- [ ] Edit the separate working copy using your own test disk/program. Extract
  for byte comparison, save a new named image and review pending/default changes.
- [ ] Select **Write image to card**, choose the spare again, take its fresh
  current-state snapshot, review the final destination and write. Wait for
  flush and complete written-range readback verification.
- [ ] Safely reconnect/use the card in the actual Drive Pak/CoCoNet hardware.
  Check disk selection, directories and loading the test program. This is the
  firmware/reader compatibility test absent from automated results.
- [ ] Optional direct-mode test, retaining the initial verified backup elsewhere:
  choose the saved source image, clear the backup checkbox, check the final
  no-backup warning, write the same spare and wait for independent readback.
- [ ] Optional changed-sector test: open a fresh verified card backup, edit a test
  file/slot, choose **Apply image edits to card**, check the planned byte count
  and retain its journal/receipt. Wait for edited-sector readback, test the correct
  bank on the CoCo, then use **Recover card edits** and verify the before contents.
- [ ] Return the spare to the computer. Open/verify the **initial** backup, take
  a **new** snapshot of its present contents, and explicitly restore the initial
  image through the normal write/readback workflow.
- [ ] Take a complete read-only backup after restoration and compare its digest
  with the initial snapshot. Retain recovery copies until satisfied.
- [ ] Optional, separately authorized spare-card failure drills: cancellation,
  media removal/replacement or induced readback failure. Confirm incomplete/
  unverified status, no automatic destructive retry and successful manual recovery
  from the earlier snapshot after a fresh current-state backup.

The 4 GB reference and other firmware/layout variants remain unvalidated. See
FORMAT.md for the supported profile, standard 68-granule BASIC file editing,
256-byte OS-9 sectors and the reference's inconsistent SIERRA extent.
