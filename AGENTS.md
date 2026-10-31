# Project invariants

* Develop and test against regular files and simulated devices only. Never write,
  erase, format, mount/unmount or destructively test any attached storage without
  the user's separate authorization for that specific hardware test.
* Keep the GUI unprivileged. Only the installed, root-owned helper may open real
  block devices. Never install the helper or alter system configuration as part
  of development tests.
* Originals and verified backups are immutable to this application. Edit separate
  working images. Preserve all unrelated bytes, physical padding, unknown fields,
  table indices and virtual disk numbers. Fail closed on ambiguous edits.
* Backups are full capacity, streamed, durable and independently verified. The
  default card-write mode requires a current-target verified snapshot. The user
  has explicitly requested an optional direct-write mode: it requires a clear
  no-backup warning, explicit final confirmation and no-backup acknowledgement
  across the privileged boundary. Device protection, source stability, bounds,
  flushing and independent readback remain mandatory in both modes.
* Changed-sector card updates compare the complete image-sized current target
  range with the immutable editor original. Save and verify before/after recovery
  bytes plus a durable receipt on independent storage before writing. A changed-
  sector journal is never described as a complete physical backup. Preserve the
  original images and all journals; recovery rejects unknown/torn sector data.
* Use checked offsets and bounded buffers. A failed/cancelled mutation rolls back
  the working image. Partial card writes are failures requiring manual recovery.
* Reference downloads are research only, not redistributable application assets.
  Normal tests run offline with synthetic fixtures. Do not commit reference data,
  games, firmware, user images, backups or credentials.
