# Local transcription with the supplied Whisper worker

This workspace connects the owner's existing local Whisper worker to the current library application. It does not introduce a second recognizer, download models, or send recordings to an online service.

## Native workflow

Open a library, then **File → Transcription: local audio and video...**. The same command first asks for the installed worker profile and its scope. It preserves the saved keyboard binding and does not migrate or reinterpret existing jobs.

| Profile | Job scope | Runtime selection |
|---|---|---|
| receipts-v1 | Selected workspace | Explicit interpreter, reviewed receipt worker and local models |
| whisper-v5-manifest-v1 | Selected workspace | Explicit repaired V5 worker with its two helpers and local models |
| supplied-whisper-v43-hardened-1 | All jobs in the current library | Explicit reviewed runtime manifest with its complete inventory |

The two workspace profiles use the editor below. Select an active workspace before opening them. The library-wide profile can open without an active workspace and shows its scope in a named read-only field.

### Workspace profiles

1. Choose **Підключити Whisper**, select the explicit worker profile, then its Python interpreter, reviewed worker.py, and installed local models. The supplied repaired V5 uses **whisper-v5-manifest-v1** and requires its unchanged local_models.py and transcript_features.py beside worker.py. The older receipt-producing integration uses **receipts-v1**. Model/runtime files remain separate from the backed-up research library.
2. Enter a title, model name, language code (or auto), CPU/CUDA profile, and whether to request per-word timestamps or mixed-language recognition.
3. Choose **Додати записи**, or **Додати папку з підпапками** for one explicitly selected local folder. Folder intake includes nested folders, checks supported audio/video extensions and reports how many media files were retained and how many unsupported files were excluded. Empty or unsupported-only folders fail visibly. Links, reparse points, unsafe sources, scan limits and size limits stop the whole intake before a job is published. The program makes checked private copies; folder-relative names remain visible and duplicate basenames are retained as separate inputs.
4. Choose **Запустити / продовжити**. The shared background-operation controls remain available. A stop first requests cooperation, then terminates a worker which has not stopped within the grace interval. Completed files remain reusable; an interrupted file is replayed from its beginning. A stop defers large receipt/source verification until the next explicit resume, so unverified saved files are not counted as completed exports.
5. Open the selected transcript and save corrections with **Зберегти правки**. Every save creates a separate edit version. Recognition text, original media and initial timestamps are preserved. Editing plain text does not realign the subtitles automatically.
6. Choose **Експорт** and a new directory. The export includes verified TXT, phrase SRT, phrase VTT and timed JSON, plus word SRT/VTT when selected, a manifest and separately labelled edited text. An existing directory is never overwritten.

The window uses named native controls, keyboard mnemonics, the application's existing background worker, and the current shared ProgressHub/ProgressReporter. Each operation binds and restores its progress context; a detached observer cannot change the saved service outcome. Unsaved changes must be saved or explicitly discarded before switching or closing. Actual NVDA usability is a separate manual acceptance step.

Folder checks and source retention run in the shared cancellable background operation. The existing intake allowance is 64 media files, 8 GiB per file and 32 GiB per job; the shared scanner also bounds directory entries and depth. Select smaller folders when a limit is reached. Unsupported sidecars are excluded by extension; a supported extension does not certify that the worker can decode the contents.

The running indicator counts files reported by the worker; these are not accepted exports until their receipts are verified. After Stop, the indicator retains the measured count while the job can truthfully show zero verified results pending explicit resume. Only a completed, verified job reaches 100 percent. Optional progress failures cannot undo a saved job, trigger a second transcription, or interrupt cooperative cancellation.

### Reviewed library-wide profile

Choose **Library-wide jobs — reviewed runtime, 17 settings** in the same profile selector. Add explicit audio/video files or a folder, choose the reviewed runtime manifest, and set the job title. Folder selection captures its current supported files; newly added files are not silently adopted.

All 17 settings remain independently typed: output grouping, speech task, translation target, input language, model, device, compute type, beam size, parallel files, VAD, source headers, TXT, SRT, VTT, JSON, multilingual inference and previous-text conditioning. Grouping can keep separate TXT files, combine each selected folder, or combine the whole job. Speech translation has the explicit target English. The chosen output subset is preserved; this profile does not force the workspace profiles' four-format contract.

