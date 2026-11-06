# Storage safety audit — 6 November 2026

This audit began at `1c428c6` (1.2.0). It reviews the privileged card boundary,
complete backups and full writes, changed-sector recovery, image transactions,
Disk BASIC/OS-9 file changes, and GUI/CLI confirmation paths. Fixes are released
as 1.2.1. It is a source review and failure-injection exercise, not an independent
security certification or proof against data loss.

No attached storage was opened, written, formatted, mounted or unmounted. Tests
used generated regular files, disposable copies of the cached reference, mocked
device topology and Unix-socket descriptor transport. The system helper was not
installed or updated during this audit. User images and backups were untouched.

## Findings and fixes

| Finding | Risk before the fix | Fix and regression evidence |
|---|---|---|
| Full writes checked device use before lengthy preflight reads | A newly busy or protected target could pass the earlier check | Recheck complete use/topology and actual source/backup backing immediately before the first write. Both write policies reject a simulated newly busy target without changing a byte |
| The confirmed full-write source was not bound across all later opens/authentication | A same-path replacement during backup verification could write different data | Carry device/inode/size/timestamps from GUI/CLI review to the actual open source and privileged helper. Reject mismatched/malformed identity before device access and monitor it during processing |
| GUI recovery did not bind the selected journal before final confirmation | Replacing both a journal and its valid receipt during confirmation could recover a different set of before bytes | Hash the selected journal in a worker before review and pass that digest to the helper. A regression swaps two individually valid recovery pairs; no card bytes are changed |
| Raw-user detection filtered descriptor paths beginning with `/dev/` | Another writer using a bind-mounted or other block-node alias could be missed | Inspect actual descriptor type and major/minor regardless of its path; alias regression detects the active user |
| Unmount used a newly fetched topology with an older protection decision | A device becoming protected between discovery and unmount could still receive the request | Evaluate identity/protection against the same topology and revalidate before each volume; mocks prove no UDisks request is sent for a newly protected device |
| A missing backup directory was created before storage validation | Even a rejected operation could create directories on the selected target | Require an existing destination folder before creating any output; regression checks no directory is created |
| Network filesystem storage was assumed independent | A loopback or remote export might itself use the target | Reject network backing whose independence cannot be established; require persistent local storage with known backing |
| Image undo writes were buffered | ENOSPC could first surface while rollback sought the undo file, leaving working-image changes unrolled | Write undo data unbuffered and exactly before changing image bytes. A capacity-limited undo-file test restores all earlier writes despite a later short write/ENOSPC |
| A failed rollback left the working image usable | A partial image could subsequently be saved or sent to a card | Mark the image unusable and block reads, edits, exports, saves and card use. The GUI offers discard/reopen; a persistent simulated I/O failure checks the original survives and discard makes a fresh verified copy |
| Discard checked the original only before making its new copy | An original changed between check and copy could replace the current working session | Compare the newly copied digest with the session's original digest before switching; racing replacement leaves current edits intact |
| Disk BASIC objects retained old allocation/directory state | Two inserts through the same object could silently replace the first file | Refresh and validate before each mutation, refresh after success, and validate rename/delete results. Sequential and separate-object regression cases preserve earlier files |
| Allocation plans could outlive another valid working-image edit | A file inserted during OS-9 source hashing could silently disappear when the older plan committed | Bind Disk BASIC and OS-9 validation/planning to the working-image fingerprint, then check it before the first mutation. Regressions preserve the later edit byte-for-byte and abort the stale plan |
| Whole OS-9 transfers validated one open, then imported an unbound second open | A same-length invalid replacement could be imported after validation | Bind imports to the validated descriptor fingerprint, recheck stability and validate the result inside the rollback unit. Both add and replace reject substitutions with complete byte preservation |
| Small disk/file reads trusted a prior path-size check | A growing file could cause an unbounded read or import inconsistent bytes | Read bounded lengths from checked regular-file descriptors and compare fingerprints; growth tests fail without changing the destination |
| OS-9 sources trusted path type/size after a separate open | A changed descriptor could be treated as an empty regular file, including during replacement | Take type and size from the opened descriptor and reject nonregular sources. Mocked descriptor types exercise both individual insertion and whole-volume access |

## Verification and packaging

The exact test results, commands and reference-image checks are recorded in
[TESTING.md](TESTING.md). Tests include actual SCM_RIGHTS transport with a
regular-file block adapter; they do not invoke physical writes. Headless GUI
tests exercise confirmation, failure handling and recovery controls.

Use the **1.2.1 application and helper together**. Full-image writes now require
`full_write_version: 2`; older helpers are rejected before elevation. Existing
backup manifests and DPEDIT01 changed-sector journals remain compatible. The
helper is still a fixed stdlib-only program, and the GUI remains unprivileged.
Installation instructions are in [INSTALL.md](INSTALL.md).

## Limits retained

* Physical writes are non-atomic. Failure, removal, power loss or a lying device
  can still damage a card. Preserve complete verified backups on independent
  storage. No automatic destructive retry or rollback is attempted.
* Working-image undo is for caught exceptions/cancellation, not crash recovery.
  A rollback I/O failure requires a fresh working copy from an unchanged original
  or backup. Do not manually reopen an unusable working file as a trusted image.
* Fingerprints and advisory locks detect ordinary changes; they do not control
  privileged or deliberately noncooperating writers. Stop other disk writers and
  do not change mounts during an operation. In particular, a concurrent path or
  mount substitution during backup output creation can leave an empty partial
  file before the helper rejects its actual backing; no backup data is copied
  until the helper has validated the descriptors.
* A journal covers only recorded sectors. Recovery checks the complete original
  image range and refuses unrelated later changes or torn sectors. It cannot
  replace a complete physical backup; larger-card tails lie outside a shorter
  original image's comparison.
* Linux exclusive claims, reader media-change reporting, flush/readback behavior
  on hardware, and actual CoCoNet program loading still need the spare-card
  checklist. Automated results are not physical-hardware validation.
