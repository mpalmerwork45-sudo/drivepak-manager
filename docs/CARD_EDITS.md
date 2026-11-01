# Apply image edits without rewriting the whole card

Use **Card → Apply image edits to card** after importing disks, editing BASIC
files or changing partitions in the working image. It supports all existing
image-editing operations, including partition creation/deletion/default changes.
It writes only changed physical sectors and independently verifies those sectors.
Use version 1.1.1 and its updated privileged helper. Its safety audit tightened
recovery correspondence and the saved-receipt checks; the 1.1.0 helper is rejected.

This workflow continues to use a separate working image. It does **not** provide
on-demand browsing of a physical card. **Back up card / Open card** gives you a
verified complete original and a working image; alternatively open the exact
image you previously wrote to this card. Keep complete backups on independent
storage. Source images and earlier snapshots remain unchanged.

1. Open the matching original image or back up/open the card, then make your edits.
2. Choose **Card → Apply image edits to card**, and explicitly select the card.
3. The app verifies a new local image checkpoint and prepares a `.dpp` journal
   containing before/after bytes for every changed 512-byte physical sector.
   It flushes and reopens the journal, compares saved header/record bytes against
   what was generated, and binds the later write to that journal's SHA-256.
   This scans local images and requires room for another complete image plus
   journal data (at worst about twice the changed physical data).
4. Review the exact destination, changes, sector/byte counts and journal path in
   one final confirmation. The helper authenticates without elevating the GUI.
5. The helper independently reads the card's entire image-sized range and checks
   it matches the editor original. It also checks every affected before sector.
   A mismatch aborts before any card write; back up/open the current card if its
   contents legitimately changed. This comparison still takes a full read pass.
6. Before writing, the helper verifies the journal, then waits for the desktop to
   durably save its `.json` recovery receipt. The desktop passes the published
   receipt's read-only descriptor. The helper checks its exact content, ownership,
   durability and actual independent storage, and rechecks current device use.
   A success message alone is insufficient. Failure means no edits are written.
   Only then are the changed sectors written, synced in
   bounded batches, independently read back and compared. Larger-card tails and
   every other sector remain untouched. New partitions do not expand themselves.

After success, the prepared local checkpoint becomes the editor's new immutable
original, so subsequent edits can be applied again. The old original, checkpoint,
journal and receipt are retained. For a small disk import this avoids a full-card
write; the required full original comparison may still take several minutes.
No physical-card behavior has been validated by the development tests.

## Recovery

Choose **Card → Recover card edits**, select the `.dpp` file, explicitly select the
card, then review the final confirmation. The matching original `.dpp.json`
receipt is required. The helper verifies journal identity/digest, reader identity
and capacity, then reads the **complete image-sized card range** before writing.
Each affected sector must match either its before or after version; every other
byte in that range must match the original. The full checksum is compared by
substituting known before bytes in a bounded memory buffer, without modifying the
card during this check. This handles a mixture from an interrupted update while
rejecting later unrelated edits or a different card in the same USB reader.
It restores the before bytes and independently reads them back. The receipt from
that recovery is saved separately in the working folder.

An edit journal is a **partial recovery file, not a complete card backup**. A
torn sector matching neither version, unrelated later edits anywhere in the
original-image range, or a different target is rejected. If you applied several
successive journals, recover the most recent first. Bytes beyond a shorter
original image are preserved, but are not covered by its recovery comparison.
Use a complete verified backup for broader
recovery. Interrupted updates are never automatically retried or rolled back on
the physical card. Keep the journal, receipt and full backups; do not install a
helper update or unplug the card during an operation.

See [the 1.1.1 audit](AUDIT-1.1.1.md) for findings, tests and hardware limits.

## CLI

```bash
.venv/bin/drivepak prepare-edits original.img modified.img edits.dpp
.venv/bin/drivepak apply-edits /dev/EXACT_CARD edits.dpp --receipt edits.dpp.json
.venv/bin/drivepak recover-edits /dev/EXACT_CARD edits.dpp --receipt recovery-attempt.json
```

Physical writes use the existing exact-device interactive confirmation, or an
explicit `--confirm-device /dev/EXACT_CARD` for automation. Save the initial
receipt as `edits.dpp.json`; recovery discovers it next to the journal. For
offline regular-file simulation use `simulate-apply-edits DEVICE_FILE JOURNAL
--receipt JOURNAL.json` and `--recover --receipt NEW_RECOVERY_RECEIPT.json`.

## Journal format

`DPEDIT01` is an application recovery format, not a Drive Pak card field. Its
56-byte header contains an 8-byte identifier, big-endian 64-bit original-image
length and record count, and the original's 32-byte SHA-256. Each 1,032-byte
record contains a big-endian 64-bit byte offset, 512 before bytes, and 512 after
bytes. Offsets must be aligned, strictly increasing and within the original
image length; before and after must differ. Exact length, source fingerprints,
capacity, hashes, device identity and bounds are checked. All processing uses
bounded buffers. No trailing data or inferred disk geometry is accepted.
