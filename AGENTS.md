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
* Backups are full capacity, streamed, durable and independently verified. A card
  write requires a verified snapshot matching its current bytes and identity.
* Use checked offsets and bounded buffers. A failed/cancelled mutation rolls back
  the working image. Partial card writes are failures requiring manual recovery.
* Reference downloads are research only, not redistributable application assets.
  Normal tests run offline with synthetic fixtures. Do not commit reference data,
  games, firmware, user images, backups or credentials.
