# Backup format 5: source validation receipt

Date: 2026-11-27. This receipt covers the isolated additive pending-import
integration based on `5ea7596eddd7d58bd4871f922a380735ab559931`.
Production source is frozen at `a31907bf22b5575dcff56deee5b2d20c388a33b3`;
this receipt adds documentation only.

## Source and execution boundary

The canonical pending producer/helper and its original 28-case regression suite
come from `33dcc42edd6781b9aeb02dcaaf016b386bcf37b6`. The enclosing recovery
suite is unchanged from `642ed98ca33399529b1058381545d9ffccef2135`, including
prepared-before-first-worker-launch recovery after external source deletion.
Current sidecar validators and the shared `verify_backup` implementation are
retained. No import worker, assistant, audio engine or UI source was replaced.

Runs use `python scripts/ci_check.py --jobs 2` with the exact suite inventories
below. That gate runs each suite in an isolated subprocess with network disabled,
requires at least one passing test and rejects skips, XFAIL and XPASS. Tests use
the worktree source and the project's pinned dependency environment. Temporary
files use a RAM-backed local directory; OCR uses the prepared pinned tessdata.

Observed runtime: Linux, Python 3.12.14. Dependency versions: `pytest 9.0.2`, `pypdf 7.1.1`, `pypdfium2 5.13.0`, `python-docx 1.2.0`, `Pillow 13.0.0`.

## Strict gate results

### Broad affected gate

Source commit: `437e89f393779530ec70fd5857115c82d55019d1`. **15 suites, 352 passed, 0 failed, 0 skipped**.

Full console-log SHA256: `f705b3376242e439f09ec56eb45e5891e56d8f08c443e5462c63917318dbebf0`.

| Required suite | Passed |
| --- | ---: |
| `tests/storage/test_complete_pending_backup.py` | 2 |
| `tests/storage/test_pending_backup.py` | 28 |
| `tests/storage/test_library_backup.py` | 9 |
| `tests/storage/test_backup_v5_pending.py` | 28 |
| `tests/storage/test_backup_schema_identity.py` | 27 |
| `tests/storage/test_backup_v4_sidecars.py` | 34 |
| `tests/storage/test_backup_keyboard_drafts.py` | 14 |
| `tests/storage/test_library_backup_reading_state.py` | 69 |
| `tests/storage/test_backup_publication_recovery.py` | 4 |
| `tests/catalog/test_metadata_backup_roundtrip.py` | 1 |
| `tests/storage/test_academic_migration.py` | 18 |
| `tests/storage/test_backup_voice_reader.py` | 23 |
| `tests/library_assistant/test_library_workflow_ui.py` | 9 |
| `tests/library_assistant/test_library_workflow.py` | 55 |
| `tests/e2e/test_extended_document_workflows.py` | 31 |

### Exact completion/source binding

Source commit: `4c52edf2add48b49e555e467b58ae88a98dbc792`. **6 suites, 146 passed, 0 failed, 0 skipped**.

Full console-log SHA256: `32ef82cfc7e95cbffcd3d8b7c4f08ca67f62e9a326fb56f42fcda75f9e0b1e1f`.

| Required suite | Passed |
| --- | ---: |
| `tests/storage/test_complete_pending_backup.py` | 2 |
| `tests/storage/test_pending_backup.py` | 28 |
| `tests/storage/test_library_backup.py` | 9 |
| `tests/storage/test_backup_v5_pending.py` | 29 |
| `tests/library_assistant/test_library_workflow.py` | 55 |
| `tests/storage/test_backup_voice_reader.py` | 23 |

### Final publication/pause boundary

Source commit: `9d0022134cb185a7fe9e8f8ef0c0662139eaf46f`. **4 suites, 123 passed, 0 failed, 0 skipped**.

Full console-log SHA256: `f7fcaad342a9a61fec14ec089c60a260e3961af227bbed3e721906d8e60f2fc8`.

| Required suite | Passed |
| --- | ---: |
| `tests/storage/test_backup_publication_recovery.py` | 4 |
| `tests/storage/test_backup_v4_sidecars.py` | 34 |
| `tests/library_assistant/test_library_workflow.py` | 55 |
| `tests/storage/test_backup_v5_pending.py` | 30 |

### Final PDF checkpoint configuration binding

Source commit: `a31907bf22b5575dcff56deee5b2d20c388a33b3`. **3 suites, 61 passed, 0 failed, 0 skipped**.

Full console-log SHA256: `419307867c245eb9d899d0958f04b0e651abe50de2495225046d8e398fa1fd21`.

| Required suite | Passed |
| --- | ---: |
| `tests/storage/test_complete_pending_backup.py` | 2 |
| `tests/storage/test_pending_backup.py` | 28 |
| `tests/storage/test_backup_v5_pending.py` | 31 |

There are **355 distinct passing cases across 15 suites** when the latest
result for each suite is used. These are recorded staged runs, not a claim that
all 355 cases were rerun together after the final three narrow fixes. The affected
completion/source, publication/recovery and PDF-configuration suites were rerun after their changes.

## Executable regression evidence

- Every supported MIME format (14 real synthetic document formats) is prepared,
  backed up, deleted from its external location, verified, restored and imported
  by the existing worker. A second backup verifies its completed state.
- Actual cancelled TXT import resumes the same job and edition from checkpoints.
  The unchanged canonical suite also exercises genuine local OCR review
  attestation and resume; that case passed and was not skipped in these runs.
- Both exact format-4 layouts and prior format-1/2/3 layouts remain readable.
  Legacy state without an installed compatible producer fails explicitly.
- Shared verification and restore reject pending byte tampering, foreign scopes,
  source retargeting, unlisted files and additional hard links before publication.
- A forged completion referring to a different selected original reproduced as
  **DID NOT RAISE** before the semantic fix. Both shared verification and restore
  now require the sealed revision's asset to match the request/batch source.
- Independent review also identified a real interrupted PDF request whose DPI
  differed from its existing checkpointed job. That case reproduced as
  **DID NOT RAISE** before the fix. Snapshot, shared verification and restore
  now reject that mismatch; TXT intentionally has no PDF configuration binding.
- Full-backup byte limits include the pending component and final manifest.
  An insufficient remaining budget rejects a large selected source before its
  bytes are opened. Real SQLite progress-handler cancellation is normalized to
  `InterruptedError` during pending journal validation.
- A child process exits with code 74 immediately after manifest publication.
  Every published pending payload has one link and remains independently
  verifiable and restorable without staging cleanup.
- Independent review reproduced an assistant pause arriving during final file
  publication: the earlier candidate stranded a snapshot without a manifest.
  A regression using the real `LocalLibraryAssistant` reproduced that failure.
  Final code preserves the pre-publication cancellation boundary, completes its
  bounded namespace commit, and resumes by verifying the same snapshot without
  creating it again. Staging and verification remain cancellable.

## Limits and remaining platform gate

Windows execution, installed Windows bundle acceptance and manual NVDA acceptance
were **NOT_RUN** in this Linux worktree. The final Windows pipeline must include
`tests/storage/test_pending_backup.py`,
`tests/storage/test_complete_pending_backup.py` and
`tests/storage/test_backup_v5_pending.py` in both source and installed gates.
Optional legacy-producer probes remain explicit probes requiring those actual
producers; their absence is not represented as a passing optional integration.

The format does not include arbitrary assistant/studio jobs, model installations,
credentials or generated audio. Operating-system failure or process termination
before the completion marker still leaves an explicit incomplete destination;
cooperative pause after final publication begins completes the bounded commit.
See [the format contract](BACKUP_FORMAT_V4.md) for exact compatibility and limits.
