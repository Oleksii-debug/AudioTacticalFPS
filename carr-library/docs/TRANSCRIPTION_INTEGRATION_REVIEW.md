# Supplied Whisper Transcribe — integration review

Original source-review date: 2026-11-27. The findings below describe the
supplied V4.3 source at intake. Later integration is recorded separately here
and in the linked current contracts; the original NOT_RUN labels are not a
statement that subsequent public adapter work is absent.

## Subsequent integration and canonical reconciliation

Canonical source `e3cf74c243d4ef930b526b5648ca9003c0900057` records the exact
37,974-byte supplied worker SHA-256 as
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
Its review also records a Linux protocol run with an isolated synthetic SDK:
successful Ukrainian exports, an explicit unsupported automatic-detection
failure, and a mandatory-export failure. That is historical synthetic protocol
evidence, not actual recognition or Windows/model qualification. The private
worker, models, recordings and transcripts remain outside this repository.

The selected implementation now uses the existing
[manifest-bound transcription service](SUPPLIED_TRANSCRIPTION_INTEGRATION.md),
[current CLI and independent exports](TRANSCRIPTION_CLI.md), and
[typed complete backups](SUPPLIED_TRANSCRIPTION_BACKUP.md). Canonical old job
bytes are preserved by [explicit inert recovery](CANONICAL_TRANSCRIPTION_RECOVERY.md).
No archived executable path becomes authority after restore.

The missing typed assistant `audio.transcribe` route is implemented through
that same service: [exact plan and recovery contract](ASSISTANT_TRANSCRIPTION.md).
Its deterministic job intent is durable before prepare, the admitted definition
is pinned before launch, and the export intent precedes publication. Completed
resume verifies the same owned source/results and independent export without
repeating inference. A changed definition, forged receipt or uncertain directory
sync stays a visible failure. New source/installed regression cases use the
existing harmless real CLI peer and require their exact composed CI run.

ASR-01/02 are addressed by immutable owned inputs and content/settings/runtime
receipts; ASR-03/04 by the reviewed no-fallback preparation and mandatory output
verification; ASR-05/06 by existing serialized admission and whole-family
cancellation. Interrupted files can restart; acoustic mid-file recovery is not
claimed. Actual selected runtime/model inference, supported languages, recognition
quality, physical hardware and manual NVDA still need their own evidence.
This remains a work package under the
[current platform specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md).

## Source and boundary

The owner-supplied implementation identifies itself as V4.3. The current source includes worker.py (37,974 bytes), accessible_window.ps1 (49,718 bytes), README_UA.txt and download_models_once.py. The readable worker source was inspected. File bytes/immutable source hash and dependency lock must be recorded before code adoption; this review has not created a redistributed source snapshot.

The source location is retained in the authorized private worker input, not in public Git. Its personal jobs, recordings/transcripts, virtual environment, historical backups/patch clutter and machine-specific paths must not be imported. No root LICENSE, dependency lock or pyproject.toml was observed; establish provenance/rights for the supplied wrapper and every runtime/model before redistribution. Do not invent a license for the owner's code.

The existing backend already supports a headless process:

    python worker.py --job-dir <validated job-directory>

It reads job_config.json and settings_live.json, responds to pause.flag/stop.flag and writes job_status.json plus selected transcript outputs. The platform should adapt this boundary to its shared job/capability/backup services and native wx UI, not embed another independent PowerShell application.

Reusable functionality includes a persistent faster_whisper.WhisperModel, CPU int8/CUDA choices, local model loading with local_files_only=True, file/folder batches, language selection/detection, VAD and TXT/SRT/VTT/JSON export. Verify each after integration; source presence is not output-quality evidence.

## Runtime inventory, not an approved lock

The supplied environment metadata records Python3.14.5, faster-whisper1.2.1, CTranslate2 4.8.0, PyAV17.1.0, ONNX Runtime1.26.0, NumPy2.4.6, huggingface-hub1.19.0 and tokenizers0.23.1. These observations do not prove compatibility or portability. The current platform uses a different CPython3.11 runtime; retain an isolated reviewed engine pack or rebuild and test a deliberately selected compatible set.

No models directory was observed at the supplied project root. The downloader names tiny/base/small/medium/large-v3-turbo/large-v3 without immutable revisions and hashes. Actual model availability, complete dependencies and cold offline operation remain unverified. Do not copy the virtual environment as a portable runtime or automatically download all models.

## Findings to resolve before qualification

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

## Incremental adoption

1. Retain the current worker and protocol as the source of reuse; capture immutable identity and authorized provenance, remove private/machine-specific content and historical patch clutter.
2. Correct the listed correctness/resource issues in that implementation with focused regressions. Consolidate active V4.3 override/patch paths where necessary rather than maintain two competing workers.
3. Adapt request/progress/result schemas to the platform's existing operation ID, source/model/settings identity, artifact publication, cancellation and recovery records.
4. Register the actual runtime/model capability and preserve its dependency environment separately from the reader/UI.
5. Connect accessible native selection, language/options, status, transcript editing and export through existing commands. Keep original audio, transcript revisions and translated derivatives separate.
6. Independently verify real authorized audio, known speech/non-speech and multilingual samples, timestamps/exports, same-name/corrupt/cache cases, long-file cancellation, CPU/GPU behavior, no-network startup and native Windows/NVDA. Measure quality and resource use on declared hardware; do not claim all135languages.

Advisory leads: CARR03 implementation/adaptation,10 jobs/resources,05 recovery,09 accessible UI,11 runtime/package,12/13/14/15 independent acceptance. Source availability no longer blocks review; runtime/rights/quality prerequisites remain explicit. Work on other platform modules continues.
