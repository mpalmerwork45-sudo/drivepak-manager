# Backup, write and recovery behavior

This is storage-management software with non-atomic physical writes. Software
verification reduces specific corruption risks; it does not prove hardware
compatibility or guarantee against loss, faulty flash, controller lies or power
failure. Keep recovery snapshots on independent storage. Physical-card testing
has not been performed for this release candidate.

## Normal desktop workflow

```text
Select card → verify complete backup → separate working image → edit
→ review → select destination → fresh verified current-target backup
→ final write confirmation → authenticate → write/flush/readback verify
```

The second snapshot prevents reuse of a stale backup and covers a separately
selected destination card. It is retained even if the final write review is
cancelled. The helper compares it with the entire current target again under
exclusive access immediately before writing. CLI users may name an existing
snapshot, but the same identity/capacity/full-content check is mandatory in
backup-first mode.

An explicit alternative is available under **Write image to card**: select the
source image, then clear **Create and verify a backup of the card first**.
This direct mode has no backup Save dialog. A visible warning and the single
final confirmation explain that existing card contents are overwritten without
creating a recovery snapshot. The choice defaults back to backup-first each
time. CLI users must explicitly pass `--no-backup` instead of `--backup`.
Direct mode retains device protection, exclusive access, source stability,
capacity/bounds checks, flushing and mandatory independent readback. It cannot
recover previous card contents unless an earlier backup exists elsewhere.

Each physical backup reads **every byte of the actual device capacity**, including
unallocated space, table slack, boot code, padding and unknown regions. Recognised
volume exports are never labelled physical backups. The backup filename is
published only after successful durable copying and independent verification.
A `.json` sidecar records identity, actual capacity, exact byte count, timestamp,
application version, SHA-256 and verified status. The sidecar is local provenance,
not a signed attestation. It cannot bypass the helper's full byte correspondence
check. A backup opened in the GUI is checked against its known backup sidecar.

Backups stream through bounded buffers. Partial reads are retried until the exact
requested count is obtained; EOF/read error aborts instead of inserting zeroes.
Short writes loop until complete; a zero write or ENOSPC aborts. The output is a
unique `.partial` file, flushed with fsync, independently reopened, length checked
and hashed, then the complete source is read again to establish stability.
Only then is it published without replacing another file; its directory is
synced. A failed/cancelled output remains uniquely named `.partial`, with **no
verified manifest**. Incomplete outputs are never silently resumed or deleted.

Working images are separate, durably copied and verified against their source
stream. Source and destination path equality and hard-link aliasing are rejected.
Saves use new output files with durable publication. Existing images, exports
and backups are never overwritten by save/export. The app never automatically
deletes old working images or any recovery backup. Work-image edits use an undo
journal for exceptions/cancellation; originals remain the recovery point if the
application or machine crashes.

Choose enough space for the original/backup, a working image, saved results,
fresh destination snapshot, and mutation undo data. A 2 GB card session can need
several card capacities of free independent storage. There is an early space
check and ENOSPC handling; free space can still change concurrently.

## Device protection

Read-only discovery uses lsblk, sysfs, mount information and active swap. It shows
model, actual bytes, path, serial/WWN or kernel disk sequence, transport and use
status. USB readers are eligible even with a false removable flag. Removable MMC
is supported. Other/internal transports are rejected. This intentionally excludes
ambiguous hardware rather than trusting a `/dev/sdX` name or removable bit alone.
Reader slots reporting zero or invalid capacity are visible but unavailable.
They cannot produce a verified empty backup or start a write. Media loss during
the exclusive open is reported as a missing/disconnected card, before card I/O.

The protection graph joins whole disks, partitions and sysfs holders/slaves,
including device mapper/crypt/LVM relationships. Root, boot, home and swap backing
devices are rejected, as are holders, mounted devices and read-only devices.
Existing raw block-device opens by other processes are checked as well; the
privileged endpoint requires process descriptor inspection to succeed.
Backup, working and source filesystem backing is checked through actual open
descriptors as well as paths. Unknown local backing fails closed. Parent/sibling
relationships are conservatively protected; an apparently separate partition on
the same target disk is not independent backup storage.

Unmount is a deliberate button in the card selector, using the existing UDisks
service and its permissions. It is never automatic during discovery. System
devices cannot be unmounted through this UI. The card is re-inspected after the
request and again before actual access. Enumeration and unmount inspection run
in a worker so the desktop remains responsive.

Device identity includes canonical path, major/minor, model, serial/WWN,
transport, actual capacity, sysfs path, read-only/external status and **diskseq**.
There is no remembered write destination. Missing disk sequence disables card
access. Changes after selection are rejected. The helper revalidates everything
after authentication, opens with `O_EXCL|O_NOFOLLOW`, holds an exclusive flock,
checks the descriptor's actual block identity, then rechecks topology and mounts.
Identity/sequence/capacity are checked between chunks. Removal/replacement or
identity change aborts. The system's own exclusive block-claim behavior and card
reader media-change reporting still need spare-card hardware validation.

