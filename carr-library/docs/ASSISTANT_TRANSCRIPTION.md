# Current assistant transcription step

The canonical assistant's `audio.transcribe` behavior is implemented through
the existing supplied-worker supervisor, retained transcription jobs, shared
resource broker and new-only transcript exporter. There is no additional ASR
engine, private worker source, model downloader or credential authority.

## Explicit reviewed plan

Use a typed assistant JSON plan, with the normal selected input/output folders:

```json
{
  "schema": "carr-assistant-plan/1",
  "steps": [{
    "tool": "audio.transcribe",
    "arguments": {
      "path": "C:/Selected/recording.wav",
      "name": "Transcript",
      "runtime_manifest": "C:/Reviewed/runtime/manifest.json",
      "runtime_manifest_sha256": "REPLACE_WITH_THE_EXACT_REVIEWED_64_CHARACTER_SHA256",
      "settings": {
        "output_mode": "separate",
        "task": "transcribe",
        "translation_target": "en",
        "language": "uk",
        "model": "small",
        "device": "cpu",
        "compute_type": "int8",
        "beam_size": 1,
        "parallel_files": 1,
        "vad_filter": true,
        "include_headers": false,
        "output_txt": true,
        "output_srt": true,
        "output_vtt": false,
        "output_json": true,
        "multilingual": false,
        "condition_on_previous_text": true
      }
    }
  }],
  "summary": "One explicitly selected local input and exact reviewed runtime."
}
```

The displayed placeholder is intentionally invalid. Copy an actual admitted
manifest hash and the explicit settings supported by that installed local
runtime. Every one of the current seventeen settings is required, so a saved
template never silently acquires new defaults. The supplied-runtime manifest
still requires the existing local inventory, accepted rights/trusted weights
and language/device/task declarations. This step does not grant those rights.

One file is admitted per step. Multiple files can be separate reviewed steps;
the existing transcription dialog/CLI continues to own larger folder jobs.
The file and output names stay inside the assistant's selected scope. The file
budget includes the export receipt; output bytes are checked before export.

The normal assistant Preview/Submit/Run and named inert template flows work
without another command dispatcher. A loaded template still requires current
scope and a separate explicit approval. Saved executable paths remain inert
after library restoration, and the original assistant job cannot resume in a
different library root.

## Commit and recovery boundaries

Before preparation, the assistant persists a deterministic job ID, selected
source hash/size, exact manifest/model hashes, captured settings and destination.
The current supervisor accepts that optional ID only as a new, exclusive job
name. An already prepared job is reused only after its owned definition,
source provenance and retained input hashes match the saved intent exactly.
Different existing jobs are visible conflicts; no directory is overwritten.

The exact prepared definition digest is rechecked under the supervisor's native
guards before its normal worker launch. Cancellation uses the same owned child,
worker lock, resource lease and segment/cache protocol as the existing ASR UI.
The existing native/CLI stop signal also reaches Preview's runtime inventory.
Shared reader/copy cancellation enters the assistant's durable Paused state
through the same checked InterruptedError contract.

A successful execution is reverified through the current completion receipt.
Claimed complete state with a missing or changed receipt fails instead of
silently starting a new inference. The expected transcript export receipt is
saved before publication. A crash after the final directory move can therefore
recover the exact existing export, verify every declared byte and directory,
and retry directory durability without overwriting it or running ASR again.

Completed assistant steps recheck both the retained ASR completion and the
independent exported files. They need neither the external runtime nor the
original external recording. Complete backup validates the owned transcription
definition/receipt against the assistant intent while leaving external paths
unopened; the current transcription producer retains the actual inputs/cache.

## Evidence boundary

The new source suite is
`tests/assistant/test_transcription_tool.py`. It uses the already established
harmless isolated CLI protocol peer, real retained inputs, actual publication,
current receipts and complete backup. It exercises prepare/intent crashes,
cancellation, exact-definition refusal, export acknowledgement loss, runtime-free
completion checks, output tampering, budgets, inert templates and restoration.
The CLI acknowledgement uses ASCII-escaped JSON that decodes to exact Unicode
paths even on cp1252 consoles. Bounded progress diagnostics are observational;
a disconnected diagnostic stream cannot turn a committed operation into failure.
The source suite exercises the actual CLI commit/reopen with strict cp1252
streams and injects write/flush failures after real completion.

These newly authored cases are **NOT_RUN** during the current executor outage.
They do not qualify the private supplied worker, model accuracy, a language,
CUDA, physical hardware or native Windows behavior. Existing private-worker and
native ASR evidence remain separate and retain their original source identities.