**Prepare retained inputs** captures the exact inputs, settings and runtime identity without launching inference. Prepared settings become read-only. **Start prepared job** separately checks the explicitly selected manifest against that saved identity and starts the owned worker. **Pause**, **Resume** and **Stop** control that running job. A pause request is distinguished from the worker's observed checkpoint; stopped jobs can be reviewed and started again through the same saved job.

**Verify outputs** rechecks saved completion and output bytes without activating the previous runtime. **Edit reading copy** opens the existing versioned narration editor only after that recheck. Its Save All participant saves a separate reading project, preserving the recognized output and its original hash. It does not create or rewrite a workspace-profile transcript edit receipt.

A verified execution is accepted only after successful process exit, a matching completion receipt, retained input/output checks and the durable complete state. File counts reported during execution remain provisional. The existing desktop Future finalizes the same shared progress task; a detached progress observer cannot replace the service result or turn a late stop into a failed committed execution.

## Durable jobs and restoration

Workspace-profile jobs belong to their original selected workspace. Every operation rechecks that scope. Retained media, durable receipts, results and edit versions live in application state and are included by the existing typed complete backup. Stop transcription before backing up. This host currently retains its 512 MiB total sidecar allowance: a larger transcription collection makes backup fail visibly before publication, rather than omitting recordings. Retained inputs are not the optional generated-audio cache. Rebuildable recognizer caches, runtime homes, temporary files and diagnostic logs are excluded; incomplete recordings may require recognition again after restore.

Library-wide supplied jobs remain under their separate, explicitly typed library namespace. Their immutable definitions, retained inputs, accepted execution/history records, integrity logs, receipts, outputs and publication-recovery records are included by the existing complete backup. These integrity logs are durable evidence and are not discarded as ordinary workspace-profile diagnostic logs. Restore validates them passively without activating or reading the original external model/runtime installation. The existing backup format and overall size allowance remain unchanged.

Exports and restored completed jobs verify retained bytes and each current-run receipt again. A receipt must agree with the selected worker/interpreter, exact settings, source hash, all requested outputs and timed JSON hash. A worker exit code or an existing same-name TXT alone cannot mark a job completed. Partial or failed exports remain visibly partial or failed.

A restored engine configuration may point to runtime/model locations that are unavailable on the new computer. Select the installed runtime there before resuming. Completed results and edits can still be read/exported without the old engine. Moving an interrupted job can require recognizing its incomplete inputs again because the worker's cache identity includes its bound source location.

## Shared command line

The native entrypoint and command line select the same existing protocol-specific services. Workspace commands retain their current --workspace argument. The reviewed library-wide protocol requires --profile supplied-whisper-v43-hardened-1 and rejects --workspace, so an ID cannot silently change scope.

Run python -m carr_library --list-libraries --data-dir DATA to obtain the active library ID. Then:

- python -m carr_library transcribe --data-dir DATA --workspace ID configure --python PYTHON --worker WORKER --models-dir MODELS
- python -m carr_library transcribe --data-dir DATA --workspace ID configure --protocol whisper-v5-manifest-v1 --python PYTHON --worker WORKER --models-dir MODELS
- python -m carr_library transcribe --data-dir DATA --workspace ID create recording.wav --title Lecture --language auto --model small
- python -m carr_library transcribe --data-dir DATA --workspace ID create-folder RECORDINGS_DIRECTORY --title Lectures --language auto --model small
- python -m carr_library transcribe --data-dir DATA --workspace ID run JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID jobs
- python -m carr_library transcribe --data-dir DATA --workspace ID verify JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID export JOB_ID NEW_DIRECTORY

The create and create-folder commands also support --protocol, --multilingual, --no-word-timestamps, --device, --compute-type and --max-attempts. New jobs persist the configured profile unless explicitly selected. An exact --model-revision commit is supported only by receipts-v1; V5 binds its installed model directory by content instead. create-folder returns the new job ID, retained-file count and excluded unsupported-file count. The edit command takes a retained source filename from show and a UTF-8 --text-file. These workspace profiles perform original-language transcription; their settings are not coerced into the different library-wide speech-translation contract.

### Library-wide commands

Use the same transcribe command, choosing the protocol explicitly:

- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 prepare RECORDINGS_DIRECTORY --manifest MANIFEST --title Lectures --language uk --output-mode one_per_folder --output-srt --output-vtt --output-json
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 jobs
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 show JOB_ID
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 run JOB_ID --manifest MANIFEST
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 pause JOB_ID
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 resume JOB_ID
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 stop JOB_ID
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 verify JOB_ID
- python -m carr_library transcribe --data-dir DATA --profile supplied-whisper-v43-hardened-1 reading-copy JOB_ID --output EXACT_RELATIVE_OUTPUT

