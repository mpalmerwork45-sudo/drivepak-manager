# Changes

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
