# Supplied Whisper Transcribe — integration review

Review and execution date: 2026-11-27. The supplied worker is now adapted through
`TranscriptionService`, the native Tools dialog and the `transcription` CLI.
Actual offline CPU inference, verified cache reuse and destructive backup/restore
have executed with one pinned English test model. Windows/NVDA, multilingual
quality, GPU behavior and redistribution clearance remain separate gates. This
implements the transcription work package in the
[current platform specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md).

## Current integration and execution evidence

The unmodified supplied worker has SHA256
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
Its corrected private derivative is admitted only as SHA256
`5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c`.
Public source contains the adapter and synthetic protocol tests; it contains no
private worker source, engine environment, model weights or recordings.
An earlier `d911…` derivative is not admitted because replacement inference
processes could retain the previous runtime fingerprint.

The final service blob is `cc696c76e805a0589fbdad93f38e0a9d292ac8d2`.
The actual service/CLI acceptance ran at
`e1e8136e4793ae35ef551704832dca7f44b69201`; integration retains the same service
bytes. Its private evidence contains 81 passed assertions. The public
[execution record](evidence/transcription-private-worker-20261127.json) records
the bounded conclusions and hashes without private machine paths.

| Dimension | Current evidence |
| --- | --- |
| SOURCE_REVIEWED | Original source identified; ASR-01 through ASR-06 corrected in the supplied implementation; 36 worker-boundary regressions pass |
| ADAPTER_PRESENT / INTEGRATED | One public service, retained audio, durable jobs, native wx dialog, CLI, shared progress/cancellation and complete library backup |
| EXECUTED | Actual faster-whisper CPU inference with `Systran/faster-whisper-tiny.en`, revision `0d3d19a32d3338f10357c0889762bd8d64bbdeba`; no external route inside the Linux network namespace |
| Recovery | Real interrupted job resumes with a new matching run ID and verified recognition reuse; original input and whole library removed before restore; same saved job resumes after explicit engine rebind |
| Original / derivative boundary | Original audio and generated transcript hashes retained; editing creates a separate revision; export refuses an existing destination |
| QUALIFIED | Only compatibility with this exact runtime/model and one 5.29-second synthetic English fixture; not multilingual, human listening, general accuracy, long-file or GPU qualification |
| Windows / NVDA | Native source and installed-package probes are required by the composed Windows workflow; this Linux evidence makes no native Windows or manual NVDA claim |
| DISTRIBUTION_CLEARED | WAITING_INPUT: private wrapper authorization, exact runtime/model notices and distribution profile remain separate; no inference assets are bundled automatically |

The exercised runtime used faster-whisper 1.2.1, CTranslate2 4.8.0, PyAV 17.1.0,
ONNX Runtime 1.26.0, NumPy 2.4.6, huggingface-hub 1.19.0 and tokenizers 0.23.1.
The installed dependency freeze and `pip check` were recorded privately.
Requested CPU `int8` resolved to actual `int8_float32`; the receipt reports the
actual value. Effective inference concurrency was one. A cold service run took
1.990 seconds and the explicit warm CLI resume 1.716 seconds on the test host;
these are individual observations, not performance guarantees.

The test model emitted a segment end beyond the decoded duration. The corrected
worker retained the raw timestamp, clamped the exported interval to 5.29 seconds
and exposed a warning. Timestamp cleanup is not evidence of acoustic alignment.
`tiny.en` is explicitly English-only in the service and dialog.

After relocation, a stored engine selection is inactive until the user explicitly
selects the reviewed external runtime again. A resumed job regenerates all
execution paths from its current retained inputs. Relocation correctly caused
fresh recognition instead of reusing the old absolute-path cache identity.

## Original static review and resolved findings

The following inventory describes the received V4.3 snapshot. It is retained as
the provenance and defect record; it does not describe the corrected derivative
as still unintegrated.

## Source and boundary

The owner-supplied implementation identifies itself as V4.3. The received source includes worker.py (37,974 bytes), accessible_window.ps1 (49,718 bytes), README_UA.txt and download_models_once.py. The worker's immutable identity is recorded above. No private source snapshot has been redistributed in public Git.

The source location is retained in the authorized private worker input, not in public Git. Its personal jobs, recordings/transcripts, virtual environment, historical backups/patch clutter and machine-specific paths must not be imported. No root LICENSE, dependency lock or pyproject.toml was observed; establish provenance/rights for the supplied wrapper and every runtime/model before redistribution. Do not invent a license for the owner's code.

The existing backend already supports a headless process:

    python worker.py --job-dir <validated job-directory>

It reads job_config.json and settings_live.json, responds to pause.flag/stop.flag and writes job_status.json plus selected transcript outputs. The platform should adapt this boundary to its shared job/capability/backup services and native wx UI, not embed another independent PowerShell application.

Reusable functionality includes a persistent faster_whisper.WhisperModel, CPU int8/CUDA choices, local model loading with local_files_only=True, file/folder batches, language selection/detection, VAD and TXT/SRT/VTT/JSON export. Verify each after integration; source presence is not output-quality evidence.

## Runtime inventory, not an approved lock

The supplied environment metadata records Python3.14.5, faster-whisper1.2.1, CTranslate2 4.8.0, PyAV17.1.0, ONNX Runtime1.26.0, NumPy2.4.6, huggingface-hub1.19.0 and tokenizers0.23.1. These observations do not prove compatibility or portability. The current platform uses a different CPython3.11 runtime; retain an isolated reviewed engine pack or rebuild and test a deliberately selected compatible set.

No models directory was observed at the supplied project root. Its downloader names tiny/base/small/medium/large-v3-turbo/large-v3 without immutable revisions and hashes. The explicitly pinned tiny.en model and isolated test environment above resolve cold offline execution for that test profile only. Other models remain unverified. Do not copy the supplied virtual environment as a portable runtime or automatically download models.

## Defect record and remaining qualification

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

ASR-01 through ASR-06 are corrected: cache identity binds exact audio, model tree,
settings and the actual replacement child runtime; unrelated text is never
adopted; silent language fallback is removed; every requested artifact must
exist with its declared hash; inference uses one owned process; cancellation
terminates it and preserves independently verified checkpoints. ASR-07 has
executed for the pinned tiny.en profile only. ASR-08 remains unimplemented:
no diarization or word-alignment feature is advertised.

## Incremental adoption

1. Retain the current worker and protocol as the source of reuse; capture immutable identity and authorized provenance, remove private/machine-specific content and historical patch clutter.
2. Correct the listed correctness/resource issues in that implementation with focused regressions. Consolidate active V4.3 override/patch paths where necessary rather than maintain two competing workers.
3. Adapt request/progress/result schemas to the platform's existing operation ID, source/model/settings identity, artifact publication, cancellation and recovery records.
4. Register the actual runtime/model capability and preserve its dependency environment separately from the reader/UI.
5. Connect accessible native selection, language/options, status, transcript editing and export through existing commands. Keep original audio, transcript revisions and translated derivatives separate.
6. Independently verify real authorized audio, known speech/non-speech and multilingual samples, timestamps/exports, same-name/corrupt/cache cases, long-file cancellation, CPU/GPU behavior, no-network startup and native Windows/NVDA. Measure quality and resource use on declared hardware; do not claim all135languages.

Advisory leads: CARR03 implementation/adaptation,10 jobs/resources,05 recovery,09 accessible UI,11 runtime/package,12/13/14/15 independent acceptance. Source availability no longer blocks review; runtime/rights/quality prerequisites remain explicit. Work on other platform modules continues.
