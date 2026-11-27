# Supplied transcription on the current desktop host

The integration reuses PR140's existing transcription service, including the
explicit V5 profile and recursive folder intake at
`ebb5539b763a0733574f7e248be474902ebf723c`. Its earlier service identity is
`9c1615feceda570a0d52af4c8cf2abbcf1baa2a1`; the selected-host adaptation
`7efde01c42adfecd441c99aa80dc61edb724525c` supplied compatible process and
receipt integration patterns. The current host keeps its own audio, progress,
keyboard and typed-backup infrastructure.

The originally supplied worker was 37,974 bytes, SHA256
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
Private repaired workers, interpreters, models and recordings remain separately
supplied. This source adds no recognizer, model training, automatic acquisition
or competing job database.

## Explicit worker profiles

| Profile | Selected contract | Bound identities |
|---|---|---|
| `receipts-v1` | Existing repaired worker's current-run result receipts | Interpreter, worker, original source, model, settings and every accepted artifact |
| `whisper-v5-manifest-v1` | Supplied repaired V5 whole-run manifest | The same identities plus all three wrapper sources and requested/effective retry settings |

The V5 profile requires `worker.py`, `local_models.py` and
`transcript_features.py` together. The service validates the original
manifest and its outputs before normalizing them into its existing receipt,
edit, export and backup lifecycle. Word subtitles come only from validated word
timings. Every attempt uses a fresh result root, so an old manifest cannot
complete a new run. Unsupported profiles and unsupported options fail visibly.

The native configuration action and CLI select a profile explicitly. New jobs
retain that choice; older jobs without a protocol field keep the original
receipt contract. Reconfiguring another profile does not silently change an
existing job. Verified completed results remain readable and exportable when
the old engine is unavailable.

File and recursive folder intake share the existing bounded scanner. The
document-import policy remains its default. Media intake preserves relative
names, copies exact checked originals, reports excluded sidecars and rejects
links, changed inputs and exceeded limits before publishing a job.

## Current host bindings

- `transcription_host.run_process` uses the current `audio.engines._run`.
  Owned-process cleanup, deadlines and bounded logs remain in use. The adapter
  adds watched staging bytes/files and a free-space reserve.
- The desktop's existing `run_job` binds one shared `ProgressReporter` around
  the actual executor operation. The transcription dialog updates that same
  reporter through `current_progress`; it creates no second task. Context is
  restored on every exit.
- The sole native controller, `OperationProgress`, observes the existing
  `ProgressHub` and uses one `NotificationWorker`. `frame.progress_view` is an
  alias of that same controller. The hub survives a library-root change so
  active media suppression remains valid. Measured worker counts remain distinct from verified
  receipt counts. Unmeasured work stays indeterminate. Only an accepted
  completed result reaches 100.
- Observer failure, eviction or a detached UI cannot retry or reverse a saved
  service result. Failure to present progress does not suppress its real result
  callback. Cooperative Stop and verified completion remain distinct.
- Windows worker-status reads reuse the exact shared-handle design from
  `881b6f94d995be81310795c8dc31f6593bc6e115`. Read/write/delete sharing allows
  the worker's concurrent atomic replacement. Bounded JSON, duplicate-key,
  non-finite-number, regular-file and link checks remain enabled.
- New JSON and job/export publication use the current exclusive publication
  primitive, preserving concurrent destinations without hardlink aliases.
  Replacement state remains protected by its existing job lock.
- Portable names use `backup_files.portable_relative`. Submission acquires
  engine then workspace-submit locks; backup can therefore freeze namespace
  creation before collecting existing job locks. Run/edit/export use each
  job's transcribe lock without reversing that order.
- The current typed backup producer retains original media, accepted manifests,
  receipts, required outputs and separately saved edits for both profiles.
  Validation is passive: it does not open or execute external engines/models.
  Source state and copied staging are validated before backup publication.
- A saved edit after partial recognition remains bound to its original receipt
  even when a later resume is stopped. Restoring that history requires the
  retained original, exact text and required artifacts; V5 additionally requires
  its original manifest and effective settings. Stopped counters are not
  promoted into accepted results.
- Runtime homes, logs, temporary files and rebuildable caches are explicitly
  excluded. Existing aggregate backup limits still apply; inputs are never
  silently omitted to fit a backup.

Existing sync, translation, audio, pending-import and keyboard workflows remain
part of the required integration tests. Database schema versions, backup-format
versions and transcription protocol versions are separate contracts.

## Exact acceptance required

This composition was prepared during an executor outage. Source review and
earlier component results are not a runtime verdict for the composed tree.
The final source SHA and installed application-wheel identity must be recorded
by the corresponding successful workflow.

Required source and installed cases include:

| Suite | Required cases |
|---|---:|
| `tests/transcription/test_supplied_worker_bridge.py` | 59 |
| `tests/transcription/test_pr99_host.py` | 27 |
| `tests/transcription/test_v5_worker_profile.py` | 37 |
| `tests/storage/test_transcription_backup.py` | 42 |
| `tests/ui/test_progress_view.py` | 25 |

The workflow checks named cases and rejects failures and skips. Existing
scanner, playback, progress, keyboard, backup, publication and packaging suites
remain required as well. Public test workers are explicitly synthetic; a valid
silent WAV keeps real media validation active without claiming acoustic
recognition.

Verify installed origins for the service, host, typed backup, CLI, durable-state
helper, transcription dialog, progress core, notification worker and native
progress view. Run both native probes after the bundle's native-runtime
activation and test-only dependencies, inside its existing outbound-firewall
boundary:

```
python -I -B -X utf8 scripts/windows_transcription_smoke.py
python -I -B -X utf8 scripts/windows_shared_progress_smoke.py
```

The transcription probe retains the original six checks and adds native profile
selection/configuration, the V5 three-source binding, explicit receipt-profile
reconfiguration and the actual folder action/intake/result counts. Picker
answers are controlled synthetic inputs; those picker interactions are not
claimed as manual keyboard acceptance.

The progress probe requires 44 recorded checks: actual installed module bytes,
native controls, operating-system key dispatch, focus preservation, measured
25/50/75/99 values, UIA range/value at 50 and verified 100, cancellation, a real
committed file surviving a late cancellation request, persisted settings and
notification cleanup. It keeps optional sound Off.

## Private qualification boundary

The two public profiles validate different existing worker contracts. A private
repair must match its explicitly selected profile; it is not accepted merely
because its filename or version looks similar. Configuration records selected
hashes and `inference_qualified=false`.

Actual private inference, exact runtime/model compatibility, mixed-language and
per-language quality, native Windows execution with that private pack, physical
audio listening and manual NVDA usability require separate evidence. Public
synthetic workers establish protocol and recovery behavior. They do not
establish neural recognition, diarization or language accuracy.

Transcripts and edits remain derived assets. They never automatically become
original research evidence or verified quotations.
