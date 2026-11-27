# Supplied transcription bridge on the selected audio host

The service, native workspace, CLI and synthetic subprocess tests reuse
[PR140 source 9c1615feceda570a0d52af4c8cf2abbcf1baa2a1](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/9c1615feceda570a0d52af4c8cf2abbcf1baa2a1).
The reviewed original supplied worker is 37,974 bytes with SHA256
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
Its private repair, runtime and models remain separately supplied components.

## Host integration

- `transcription_host.py` binds the service to the existing `audio.engines._run`
  lifecycle and `audio.jobs.RenderCancelled`; it adds the reviewed staging
  byte/file/free-space budget. No additional recognizer, process runner, voice
  registry, audio project model, provider vault or queue is installed.
- The selected f806 host uses its existing `run_job`, cancellation event and
  named status controls. If a richer host supplies `progress_panel`, the same
  adapter preserves its monotonic count. No new progress framework is installed.
  A stopped operation reports cancellation through the existing background job;
  a partial receipt cannot be reported as successful completion.
- `LibraryApp.transcription`, the Library menu and the `transcribe` CLI dispatch
  are additive entry points to the same service.
- `transcription_backup.py` extends the existing typed sidecar inventory.
  It streams media/output copies, binds jobs to the saved workspace and original
  source bytes, verifies completed receipts and edit provenance, and preserves
  original receipt bytes during passive restoration. It uses the unchanged
  common 512 MiB sidecar allowance; larger collections fail before publication.
  A backup requires stopped jobs. Logs, recognizer caches, temporary directories
  and runtime homes are explicitly rebuildable and excluded.
- CLI regression children derive import roots from the actual parent-loaded
  application and bootstrap packages, disable bytecode and verify both origins.
  This permits the same assertions against source and a separately installed wheel.

The base is authentic `f806a0e9795f7c6983b895a3c421e58e33a75866`.
The neutral durable-state helper is reused byte-for-byte from public blob
`b2a05f907dd4fe95c61013964c1b36e37e28597e`. Existing audio engines, job lifecycle,
backup format and database schema are unchanged. AF5's existing host/typed-backup
implementation is reused through narrow anchors, without importing its different
assistant or pending-import producer. No existing sidecar adapter is removed.

The newer upstream observer-isolation and monotonic reported-count corrections
are retained. The completed-job recheck also receives its cancellation callback.
Passive backup validation translates the reused audio cancellation exception into
the existing backup `InterruptedError` contract without publishing partial output.

## Evidence and final integration gate

Exact upstream5498 Windows run
[45922833037](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45922833037),
job140257338236, independently confirms34 bridge and33 progress cases and all six
native synthetic-child checks. Those results belong to that canonical host;
they do not qualify this f806 host composition or the private recognizer.

The current source bridge and host contracts passed48 cases on Linux, with no
failures or skips. These are real filesystem/database/subprocess tests using
synthetic protocol media. Further installed-wheel, existing-backup and composed
Windows/native evidence must identify its exact tested source separately.

Required source and installed suites:

```
tests/transcription/test_supplied_worker_bridge.py
tests/transcription/test_pr99_host.py
```

Preserve all existing typed-backup, reader, assistant, keyboard and native
gates. Verify installed origins for `transcription`, `transcription_host`,
`transcription_backup`, `transcription_cli`, `ui.transcription_dialog` and
`durable_state`. Run the unchanged upstream native probe against the actual
installed bundle under its existing firewall:

```
python -I -B -X utf8 scripts/windows_transcription_smoke.py
```

Its JSON must report PASS for all six checks. The report deliberately keeps
`whisper_acoustic_inference` and `manual_NVDA` as NOT_RUN. The probe uses named
wx controls, a real isolated synthetic child, six checked export formats,
separate edits and unchanged original media; it performs no recognition.

## Private engine boundary

Use the matching repaired worker that supplies the PR140 current-run receipt
contract. An older output-adjacent `.receipt.json` repair is not interchangeable.
Selection records interpreter/worker hashes and `inference_qualified=false`.
No engine code, models, recordings, keys or private source paths are committed.
No automatic model download or remote fallback occurs. A reviewed offline
runtime/model pack, actual private-worker execution, acoustic quality, language
coverage, Windows11/NVDA, diarization and voice cloning remain distinct gates.
Transcripts and edits are derived assets and are never automatically promoted
to original research evidence or a verified quotation.
