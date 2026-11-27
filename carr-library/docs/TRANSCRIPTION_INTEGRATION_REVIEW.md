# Supplied Whisper Transcribe — integration review

Review date: 2026-11-27. Status: PRIVATE_V5_CORRECTNESS_CANDIDATE_INDEPENDENTLY_TESTED; supplied-worker integration/runtime/Windows/NVDA/model-quality acceptance remains separate. This is a work package under the [current platform specification](UNIVERSAL_SCOPE_V3_FINALIZATION.md), not a separate ASR product.

## Follow-up: supplied V5 correctness candidate

The owner updated the supplied implementation to V5 while the initial V4.3
review was in progress. The private correction reuses that V5 and its existing
worker, model resolver and transcript exporter. Original owner files were not
changed. No supplied worker, personal file, environment or model is included in
this public repository.

| Source identity | SHA256 |
| --- | --- |
| Original V4.3 worker, 37,974 bytes | `1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9` |
| Reviewed V5 worker baseline | `712682aa5b2ed922956e4e20652143f6941899447f6e5417a5fa892b42854fc7` |
| Corrected private V5 worker | `137c3be36d5bada0c1d5e971f13fdd3d234747fae6dadb5c642e023a3357e7b6` |
| Corrected private transcript exporter | `fed67d1ea0ffc1a5ef06dc106642363ecb31d4c0e68165de1447cda8af02f945` |

The exact delivered candidate passed **106 tests and 24 subtests, with no
failures or skips**, including an independent review. Execution used Python
3.12.14, pytest 9.0.2, synthetic source/model bytes and an injected inference
boundary. The original V5 failed its supplied silent-rerun aggregate test and
all eleven newly added correctness regressions. These are source-control-flow
results, not neural inference, acoustic quality or native accessibility evidence.

The correction binds cached results to actual audio/model bytes, normalized
recognition settings, runtime package versions and wrapper-source hashes. It
checks these identities during recognition and before publication. V5's refusal
to adopt unrelated basename TXT files and its explicit language intent are
preserved. Required exports are verified against the exact bytes rendered by
the exporter; a later edit to an earlier output cannot become a valid result.
The result manifest commits last, and completed/cached counters follow that
commit. Partial failures retain only verified successful records. Silent reruns
and live-setting changes preserve truthful aggregate text. Pause/stop checks
occur before the first and each later lazy generator step, during hashing and
before publication; the existing shared-model reference remains in use.

The independent review found and corrected an intermediate defect where a later
file could alter an earlier export before the final seal. The final tests cover
TXT, phrases, words, SRT, VTT, JSON, aggregate corruption and receipt-write
failure. The private deliverable retains the exact baseline, corrected files,
patch, portable tests, JUnit evidence and per-file hashes.

The legacy job-directory entry point and status keys remain compatible with
the supplied V5. Its new `result_manifest.json` is **not yet the public
application's per-file receipt protocol**. Reconcile it with the existing
supplied-worker integration in PR140; do not introduce another ASR queue or
claim that this PR99-based candidate has already integrated transcription.
Package-version receipts explicitly say
`package_versions_only_not_a_qualified_runtime_pack`. Blocking native inference
still needs the existing application supervisor's bounded stop/termination
policy. Exact runtime/model inventory, rights, real inference, language quality,
word alignment/diarization and Windows/NVDA acceptance remain unqualified here.

## Source and boundary

The initially supplied implementation identifies itself as V4.3. That source includes worker.py (37,974 bytes), accessible_window.ps1 (49,718 bytes), README_UA.txt and download_models_once.py. The readable worker source was inspected; its immutable identity is now recorded above. A qualified dependency lock is still required before runtime adoption. No redistributed source snapshot is created by this public review.

The source location is retained in the authorized private worker input, not in public Git. Its personal jobs, recordings/transcripts, virtual environment, historical backups/patch clutter and machine-specific paths must not be imported. No root LICENSE, dependency lock or pyproject.toml was observed; establish provenance/rights for the supplied wrapper and every runtime/model before redistribution. Do not invent a license for the owner's code.

The existing backend already supports a headless process:

    python worker.py --job-dir <validated job-directory>

It reads job_config.json and settings_live.json, responds to pause.flag/stop.flag and writes job_status.json plus selected transcript outputs. The platform should adapt this boundary to its shared job/capability/backup services and native wx UI, not embed another independent PowerShell application.

Reusable functionality includes a persistent faster_whisper.WhisperModel, CPU int8/CUDA choices, local model loading with local_files_only=True, file/folder batches, language selection/detection, VAD and TXT/SRT/VTT/JSON export. Verify each after integration; source presence is not output-quality evidence.

## Runtime inventory, not an approved lock

The supplied environment metadata records Python3.14.5, faster-whisper1.2.1, CTranslate2 4.8.0, PyAV17.1.0, ONNX Runtime1.26.0, NumPy2.4.6, huggingface-hub1.19.0 and tokenizers0.23.1. These observations do not prove compatibility or portability. The current platform uses a different CPython3.11 runtime; retain an isolated reviewed engine pack or rebuild and test a deliberately selected compatible set.

No models directory was observed at the supplied project root. The downloader names tiny/base/small/medium/large-v3-turbo/large-v3 without immutable revisions and hashes. Actual model availability, complete dependencies and cold offline operation remain unverified. Do not copy the virtual environment as a portable runtime or automatically download all models.

## Initial V4.3 findings and qualification requirements

Line references identify the initial V4.3 worker.py snapshot. The private V5
source corrections and their limits are recorded above; these historical line
numbers do not describe the corrected candidate.

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
