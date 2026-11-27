# Pending import and historical-state integration

Source base: PR99 `b63e90e895d08e9ba1a529f9be42dec1a94abcc3`.
Reused canonical producer: `bfcc1f5cae137cc27030c0cec6dc46a1641892c1`,
`src/carr_library/pending_backup.py` blob
`134d7534a8abf1b68b032ace804b2494853c1d90`.

## Changes

- One current backup service emits v5 when import journals or historical
  recovery state exist; existing no-import formats remain supported.
- Canonical pending request/batch/review/journal validation is reused, with
  current format-registry suffixes, actual import guards, shared relational
  checks and read-only streamed verification.
- Both historical v3/v4 manifest families are dispatched by exact fields.
  Alternate application state is inert, hash-bound and included on rebackup.
- Current assistant native ownership, scope checks, cooperative cancellation,
  source-bound reading recovery and audio sidecar validation remain in place.
- The translation namespace has a lazy typed-producer hook for
  `assistant.translation_state`. A declared namespace without that adapter
  fails explicitly. The integration expects `is_state_path`, `state_limit`,
  `state_paths`, `validate_state_tree`, `snapshot_guard`,
  `MAX_JOBS`, `MAX_CACHES`, and `MAX_SNAPSHOT_BYTES`.
- Historical-format tests build explicit earlier-layout fixtures. They do not
  change the current producer or remove current-format pending tests.

## Required execution gates

**NOT RUN for this patch:** the local executor was unavailable during preparation.
Earlier source-tree or Windows results are not evidence for this new patch.

Run the repository's strict `scripts/ci_check.py` runner, preserving zero skips,
for at least:

- `tests/storage/test_pending_backup.py`
- `tests/storage/test_complete_pending_backup.py`
- `tests/storage/test_backup_pending_integration.py`
- `tests/storage/test_library_backup.py`
- `tests/storage/test_library_backup_reading_state.py`
- `tests/storage/test_backup_reading_state.py`
- `tests/storage/test_backup_v3_compatibility.py`
- `tests/storage/test_backup_schema_identity.py`
- `tests/storage/test_backup_source_binding_review.py`
- `tests/storage/test_backup_productivity_state.py`
- `tests/storage/test_backup_recovery_and_verify.py`
- `tests/storage/test_backup_unified_audio.py`
- `tests/storage/test_backup_assistant_state.py`
- the complete `tests/assistant` selection
- existing portable snapshot and encrypted transfer selections
- the translation producer's new suites after composition

The actual OCR review/resume case from canonical source is retained separately
in `tests/storage/test_pending_backup_ocr.py`. It requires the actual local OCR
runtime plus its dependencies; a skip is not qualification. Native Windows
execution, physical-device/NVDA acceptance and full platform completion remain
separate gates.
