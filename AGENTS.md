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
  Full writes bind the reviewed source fingerprint through descriptor transfer
  and repeat complete target-use/storage validation after preflight reads,
  immediately before writing. Older helpers lacking these checks must fail closed.
* Changed-sector card updates compare the complete image-sized current target
  range with the immutable editor original. Save and verify before/after recovery
  bytes plus a durable receipt on independent storage before writing. A changed-
  sector journal is never described as a complete physical backup. Preserve the
  original images and all journals; recovery rejects unknown/torn sector data.
  Recovery must also compare every unjournaled byte within the original image
  range; reader serial plus matching edited sectors alone is insufficient.
  Bind application to the reviewed journal checksum. The privileged helper must
  verify the published receipt's actual descriptor, content and storage before
  writing, and recheck current target use status at that checkpoint.
* Use checked offsets and bounded buffers. A failed/cancelled mutation rolls back
  the working image. Partial card writes are failures requiring manual recovery.
  Undo bytes must be written exactly and unbuffered before image mutation. If
  rollback itself fails, mark the session unusable and block export/save/card use;
  recovery creates a fresh copy of the unchanged original. Filesystem allocation
  plans must reject working-image changes that occur during their preparation.
* Reference downloads are research only, not redistributable application assets.
  Normal tests run offline with synthetic fixtures. Do not commit reference data,
  games, firmware, user images, backups or credentials.
