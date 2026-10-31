# Changes

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