## Privileged boundary

The GUI and CLI remain unprivileged. A root-owned wrapper uses isolated system
Python (`-I`) to run a fixed zipapp installed at a fixed path. The helper accepts
only complete snapshots and checked image writes (backup-first or explicitly
acknowledged direct mode). It does not run
shell strings, arbitrary commands, Python supplied by the caller, or install
actions. It does not open arbitrary output paths as root.

The desktop creates a private mode-0700 Unix socket directory. The helper and
caller check `SO_PEERCRED`; the authenticated `PKEXEC_UID` must match the caller.
The caller passes already-open regular-file descriptors with SCM_RIGHTS. For a
backup these are an empty new output owned by the caller and its independently
reopened read-only descriptor. Backup-first writes use read-only source and
recovery backup descriptors; direct writes use a single read-only source and an
explicit boolean no-backup acknowledgement. An old installed helper is rejected
before elevation for direct writes using its installed capability metadata.
Access modes, file type, identity and backing storage are
validated. The only privileged writable object opened by the helper is the
revalidated external block device. File locks and fingerprints protect the source
and snapshot against cooperating writers and detect changes during processing.

The polkit policy requires administrator authentication per operation in an active
desktop session; it provides no passwordless/permissive device rules. Install is
deliberate and documented in INSTALL.md. The GUI refuses to run as root. The
endpoint has tests for validation logic and shared I/O; it has not been exercised
with privileged real block-device access during development.

## Writing and verification

A final review names the exact path, model, stable identifier, byte capacity,
source image, current-target snapshot or explicit no-backup warning, and range. Too-small cards
are rejected before writing; there is no truncation option. A shorter distributable
image is distinguished from a complete physical snapshot. On a larger target the
unwritten tail is retained and no partition is expanded.

In backup-first mode, before the first write the helper hashes the recovery backup, source image and
whole current target, validates manifest identity/capacity, compares the target
with its recovery snapshot and checks that open sources remained stable. Stale,
foreign or modified backups fail before any destructive I/O. Direct mode skips
the snapshot comparison only after explicit acknowledgement. It hashes the
source before copying and checks that its identity/size/timestamps remain stable.

After writing the helper flushes/fsyncs the device, invalidates the Linux block
cache with BLKFLSBUF, then reads **the written range from the device** and compares
that SHA-256 with the stable source. Source hashing by itself is never called
write verification. The source, any supplied backup and device identity are checked again.
Controller durability/flash retention beyond kernel flush/readback is a hardware
property and remains outside the software's proof.

Writes are synced in batches of up to 64 MiB (including the final partial batch)
to bound queued dirty data. Flushing and preparation for readback have explicit
indeterminate progress stages. The final write percentage follows the final
batch sync, but only successful independent readback labels the card verified.
The older 1.0.3 helper could show **Copying 100%** while the kernel still flushed
an entire image queued in memory. Do not replace the helper during an operation.

Cancellation is observed between bounded I/O chunks. Once writing begins,
cancellation, unplugging, short write, flush error, source change or readback
mismatch is reported as **PHYSICAL WRITE INCOMPLETE OR UNVERIFIED**. No automatic
destructive retry occurs. A disconnected/crashed helper is also unverified.
Direct-mode failures explicitly state that no recovery backup was created;
recovery requires an existing backup or another known-good image.
An in-flight kernel write/flush cannot be interrupted safely by the application;
cancellation remains pending until the call returns. It has no forced timeout or
automatic destructive retry. The GUI event loop remains active while waiting.

## Manual recovery

1. Keep the earlier verified backup and its manifest. Do not overwrite/delete it.
2. Reconnect the spare card if needed and refresh/select it by identity. Do not
   assume the old device path still names the same hardware.
3. **Verify backup**, then **Open image** on the earlier snapshot you want restored.
   It becomes an unchanged original plus a separate working copy.
4. Choose **Write image to card**, explicitly select the destination and take a
   **new** complete snapshot of its current (possibly damaged) state under a new
   filename. This preserves the earlier recovery snapshot and establishes a
   current-target recovery point.
5. Review the final restore operation, authenticate, and wait for device readback
   verification. Keep both snapshots until later CoCo hardware checks pass.

The same recovery route is available in CLI: take `backup-card` to a new path,
then `write-card DEVICE EARLIER_IMAGE --backup NEW_CURRENT_SNAPSHOT` with the exact
device confirmation. Never pass the earlier image as proof of the target's
present contents unless the current-content check actually succeeds.

Development restriction: no real device write, erase, format, mount/unmount or
destructive test is authorized without the user's separate permission for that
specific hardware test. Read-only enumeration and regular-file simulation are
the only device-related actions used during development.
