# Earlier canonical transcription projects

## What is preserved

The former canonical namespace is `state/transcription`, schema
`resonance-transcription-job/1`. Current executions use
`transcription-jobs`, schema `carr-supplied-transcription-job/1`.
They are different protocols. An old status, trust flag, profile path, cache or
receipt is never a current execution/completion authorization.

The recovery adapter reuses the pure canonical e69ba240 schema/result validators.
It retains original source bytes, runtime declarations, accepted transcripts in
their saved formats, user edits, controls, bounded logs and declared worker
history. It never opens an archived interpreter, model, original external path
or credential. Runtime/model payloads were never part of this namespace.

Complete backup takes the existing application writer, then the actual canonical
profile lock and each sorted job's execution/control locks. An actively owned job
refuses the snapshot. A stale recorded running label after a crash remains inert
historical data. Inventory and full source hashes are checked again before the
backup manifest is published. No status polling substitutes for native ownership.

Live earlier data is copied into immutable, content-addressed
`state/transcription-imports/archives/<manifest-sha256>` archives. Backup and
restore include this registered archive producer. Original files are not changed.
Restoration does not recreate the former active `state/transcription` tree.
The original `trusted` values remain verbatim data inside the archive, with no
reader that interprets them as current authority.

The exact common-v4 historical backup envelope is also supported. Its original
manifest and original payload names remain hash-bound under
`state/historical-backups`. Only the transcription namespace receives its actual
canonical bounds: up to 12 path components, 32 GiB per source, 64 GiB retained
inputs in total, 32 MiB per metadata record and 512 MiB metadata in total.
Legitimate empty transcript/log/control files are admitted only by the typed
path rules. Other historical sidecar budgets are unchanged. All current caller
file/byte budgets still apply to the complete backup.

## User actions

Open **Local speech transcription**, then **Earlier transcription history**.

1. Choose stored history, or explicitly retain an earlier installed namespace.
   Retaining a live namespace uses the current resource broker and preserves an
   independent archive before offering its jobs.
2. Select a historical job. Its label says *recorded* status.
3. Open an accepted original or saved edit through the existing reading-copy
   editor; export an existing TXT, SRT, VTT or JSON artifact; or prepare selected
   retained originals as a new current job.

Reading/exporting historical results needs no old runtime, model or original
external recording. Accepted receipt/source/output hashes are revalidated.
Editing creates a separate current reading copy and preserves the old record.
A transcript remains a derived, unqualified text; it does not become an original
document quotation.

New preparation requires a currently selected reviewed runtime manifest and the
current settings controls. No settings, execution paths or permission flags are
borrowed from an archived profile. Choose an explicit source subset within the
current 32 GiB job bound; a larger old job can be split into several new jobs.
The existing TranscriptionService.prepare retains and validates the selected
inputs. Start remains a separate existing action.

The bridge materializes only that selected subset in an owned temporary folder,
retaining the validated audio suffix. Admission reserves the temporary copy, the
independent prepared copy, the largest parallel worker captures and a safety
reserve. It does not increase the current service's admission limits.

A separate import link records the old archive/job/source identities and the
new immutable definition hash. It is provenance only, never a completion receipt.
If definition reread or link publication fails after prepare committed, the
returned job ID remains a successful preparation with a reopen warning. Native
Start stays disabled until the ordinary saved-job review obtains its definition.
Known interrupted archive/link staging is excluded from durable inventory;
unknown names, links, reparse points and aliased locks fail closed.

## Public interface

`legacy_transcription.jobs(root)` returns bounded, explicitly historical metadata.
`read_transcript`, `reading_copy`, `export_text` and `export_output` validate
retained historical data without activating the old runtime.
`retain_live(root)` makes an explicit inert snapshot.
`import_as_new(service, reference, source_ids, runtime_manifest, settings, ...)`
uses the current service and returns its prepared ID, definition when readable,
and any postcommit warning.

## Required acceptance and current evidence

This candidate was prepared during executor unavailability. Its new tests and
native probe are **NOT_RUN** until exact-source CI completes. Source review is
not inference, acoustic quality, language qualification or manual NVDA evidence.

Required portable and installed suites:

- `tests/storage/test_legacy_transcription_recovery.py`: real live/common-v4
  backup, verification, relocated restore/rebackup, exact inert bytes, old result
  reading/export without externals, current actual preparation, source/receipt
  forgery, native cross-process lock refusal, cancellation, commit acknowledgement,
  same-content directory-sync retry and explicit size bounds.
- `tests/ui/test_legacy_transcription_recovery.py`: current session background
  boundary, explicit current runtime/subset selection, definition-read warning,
  changed-root/close/cancel isolation and existing reading-editor handoff.
- Existing current transcription service, receipt, backup and native-dialog suites.

`scripts/windows_legacy_transcription_smoke.py --native` must run from the installed
product. It drives actual Windows choice/checklist/button events and current
preparation over a public synthetic retained-input/runtime-inventory fixture.
It never starts inference. The workflow also checks installed module origins and
requires the named recovery JUnit cases with zero skips.

## Composed application compatibility

This component ports the passive canonical schemas and recovery flow from owner
`f8d5f85619b61fb52df24c7eb2f5dd4d1febc18f` onto the clean composed application
`99405f10d88768534f7d0ff1df3da86cea3f77ce`; it does not adopt the owner's alternate
transcription supervisor, UI family, trust policy or general media pipeline.

Three saved ASR families remain distinct. Earlier canonical `profiles/` and
`jobs/` under `state/transcription` are captured as inert history. Existing
workspace-scoped `engine.json` and `<workspace-hash>/jobs/` are retained and
validated by their original adapter. Current supplied jobs remain under
`transcription-jobs`. Unknown siblings fail closed. The complete-backup writer
holds both producer lock families and validates the copied catalog/source state
before publishing its completion manifest. Restoration of canonical history
never creates an active earlier job.

The generic older state-folder budget remains 32 GiB with its existing
2048-component / 4096-character portable-path bound. The larger typed allowance
is requested only by the canonical archive reader. Exact historical state v3/v4,
common-root v4 and state-plus-reading v5 envelopes are disjoint from current typed
backup manifests. All old foreign state remains inert; this port does not
activate unrelated old provider or reader configuration.

The current V43 dialog exposes **Earlier history** through its existing guarded
session. A selected saved runtime's reviewed manifest SHA256 is forwarded to
current preparation and checked before publication. Start remains separate.
Current trust admission, progress/cancellation behavior, recording ownership and
worker protocol are preserved.

### Headless commands

Use the existing transcription CLI with the explicit library-wide supplied
profile `supplied-whisper-v43-hardened-1` and `--data-dir`. The additive commands
are `history`, `retain-history`, `history-reading-copy`, `history-export`
and `history-prepare`. Listing never captures automatically. Source selections
require the exact archive kind, archive digest, earlier job ID and source ID(s)
returned by `history`. Preparing requires a current `--manifest`, supports
`--expected-manifest-sha256`, and uses the same seventeen current settings.
Exports require a fresh destination and never silently replace existing files.

### Qualification status

The new compatibility tests exercise all three ASR families together through
actual complete backup, verify, restore and rebackup; they also check default
historical limits, unknown sibling refusal, current manifest identity, CLI
preparation/export/reading copies and guarded native-session callbacks.
At authoring these changes are **NOT_RUN**: no compilation, inference,
speech quality, provider/model qualification or NVDA claim is inferred from the
owner implementation or from static review. Exact source/installed/native
qualification is recorded separately by the committed focused gate.
