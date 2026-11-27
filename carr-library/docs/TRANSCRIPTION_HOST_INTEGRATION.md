# Supplied transcription bridge on the selected host

This feature reuses the supplied job-directory service from PR140
`9c1615feceda570a0d52af4c8cf2abbcf1baa2a1` and its selected-host adaptation
`7efde01c42adfecd441c99aa80dc61edb724525c`. The integration base is
`228e85c85687a85c1dd8d227c6e88676f3795cf6`, tree
`3669f9f26eb125f2534654f9a6d316ca06d9cc3c`.
The earlier adaptation's backup/progress APIs differ from this host; only its
ASR service, protocol, UI, CLI and synthetic fixtures are reused.

There is no new recognizer, audio model registry, provider database, progress
panel or queue. The original supplied worker is received: 37,974 bytes,
SHA256 `1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
The matching repaired private worker, runtime and model assets remain separately
supplied and are not part of this source.

## Current host bindings

- `transcription_host.run_process` uses the current `audio.engines._run`.
  Existing owned-process cleanup, deadlines and bounded logs remain in use;
  the adapter adds watched staging bytes/files and a free-space reserve.
- The native dialog uses the existing `frame.run_job` worker. It reuses one
  current `ProgressHub` on the frame and binds its `ProgressReporter` around
  the operation. The previous context is restored on every exit.
  Native status text displays measured worker counts. Verified receipt counts
  remain separate. Only a verified completed result completes the task.
- The service's existing observer exception isolation and monotonic counts are
  retained. Terminal observer errors cannot retry or reverse a saved result.
  Stopped operations map to cancellation; incomplete/error results map to failure.
- The small `durable_state` helper is reused from the original host adaptation.
  New-state publication and job/export directory publication use the current
  shared exclusive-rename primitive, preserving concurrent destinations and
  avoiding hardlink aliases. Replacement JSON remains protected by its job lock.
- Portable names use the current `backup_files.portable_relative` boundary.
  No alternate `backup_sidecars`, `state_backup` or progress module is imported.
- Submission acquires engine then workspace-submit locks, so typed backup can
  freeze namespace creation before collecting the existing job locks.
  Running, editing and exporting use each job's transcribe lock; none of those
  operations acquires engine while holding transcribe.
- Complete backup uses the current typed-producer integration and existing
  manifest format. Original media, receipts, required results and separate
  edits are retained; engine configuration restores passively.
  Rebuildable runtime homes, caches, logs and temporary files are excluded.
  Existing aggregate limits remain enforced; oversized collections fail before
  backup publication rather than omitting originals.

The only existing application entry-point changes are the `transcribe` CLI
dispatch, `LibraryApp.transcription`, and the menu installer. Existing sync,
translation, audio, pending-import recovery and keyboard actions are retained.
A completed-job replay forwards cancellation into the final receipt recheck.

## Required acceptance

This new composition was prepared from exact published sources while the local
executor was unavailable. It has not yet executed. Earlier component results
are not results for these new bytes.

Run both existing service/host suites in source and installed-wheel modes:

```
tests/transcription/test_supplied_worker_bridge.py
tests/transcription/test_pr99_host.py
```

Expected collection is **38 bridge cases and 26 host cases**, with no skips.
These totals retain the previous 36/14 cases and add cancellation during
completed-result verification, concurrent export/JSON publication, current-host
progress context and failure behavior, engine-lock namespace serialization,
semantic command registration before/after keyboard setup, and failed submission
or late-callback handling. Existing Windows stop/partial assertions retain
bounded synthetic-child diagnostics; unobserved causes are not inferred.
The host's separate typed-backup regression suite must also run:

```
tests/storage/test_transcription_backup.py
```

The synthetic retained audio fixture is a structurally valid WAV containing
silence, so current backup media validation remains enabled. The worker itself
is explicitly synthetic; these tests do not recognize that audio.

Verify installed origins for `transcription`, `transcription_host`,
`transcription_backup`, `transcription_cli`, `ui.transcription_dialog` and
`durable_state`. Run the native probe with the installed bundle's existing
activation and outbound firewall, after its test-only pytest dependency:

```
python -I -B -X utf8 scripts/windows_transcription_smoke.py
```

Require PASS for all six existing check names: actual named wx controls; shared
background operation; actual isolated child protocol; six verified formats;
separate edit revision; original unchanged. The probe continues to report
`whisper_acoustic_inference=NOT_RUN` and `manual_NVDA=NOT_RUN`.
Its valid WAV fixture and exact original-byte assertion preserve the same
six native scenarios.

## Private qualification boundary

The selected private repair must expose PR140's current-run
run_id/source/model/runtime/settings/artifact receipt schema. A repair using
output-adjacent receipts, or a V5 whole-job manifest alone, is not interchangeable.
Configuration records selected interpreter/worker hashes and
`inference_qualified=false`; it is not runtime or acoustic qualification.

A compatible reviewed runtime/model pack, actual private inference, mixed-language
and per-language quality, native Windows execution with that pack, and manual
NVDA acceptance still require their own evidence. The existing public synthetic
child proves process/protocol behavior only. Transcripts and their edits are
derived assets and are never automatically approved as original research
evidence or verified quotations.
