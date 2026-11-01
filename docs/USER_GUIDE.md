# Using DrivePak Manager

## Start the app

In the project directory, run `./start-drivepak.sh`, or execute that script
from Dolphin. It uses the project's own Python environment and can be started
from any working directory. If the environment is missing, it explains how to
set it up. Run the app as your normal user; card access uses the separately
installed helper described in [INSTALL.md](INSTALL.md).

## Start with a copy

**Open image** accepts a raw `.img` and creates a separate verified working file.
The original path and working path stay visible. If the selected image has a
known backup manifest, it is verified as well. **Back up card / Open card** first
lists Linux devices. Choose an available external reader; if its volumes are
mounted, use the deliberate unmount button and wait for reinspection. Choose a
backup directory on another device. Wait for copying, saved-backup verification
and a second source-stability pass before the working image opens.

The banner distinguishes working changes, a saved image, backup status and an
actual verified card write. Saving an image does not write a card. The optional
**View → Technical details** panel shows addresses, defaults, diagnostics and free
regions. **File → Save diagnostics** works with unrecognised layouts as well.

## Add games as disks

Expand a Disk BASIC partition and choose a disk number. **Import disk(s)** opens
native file selection and then a mapping table: each source, destination bank,
zero-based slot, byte length and current contents are visible. Set slots explicitly;
duplicate destinations are rejected. Importing several disks is one rollback unit.

For example, expand **T&D**, select **Disk 031**, then choose **Import disk(s)**.
Select a raw `.dsk` from your Linux files, confirm **T&D** and slot **31** in the
mapping preview, and click **Import mapped disks**. This replaces that slot's
contents in the working copy. Save a new image, then use **Write image to card**
when ready to transfer it back through the verified backup/write workflow.

If import is disabled, a persistent explanation above the browser and the
action's tooltip identify the blocking layout issue. Informational notes about
preserved legacy default-drive labels do not block supported disk imports.

Choose how a smaller raw floppy fills its larger slot: clear unused logical data
to zero, fill it with FF, or keep the destination's existing tail. This never
changes physical padding. Headered emulator formats are rejected; supply raw
sector data. Supported sizes and interpretations are in FORMAT.md.

**Export disk(s)** transfers selected disk rows. Ctrl/Shift-select multiple rows
for batch export; review the resulting filenames and destination directory.
Choose the export length explicitly. Full slot exports all 737280 payload bytes.
Smaller choices omit the tail; they cannot prove the original file length. Existing
outputs are protected, and exported GUI disks include an explanatory JSON sidecar.

Use the current-directory filter or **Search disks** for filenames across BASIC
banks. Search reports unrecognised filesystems that were skipped. Unsupported
disks can still be transferred as raw slots when their extents are safe.

## Manage files

Select a BASIC disk to browse files and standard filesystem free space. **Files →
Insert file** accepts verbatim host-file bytes; select the 8.3 name, file type and
ASCII/binary metadata. Those settings never change line endings or binary bytes.
An extraction `.drivepak.json` sidecar restores the original metadata after its
checksum is validated. Replacement is an explicit option in the insert dialog.

Select file rows and use **Extract file(s)** to choose a directory. Each result
includes a metadata sidecar. **Rename file** changes the name, and **Delete file**
removes its directory/FAT references in the working image. Disk BASIC's usual
68-granule space limit applies even inside a much larger floppy slot. Invalid or
ambiguous filesystems show a concrete reason and disable unsafe file edits.

For an OS-9 partition, double-click directories and use **Up**. Files can be
extracted without conversion. **Export OS-9 volume** transfers the complete stored
partition payload; **Replace OS-9 volume** requires a validated raw volume of the
same sector count. Use a new partition for a differently sized volume. Individual
OS-9 file modification is outside this release. An inconsistent extent gets a
specific warning; diagnostic raw export does not pretend to repair it.

## Partitions and blank images

**Create blank image** asks for a physical capacity, card name and number of empty
DOS slots. It supplies no games, firmware or boot programs. Under **Partitions →
Create partition / Add OS-9 volume**, choose a name and empty BASIC bank size, or
select a raw OS-9 volume. Free physical space is displayed; existing partitions
are not moved and live disk numbers are preserved. New/renamed names allow 15
ASCII characters; use eight or fewer for Explorer's display compatibility.

Rename/delete operate on the selected partition. Delete leaves its old data
intact but makes its space available for future allocation. The dialog lists
affected default drives and lets you choose a compatible replacement or an
unassigned pointer. **Set default drive** explicitly assigns a selected bank or
OS-9 volume. Table entry positions are retained instead of compacted.

## Save, write and recover

For small updates, use **Card → Apply image edits to card** after editing the
working image. It prepares a local checkpoint and before/after sector journal,
checks the full image-sized card range matches the editor's original, writes
only changed sectors and verifies them independently. Partition/default edits
and disk/file changes are included. It still uses a working image and takes a
full comparison read; it avoids another whole-card write. **Recover card edits**
restores journaled before sectors when their contents still match either version
and the rest of the original-image range remains unchanged. Later unrelated
edits are rejected; recover successive journals in reverse order.
This partial recovery journal is not a complete backup. See [CARD_EDITS.md](CARD_EDITS.md).

**Save image as** writes and verifies a new file. **Review changes** shows paths
and the edit history. **Discard changes** recreates the working copy from its
unchanged original. Working files remain on disk after closing; their displayed
paths can be opened later. Manage old working files manually only after ensuring
you have the originals/backups and saved results you want.

**Write image to card** starts by showing the **Image file to read from**.
Choose an existing file such as `NEWIMAGE.img`; this works even without an image
open in the editor. The source is read-only. Choose the exact destination card
anew each time.

**Create and verify a backup of the card first** is enabled by default. Its Save
dialog asks for a new backup of the destination's current contents, with a
suggested `BACKUP-before-…` filename. It does not ask to save the source image.
Use a separate filename on independent storage and wait for backup verification.

For direct writing, clear that checkbox. No Save dialog appears and no recovery
backup is created. Existing card data will be overwritten; a failure may leave
partial contents. Keep any earlier recovery images yourself. Direct writing
requires the updated 1.0.3 helper (see INSTALL.md).

Review the single final dialog naming **READ SOURCE IMAGE**, **WRITE DESTINATION
CARD**, the backup or no-backup warning, written range and retained tail. Confirm
and authenticate. Both modes require independent card readback before reporting
success. Never assume a successful image save means a card has been written.

**Writing card**, **Flushing card** and **Reading back written card range** are
separate stages. A percentage applies only to its current stage. Flushing uses
an animated indicator because Linux does not provide a reliable percentage for
that call. Leave the card connected until **Card write verified**. Slow cards
can take tens of minutes; readback requires a further pass over the written data.
Version 1.0.4 flushes batches of up to 64 MiB so the kernel cannot queue the whole
image before writing reaches 100%. With an older helper, **Copying 100%** may
remain visible while gigabytes of queued writes finish, before readback begins.

Progress and cancellation are available for copies, transfers, search and card
operations. Cancelling an image edit rolls it back. Cancelling a physical write
can leave incomplete contents: keep the backup and follow the complete recovery
steps in SAFETY.md. To restore an older snapshot, open it as an image, then use
the normal write workflow with a new snapshot of the card's present contents.
Cancellation cannot interrupt a kernel flush already in progress; the app stays
responsive and waits for that I/O to return. Do not unplug the card or force-close
the app while waiting for the cancellation result.
