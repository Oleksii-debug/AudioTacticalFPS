# Supplied Whisper Transcribe — integration review

Review date: 2026-11-27. Updated status: SOURCE_REVIEWED, ADAPTER_PRESENT,
INTEGRATED, EXECUTED_WITH_SYNTHETIC_RECOGNITION. Actual model inference, runtime
distribution clearance and human NVDA/model-quality acceptance remain NOT_RUN.
Windows acceptance is recorded by the exact-head checks in
[PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143), separately from
the source review below. This is a work package under the
[current platform specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md).

## Historical adapter evidence — 2026-11-27

The original review below remains a snapshot of the supplied source. The exact
37,974-byte worker has since been bound to SHA256
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
The [primary opt-in adapter](SUPPLIED_TRANSCRIPTION_ADAPTER.md) reuses the existing
assistant JobStore, scoped plans and bounded audio process runner. It addresses
ASR-01/02 by using immutable one-file staging and content/settings/runtime-bound
receipts, ASR-03 through the exact reviewed no-fallback source correction, ASR-04
through mandatory export verification, and ASR-05/06 through isolated single-file
workers, serialized admission within one application store and owned-process
cancellation. Interrupted files restart; generic hard-kill recovery and acoustic
mid-file checkpoints are not qualified.

The actual supplied worker protocol has been executed on Linux with a synthetic
isolated SDK for successful Ukrainian exports and two truthful-failure cases.
That run advances integration/protocol evidence only. The later composition
routes the historical adapter through the same current TranscriptionService,
native configuration, typed job store and common-format backup. Those composed
paths require their exact-commit tests. ASR-07 real runtime/model and cold offline
Windows qualification, ASR-08 additional capabilities and real recognition
quality remain open. No private
worker, source recordings, transcripts, models or dependencies were published.

## Source and boundary

The owner-supplied implementation identifies itself as V4.3. The current source includes worker.py (37,974 bytes), accessible_window.ps1 (49,718 bytes), README_UA.txt and download_models_once.py. The readable worker source was inspected. Its original worker SHA256 is `1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`; the strict-3 prepared worker SHA256 is `597ad8182adc99a826798aa530fa792784679a5c2755c62a27c9bf0cf55dc2a6`. The preparation script accepts only that reviewed original and verifies its result. No redistributed private source snapshot or approved dependency/model pack was created.

The source location is retained in the authorized private worker input, not in public Git. Its personal jobs, recordings/transcripts, virtual environment, historical backups/patch clutter and machine-specific paths must not be imported. No root LICENSE, dependency lock or pyproject.toml was observed; establish provenance/rights for the supplied wrapper and every runtime/model before redistribution. Do not invent a license for the owner's code.

The existing backend already supports a headless process:

    python worker.py --job-dir <validated job-directory>

It reads job_config.json and settings_live.json, responds to pause.flag/stop.flag and writes job_status.json plus selected transcript outputs. The platform should adapt this boundary to its shared job/capability/backup services and native wx UI, not embed another independent PowerShell application.

Reusable functionality includes a persistent faster_whisper.WhisperModel, CPU int8/CUDA choices, local model loading with local_files_only=True, file/folder batches, language selection/detection, VAD and TXT/SRT/VTT/JSON export. Verify each after integration; source presence is not output-quality evidence.

## Runtime inventory, not an approved lock

The supplied environment metadata records Python3.14.5, faster-whisper1.2.1, CTranslate2 4.8.0, PyAV17.1.0, ONNX Runtime1.26.0, NumPy2.4.6, huggingface-hub1.19.0 and tokenizers0.23.1. These observations do not prove compatibility or portability. The current platform uses a different CPython3.11 runtime; retain an isolated reviewed engine pack or rebuild and test a deliberately selected compatible set.

No models directory was observed at the supplied project root. The downloader names tiny/base/small/medium/large-v3-turbo/large-v3 without immutable revisions and hashes. Actual model availability, complete dependencies and cold offline operation remain unverified. Do not copy the virtual environment as a portable runtime or automatically download all models.

## Original findings and current disposition

Line references identify the inspected worker.py snapshot and may move after changes.