Prepare exposes all 17 typed settings as matching command options. Boolean options have explicit --no- alternatives; use --help for the complete names. For speech translation, select --task translate --translation-target en and retain the actual input language. Each Start requires --manifest again; verification and reading copies do not.

Pause, Resume and Stop report that a control request was written, not that a checkpoint or successful completion has already occurred. Run returns success only after the existing service accepts the durable receipt. A cooperative stop returns code 2; Ctrl+C returns 130; validation or execution failure returns 1. Reading-copy saves through the existing narration store and returns its project ID, saved hash and original text hash. The original recognized output remains unchanged.

## Supplied V5 profile

The profile adapts the supplied result_manifest.json into the same retained jobs, verified receipts, editing, export and backup service. It does not install or replace the recognizer. Select a complete CTranslate2 model folder containing model.bin, config.json and tokenizer.json, or a parent containing the selected model-name folder. Links and ambiguous remote/cache selection are rejected; no model is downloaded.

Configuration binds the interpreter, worker.py and both helper files by SHA256. Launch and result admission recheck those identities. Each run records the exact local model files, retained input identities and requested settings. It uses a fresh results/runs directory with a host run ID, while the supplied worker's logs/cache_v5 directory stays in the existing job for explicit resume. An earlier run's manifest or a same-name TXT cannot complete a new run.

The private controlled-backend interoperability check used these unchanged supplied sources; configuration records the identities of the files actually selected, and does not download them:

| File | SHA256 of the reviewed supplied V5 source |
|---|---|
| worker.py | 137c3be36d5bada0c1d5e971f13fdd3d234747fae6dadb5c642e023a3357e7b6 |
| local_models.py | b219f311656058fbc2e46b5b2dcbaaa22fcfefeea551213c9af79816609e1d5f |
| transcript_features.py | fed67d1ea0ffc1a5ef06dc106642363ecb31d4c0e68165de1447cda8af02f945 |

V5 requires its fixed retry policy: at most three distinct attempts, preserving the requested language and model. Later attempts use beam 1 without previous-text conditioning, then disable VAD. max_attempts defaults to 3 for V5; other explicit limits and model_revision are rejected before launching. The actual successful attempt and effective settings remain in the job, original JSON and export manifest. Mixed-language recognition requires language auto; an explicit language is never silently replaced. Separate-file exports do not support include_headers.

The host verifies the current manifest's counters, source/model/helper identities, requested and effective settings, metadata fingerprint, timed JSON and every declared output before publishing a completed job. Missing, changed, duplicated or escaped outputs fail admission. Word SRT/VTT are labelled host derivatives of validated original JSON word timings. A text-bearing segment without actual word timings cannot produce those subtitles; the host does not invent alignment. Original JSON retains the worker's language uncertainty and original timestamps.

Jobs created before profile support remain receipts-v1. Switching engine configuration cannot reinterpret their results or resume them under a different contract. Completed jobs, including restored V5 jobs, remain readable and exportable without the old interpreter, helper files or model installation. Unknown profile names fail explicitly. Supplied V4.3 output-only workers are not implicitly treated as either workspace profile. The explicit supplied-whisper-v43-hardened-1 profile requires its own admitted runtime and hardened receipt contract; its 17 settings and library-wide history are kept separate.

## Evidence boundaries

The public regression worker is deliberately synthetic. Its actual subprocess tests cover retention, isolation, receipts, cancellation, timeout, restoration, edits and CLI/native integration. Those tests are not Whisper inference or acoustic-quality evidence.

The unchanged supplied repaired V5 worker and both helpers were also executed through this service with a controlled backend and dummy model files, outside public Git. That check covers protocol interoperability, actual wrapper exports, same-language retry and cache-preserving Stop/resume. It does not exercise Whisper acoustic inference. Runtime configuration and these protocol tests do not mean that model quality, mixed-language alignment, diarization, owner cloning or Windows11/NVDA acceptance has passed. Recognition requires a compatible offline faster-whisper/CTranslate2 installation and complete local models. No automatic replacement recognizer or remote fallback is selected.

The receipt/V5 workspace launch uses isolated Python startup, the existing site-path bootstrap, a minimal environment, offline library settings, blocked Python network/child-command events and Python writes confined to the job. This is not an operating-system sandbox for arbitrary native libraries. Only a reviewed explicitly selected local runtime should be configured.
