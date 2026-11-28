# Typed streamed-text backup on the current library families

This composition extends the existing complete-library and pending-import
backup validators. It retains the r6 large-PDF byte budgets, strict optional
path inspection, the existing folder cursor schema, both current transcription
namespaces, the existing recording adapter and the live-reading sidecar.

## Source authority

An explicitly streamed UTF-8 import request uses request version 2 and its
`carr-streamed-text-profile/1` maximum, capped at 128 MiB. Its journal carries
the exact `strict-utf8-stream-v1` extraction configuration. Pending backup
version 3 records a canonical `source_profiles` inventory derived from those
requests and jobs. Rehashing an outer manifest or increasing an asset size
does not create source admission. Verification reconstructs the required
profile inventory and compares actual original bytes and checked fragments.

Completed catalog originals obtain a streamed-text allowance only from the
sealed catalog configuration. The catalog copy helper carries that typed
profile to retained-file verification and streaming copy. Ordinary originals
retain their existing 64 MiB bound. Independently validated PDFs keep their
existing larger file-backed bound and each saved request's `pdf_max_bytes`.
A request cannot combine a text profile and a PDF allowance.

## Pending versions and scan continuity

Pending version 1 remains readable, including released r6 components that
already contain the current batch-version-2 cursor. Version 2 adds an explicit
`scan_pages` summary. Version 3 adds typed text `source_profiles` and keeps
`scan_pages` present, including an empty list when there is no saved scan.

The summary uses the actual current batch-chain fields:
`batch_id`, `previous_batch_id`, `next_batch_id`,
`has_continuation`, `unscanned_sources=external_not_copied` and
`restore_traversal=inactive`. It is compared against the copied batch records.
No alternate scan implementation or invented page ordinal is introduced.

Restoration copies only manifest-bound originals. It preserves saved cursor
history and sets the existing `review_required` flag, so resuming an external
folder scan requires an explicit new source selection. Import requests lose
their old process attempt, publication candidate and obsolete publication
watermark; an explicit import resume verifies the restored original and journal.

## Focused verification

`tests/storage/test_streamed_source_backup.py` contains 12 expected cases:
a real text input above 64 MiB through prepare, restore, isolated resume,
verified quotation and rebackup; a partial checked-fragment resume; four
forged profile/manifest refusals; a forged checkpoint refusal; cancellation;
mixed streamed text and saved folder continuation; two pending-version
compatibility cases; and simultaneous typed text plus an independent real
PDF above 64 MiB.

These are source-defined gates. Their inclusion is not an execution result.
The existing large-PDF, pending backup, catalog-copy, strict inaccessible-root,
speech-sidecar and audio/recording backup regressions remain required in the
composed source and installed-package runs.