| ID / priority | Observed behavior | Required correction and test |
|---|---|---|
| ASR-01 / critical | Lines630–641 key cache by path, size and integer mtime; model/settings/content identity absent | Bind result to audio bytes SHA256, exact model/runtime and normalized recognition settings. Same-size/mtime changed audio, changed language/model/VAD and renamed input must not reuse a wrong transcript. |
| ASR-02 / critical | Lines644–741 adopt existing TXT using normalized basenames | Reuse only through a validated source/result manifest. Identical basenames in different folders and unrelated edited TXT cannot be marked transcribed. |
| ASR-03 / critical | Lines749–757 retry failed auto-detection with forced Russian and then English | No silent language substitution. Preserve detection uncertainty/error or an explicitly selected fallback; record the actual language and attempt. |
| ASR-04 / critical | Lines832–849 catch requested-output failures as warnings, then increment done and report DONE | Required exports must be durably written and verified before completion. Disk-full, denied output and failed SRT/JSON must yield a truthful recoverable failure/partial state without destroying successful artifacts. |
| ASR-05 / high | Shared model lifetime can be reset during concurrent in-flight transcription, lines319–365/781–788 | Coordinate model admission/session replacement and live settings; use supported bounded inference concurrency. Test retry/model change while another file is active. |
| ASR-06 / high | Pause/stop mainly checked around files, lines402–420/795–805 | Define bounded cancellation and resumable checkpoints during long input/segment processing; no false instant-stop claim. Verify output cleanup and restart without duplicates. |
| ASR-07 / high | Unpinned model names and environment-only dependency inventory | Create exact reviewed engine/model manifest and offline installation test, preserving rights/notices and explicit setup. |
| ASR-08 / acceptance | No speaker-diarization implementation observed; word_timestamps not enabled in transcription call | Do not advertise diarization or word alignment as implemented. Qualify them as separate requested extensions using existing compatible components. |

Whisper task=translate in this implementation targets English. It is separate from the platform's general Google/local any-supported-pair translation workstation.

The adapter now addresses ASR-01/02 with retained content identities and verified
per-file receipts; it never adopts the supplied worker's basename/mtime cache.
ASR-03 is corrected by the reused strict preparation, with no forced Russian or
English fallback. ASR-04 is handled by validating every requested output and
publishing its complete directory plus receipt atomically. ASR-05 is bounded by
one file per worker and a state-root runtime lock; a persistent shared engine
session and global resource broker are still open. ASR-06 has bounded process
termination and file-level restart; segment-level resume is not implemented.
ASR-07 has strict local inventories and explicit setup, but a real, rights-cleared
engine/model pack still needs qualification. ASR-08 remains unsupported.

Earlier Linux source execution passed 42 standalone adapter cases, independently
reviewed. The new UI/cancellation and final composed tree require fresh CI. The suite covers actual child processes, changed same-size inputs, poisoned
bytecode, altered outputs, atomic multi-format publication, crash/cancel/resume,
observer failures, receipt checks and state backups. A separate real subprocess
executed the actual prepared private worker with an authored synthetic
WhisperModel and verified all four formats, Unicode paths and minute-boundary
timestamps. Neither test establishes real recognition quality. The strict-3
preparation also uses explicit LF output to make the exact text contract
independent of Windows newline translation. See
[the adapter contract and limits](TRANSCRIPTION_SUPPLIED_WORKER.md).

## Incremental adoption

1. Retain the current worker and protocol as the source of reuse; capture immutable identity and authorized provenance, remove private/machine-specific content and historical patch clutter.
2. Correct the listed correctness/resource issues in that implementation with focused regressions. Consolidate active V4.3 override/patch paths where necessary rather than maintain two competing workers.
3. Adapt request/progress/result schemas to the platform's existing operation ID, source/model/settings identity, artifact publication, cancellation and recovery records.
4. Register the actual runtime/model capability and preserve its dependency environment separately from the reader/UI.
5. Connect accessible native selection, language/options, status, transcript editing and export through existing commands. Keep original audio, transcript revisions and translated derivatives separate.
6. Independently verify real authorized audio, known speech/non-speech and multilingual samples, timestamps/exports, same-name/corrupt/cache cases, long-file cancellation, CPU/GPU behavior, no-network startup and native Windows/NVDA. Measure quality and resource use on declared hardware; do not claim all135languages.

Advisory leads: CARR03 implementation/adaptation,10 jobs/resources,05 recovery,09 accessible UI,11 runtime/package,12/13/14/15 independent acceptance. Source availability no longer blocks review; runtime/rights/quality prerequisites remain explicit. Work on other platform modules continues.
