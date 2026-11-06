# Changes

## 1.2.0

* Added individual OS-9 file insertion, explicit replacement, rename and deletion
  in working images. Existing browsing and full-volume transfers remain available.
* File edits require classic 256-byte-sector RBF and one sector per allocation
  cluster. The reachable tree, bitmap and file extents are validated first;
  linked descriptors, crosslinks, corruption and ambiguous layouts fail closed.
  Boot-referenced file replacement and deletion are rejected, and unknown
  allocated bitmap bits are preserved. Directories can be browsed but not edited.
* OS-9 extraction sidecars bind metadata to file bytes with SHA-256 for verified
  reinsertion. Existing file metadata is preserved on rename and replacement
  unless explicitly overridden. See [FORMAT.md](FORMAT.md) for date policy and
  the independently implemented RBF format references.
* The 1.1.1 card helper remains compatible; OS-9 file changes are applied to a
  working image, then saved or written through the existing card workflows.

## 1.1.1

Safety audit of changed-sector editing and recovery; see [AUDIT-1.1.1.md](AUDIT-1.1.1.md).

* Recovery now checks every byte in the original-image range, allowing only the
  journal's known before/after mixtures. It rejects unrelated later changes and
  another card sharing the USB reader identity and affected sector contents.
* Generated journals are flushed, reopened and compared with the intended
  header/records. The prepared checksum is carried through review to the helper,
  preventing different journal contents from being written after review.
* Before card edits, the helper verifies the actual published recovery receipt
  descriptor, contents, durability and independent backing storage. It rechecks
  target use status then, and monitors receipt stability throughout the write.
  Plain success acknowledgements and deleted source/recovery files are rejected.
* Empty filesystem mapping results now fail closed instead of implying independent
  storage. Unknown backing cannot authorize card access.
* Added failure-injection and actual Unix-socket protocol tests. No physical card
  was accessed, and no system helper installation was performed during the audit.

Install the **1.1.1 helper** before applying or recovering card edits. Older
helpers are rejected before elevation. Existing DPEDIT01 journals remain readable,
subject to the stricter recovery correspondence check. Full backups are still
needed for torn sectors or broader recovery; physical writes remain non-atomic.

## 1.1.0

* Added **Apply image edits to card** for all existing image mutations. It prepares
  an immutable local checkpoint and durable before/after sector journal, rejects
  a card whose image-sized range differs from the editor original, then writes
  and independently verifies only changed sectors. Unrelated regions are retained.
* Added **Recover card edits** and CLI preparation/apply/recovery/simulation.
  The helper waits for a durable recovery receipt before the first write. Partial
  recovery journals are explicitly distinguished from complete physical backups.
* Reader/system/backing-device protection, exclusive access, identity checks,
  bounded sync, cancellation and failure handling remain at the privileged boundary.
* The desktop shows the startup BASIC bank and the bank/disk commands for the
  selected disk, addressing confusion between e.g. DOS #125 and T&D #125.
* Prevented duplicate selection signals from starting another directory read and
  clearing a just-refreshed file view. Busy GUI controls no longer inspect image
  metadata while a worker uses it; shared seek/read/write operations are locked.
  Added concurrent I/O and end-to-end GUI/CLI/helper checks.

This workflow uses local working images; on-demand physical-card browsing is
not implemented. Development performed no card writes or other destructive
hardware tests. Update the helper to 1.1.0 to enable changed-sector operations.

## 1.0.4

* Card writes flush at most 64 MiB per batch and flush the final batch before
  reporting writing at 100%. This prevents queuing an entire image in Linux RAM
  while a slow reader still has gigabytes left to write.
* Explicit indeterminate **Flushing card** and **Preparing independent card
  readback** stages replace the misleading Copying 100% pause. Verification
  reports its phase before the first read. Backup/image saves also name flushing.
* Cancellation waits for an outstanding kernel flush to return; a cancelled or
  failed write remains incomplete/unverified. Flush errors never report success.
* GUI progress and completion use queued QObject slots on the desktop thread,
  with results available before completion. This fixes intermittent refresh
  races when a completed edit immediately starts another directory-read task.
* Added bounded-batch byte comparisons, injected flush errors in both write
  modes, a blocked-flush GUI heartbeat/cancellation test and fast chained tasks.

The running 1.0.3 helper must finish before installing this update. Development
used simulated devices only; no attached card was written or interrupted.

## 1.0.3

* Added an explicit image-file-to-card workflow, available without opening an
  editor session. The source is read-only; the exact source and destination are
  named in the final confirmation.
* Backup-first remains the default. Clearing the backup checkbox enables direct
  writing with a no-recovery-backup warning and one final confirmation. There is
  no Save dialog in this mode. CLI automation supports explicit `--no-backup`.
* Both paths share size checks, source stability checks, protected/exclusive
  device access, identity revalidation, flushing and independent device readback.
  Cancellation or failure remains incomplete/unverified with no automatic retry.
* Backup Save dialogs now identify their purpose and suggest a distinct
  `BACKUP-before-…` filename. Selecting the source image as backup is rejected.
* Direct writes require the updated 1.0.3 helper. A capability check explains how
  to update an older helper before elevation or opening a device. The privileged
  endpoint requires an explicit boolean no-backup acknowledgement.

## 1.0.2

* Corrected an overly strict default-drive check that disabled all image editing
  when an OS-9 default referenced a valid OS-9 floppy in a type-2 bank. Partition
  classification is advisory; existing pointers and entry types are preserved.
* Structural checks for bounds, overlapping regions, ambiguous addressing and
  invalid/dangling defaults still prevent unsafe edits.
* Layout restrictions stay visible while browsing, and disabled import actions
  explain their reason through a tooltip/status message.
* Added a synthetic legacy-card regression that imports a raw floppy into T&D
  slot 31 and verifies that defaults, padding, the OS-9 volume and every unrelated
  byte are unchanged. The GUI mapping and preserved-source workflow are covered.

## 1.0.1

* Empty USB reader slots no longer appear available. Zero/missing/invalid card
  capacities prevent backup and write selection, including CLI/helper checks.
* Backup creation and verification reject zero capacity, so an empty file cannot
  be labelled a verified physical backup even with a matching manifest.
* The helper explains missing/disconnected media during device open and retains
  exclusive access flags. Existing backup and write verification remains required.
* Device paths are visible in the first column; long status messages have tooltips
  and a readable explanation below the table. Refresh requires a fresh selection.
* Added regression tests for empty readers, media removal, invalid capacities,
  empty manifests and the disabled/enabled selection workflow.

Validation: 91 offline tests and 2 reference-image integration tests passed. The
actual read-only device selector was inspected with empty reader slots. No card
was written or unmounted. If Linux still reports 0 bytes, the card/reader must
be detected with nonzero capacity before backing up; see INSTALL.md.
