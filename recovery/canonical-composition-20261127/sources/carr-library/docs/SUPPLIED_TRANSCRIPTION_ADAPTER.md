# Supplied transcription: Assistant compatibility facade

The Assistant's `audio.transcribe` tool uses the same
`audio.transcription.TranscriptionService` as the native transcription panel and
CLI. It preserves the Assistant's selected input/output permissions and approved
plan, while recognition, retained inputs, runtime admission, cancellation,
cleanup and generated transcript receipts stay in the existing typed ASR store.
There is one supplied-worker preparation and execution path.

No private worker source, models, audio, credentials or runtime package is
redistributed. Merely adding a transcription step to an Assistant plan does not
enable a runtime.

## Reviewed installation and preparation

The preferred entry point is an explicitly selected, retained
`SuppliedWhisperRuntime` manifest. Its complete runtime/model inventories,
interpreter SHA256 and explicit `runtime_reviewed` / `rights_reviewed` flags are
checked by the existing service. The local draft-manifest helper leaves both
flags false; inventory discovery never manufactures approval.

The compatibility constructor `SuppliedWorkerInstallation` continues to accept
the six original explicit selections: interpreter, worker, isolated package
directory, model directory, legacy manifest path and separately retained
manifest SHA256. It additionally requires both explicit review flags to be true.
Provenance notices and matching files are insufficient to infer either review.
Its legacy manifest retains the original fields:

```json
{
  "schema": 1,
  "engine": "supplied-whisper-v43",
  "python_sha256": "<reviewed interpreter SHA256>",
  "python_version": "<major.minor.patch>",
  "runtime_files": {
    "<relative installed runtime file>": {"size": 123, "sha256": "<SHA256>"}
  },
  "model_files": {
    "<relative installed model file>": {"size": 123, "sha256": "<SHA256>"}
  },
  "runtime_notice": "<reviewed source, dependency inventory and rights evidence>",
  "model_notice": "<reviewed model revision, source and rights evidence>"
}
```

This abbreviated example is not a usable runtime. Every actual file in both
trees must be inventoried, including the required SDK/native dependencies and
the complete model. Links, unexpected entries, byte/size changes and missing
required packages are rejected. Compatibility enrollment converts these public
declarations in memory and passes them through the ordinary runtime validator;
it does not create another executable configuration or job store. The pinned
legacy manifest and exact Python version become part of the runtime identity.

Worker preparation delegates to `audio.supplied_whisper.prepare`, including its
separately pinned supplied V4.3 and hardened protocol revisions. The
`TRANSCRIPTION_SUPPLIED_WORKER.md` review and preparation module define the exact
source/prepared hashes. The strict settings, timestamp and newline corrections
are shared. An old Assistant intent bound to a different preparation or runtime
is preserved and requires a new reviewed plan; it is not silently reinterpreted.

The actual worker retains its `--job-dir` protocol. The existing service creates
an immutable input copy, fresh private staging/cache and fixed settings for each
file. Runtime Python starts isolated with explicit UTF-8 and a private bytecode
cache prefix. Its selected runtime path is explicit; global site-packages and
ambient imports are not an alternative dependency source. Offline flags and
Python audit guards reject network/child-command use outside the reviewed
protocol and Python file writes outside the private job stage. These are Python
policy checks, not native network or operating-system isolation.

## One job, controls and verified results

The allowlisted Assistant step has required `path` and `name`, with optional
`language`, `vad_filter`, `beam_size` and `condition_on_previous_text`.
Its input must remain in the approved input scope; its new export directory
must remain in the approved output scope. No command, downloader name, runtime
path or remote endpoint can be supplied by a plan.

Before recognition, the Assistant persists the source digest, selected settings,
runtime/preparation identity and destination. It then records the existing
64-hex transcription job ID. The job lives in
`state/transcription/<id>`; the GUI, CLI and Assistant read and control that same
job. Identical input bytes, settings and runtime can reuse its verified complete
result for a second independently approved export.

Assistant pause/cancel is forwarded to that job's existing control file from
the operation worker. Shared resource admission and native process cleanup
remain owned by TranscriptionService. A stopped file resumes through the
existing per-file checkpoint; this is not a mid-file acoustic checkpoint.

The common service requires TXT, SRT, VTT and JSON to agree with the requested
source, settings, language and finite ordered segment times. A zero exit code or
a success string alone is insufficient. Its complete per-file directory and
receipt are published together. The Assistant verifies that receipt and checks
every payload against its checksum again while copying the selected external
export. A changed generated transcript is preserved and rejected.

The Assistant exports `transcript.txt`, `transcript.srt`,
`transcript.vtt` and `transcript.json` through the existing create-only
publication helper. Its saved intent contains their exact paths, sizes and
digests before publication. Interrupted external publication can finish only
with the same saved payloads; existing edited files are never replaced.
Completed generated results are reused without rerunning inference. A late
private-stage cleanup error after verified export is reported as a cleanup
notice while retaining the completed receipt.

Export JSON records original source and installation identities plus the shared
job ID, replacing disposable worker paths. The transcript is an editable
derivative requiring human recognition review; it does not become a quotation
source. Word alignment and diarization are not asserted.

## Backup and restored scope

The existing typed backup producer retains the Assistant plan/checkpoint and
the shared ASR job's retained input and verified generated transcripts. Export
copies outside the library, the private worker, interpreter/runtime packages and
model remain external dependencies. The result states this split explicitly.

Restore preserves the recorded Assistant input/output permissions and runtime
identity. It does not authorize a moved library, different installation or new
remote request. Select and review the external runtime and restored job before
resuming. Complete exported files remain subject to the saved no-overwrite
receipts.

## Qualification

Public regression cases use authored synthetic workers and SDKs. They exercise
the real owned subprocess, UTF-8 protocol, strict language/settings/output
validation, cancellation, pause/resume on the same typed control file, runtime
identity, original/edited output preservation, backup declarations and reuse of
a completed typed ASR result without another inference call.

The protocol helper
`scripts/verify_supplied_transcription_protocol.py --worker /absolute/private/worker.py`
accepts an explicitly selected owner-local source and uses a synthetic SDK. It
does not download or load a real recognition model. Its successful execution
would qualify that protocol test only.

This canonical compatibility reconciliation is a **source proposal pending
execution** until its exact composed test/CI evidence is recorded. Earlier
component results do not establish a pass for this new union. Real model
inference, offline Windows runtime qualification, microphone/recognition
accuracy, long-file CPU/RAM measurements and human NVDA use remain separate
acceptance work. No such result is inferred from the synthetic tests.
