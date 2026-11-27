# Supplied transcription bridge on the selected audio host

This integration reuses [PR140 source 9c1615feceda570a0d52af4c8cf2abbcf1baa2a1](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/9c1615feceda570a0d52af4c8cf2abbcf1baa2a1).
It retains the supplied worker's job-directory protocol. No recognizer,
alternative audio project, provider vault, voice registry or second queue is
introduced. The original supplied worker has been received and hash-matched:
37,974 bytes, SHA256
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
Private worker repairs, models and runtime files remain separately supplied.

## Host bindings and recovery

- `transcription_host.py` delegates subprocess execution and cleanup to the
  existing `audio.engines._run`, with staging byte/file/free-space admission.
  Cancellation uses the existing audio cancellation exception.
- The native workspace uses the current `progress_panel` and background job.
  Stopped jobs raise the host cancellation outcome. Incomplete jobs preserve
  their service state and resume inputs while the background operation reports
  failure to complete; only verified completion can succeed.
- The upstream optional-observer wrapper and monotonic worker-reported counts
  are retained. Reporting cannot relabel or retry a durably saved result.
  Worker-reported counts remain distinct from accepted receipt counts.
- Additive LibraryApp, menu and CLI entry points call the same service.
- The typed transcription producer streams media and outputs through the
  existing sidecar manifest. It checks saved workspace/source identities,
  completed/partial receipts and edit provenance. Restoring configuration is
  passive and never starts a worker or downloads assets.
- The existing 512 MiB aggregate sidecar allowance is unchanged. Larger ASR
  collections fail before backup publication. Logical edit text remains capped
  at16 MiB; its serialized JSON uses the producer's32 MiB limit so escaping does
  not make an otherwise valid edit impossible to back up.
- Logs, recognizer caches, temporary directories and runtime homes are explicitly
  rebuildable and excluded. Required original media, receipts, job records,
  outputs and user edits are retained. Incomplete/unreferenced output bytes are
  preserved as inert data, not represented as verified recognition.
- CLI regression children use parent-loaded package roots, disabled bytecode,
  and explicit application/bootstrap origin assertions in source and installed
  test modes.

The canonical pending-import producer and durable-state blobs remain unchanged:
`58c37668a1964c07c18a04dd4625bab9dd4d2e08` and
`b2a05f907dd4fe95c61013964c1b36e37e28597e`. The existing backup format,
database schema and assistant restore-review handling are retained.

## Recorded evidence and exact-source gate

Before executor loss, the initial host port of88502883 plus the pending-import
fix passed165 Linux cases with warnings treated as errors and zero skips:
29 public bridge cases,10 host cases,38 pending/component cases and88 existing
typed-backup/reading cases. A separately built wheel was installed and its
39-case run started, but completion was not observed; no installed PASS is
claimed.

Upstream88502883 [Windows run45921379133](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45921379133),
job140252664642, passed29 bridge cases plus33 progress cases and all six native
checks. Those results belong to that upstream host. They do not qualify this
composition or private acoustic inference.

After executor loss, source was recovered from the published files and recorded
patches. The published9c1615 observer/count correction and independent review's
escaped-edit/hash-type corrections were then reconciled through GitHub. This
exact new union has not executed locally. Preserve every existing gate and
require both suites in source and installed environments:

```
tests/transcription/test_supplied_worker_bridge.py
tests/transcription/test_pr99_host.py
```

The bridge suite retains all upstream test functions, including five observer
cases and two actual-child stop/partial/resume cases. Those two cases use the
selected host's actual ProgressHub/ProgressTask; no canonical tracker module is
added. The host suite adds real subprocess staging limits, typed backup/restore,
foreign edit rejection, escaped-edit roundtrip and malformed engine checksums.
Expected collection is36 bridge and14 host cases; actual JUnit must confirm it.

Verify installed origins for `transcription`, `transcription_host`,
`transcription_backup`, `transcription_cli`, `ui.transcription_dialog`, and
`durable_state`. Run the unchanged upstream native probe with the actual
installed bundle under its existing firewall:

```
python -I -B -X utf8 scripts/windows_transcription_smoke.py
```

Require PASS for all six JSON check names: actual named wx controls; shared
background operation; actual isolated child protocol; six verified formats;
separate edit revision; original unchanged. The probe must continue to record
`whisper_acoustic_inference=NOT_RUN` and `manual_NVDA=NOT_RUN`.

## Private runtime boundary

Choose the matching repaired worker that exposes PR140's current-run
run_id/source/model/runtime/settings/artifact receipt contract. An older
output-adjacent receipt repair is not interchangeable. Configuration records
interpreter/worker hashes and `inference_qualified=false`. It is not acoustic
qualification.

No private worker code, model, recording, credential or personal source path is
part of this commit. A reviewed offline runtime/model pack, actual private
inference, multilingual quality, Windows11/NVDA, diarization and voice cloning
remain separate acceptance. Transcripts and edits are derived assets; they are
never automatically promoted to original research evidence or verified quotes.
