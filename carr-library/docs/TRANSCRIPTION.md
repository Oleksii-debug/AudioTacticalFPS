# Local transcription with the supplied Whisper worker

This workspace connects the owner's existing local Whisper worker to the current library application. It does not introduce a second recognizer, download models, or send recordings to an online service.

## Native workflow

Open a library, then **Library → Transcription: local audio and video...**.

1. Choose **Підключити Whisper**, select the explicit worker profile, then its Python interpreter, reviewed worker.py, and installed local models. The supplied repaired V5 uses **whisper-v5-manifest-v1** and requires its unchanged local_models.py and transcript_features.py beside worker.py. The older receipt-producing integration uses **receipts-v1**. Model/runtime files remain separate from the backed-up research library.
2. Enter a title, model name, language code (or auto), CPU/CUDA profile, and whether to request per-word timestamps or mixed-language recognition.
3. Choose **Додати записи**, or **Додати папку з підпапками** for one explicitly selected local folder. Folder intake includes nested folders, checks supported audio/video extensions and reports how many media files were retained and how many unsupported files were excluded. Empty or unsupported-only folders fail visibly. Links, reparse points, unsafe sources, scan limits and size limits stop the whole intake before a job is published. The program makes checked private copies; folder-relative names remain visible and duplicate basenames are retained as separate inputs.
4. Choose **Запустити / продовжити**. The shared background-operation controls remain available. A stop first requests cooperation, then terminates a worker which has not stopped within the grace interval. Completed files remain reusable; an interrupted file is replayed from its beginning. A stop defers large receipt/source verification until the next explicit resume, so unverified saved files are not counted as completed exports.
5. Open the selected transcript and save corrections with **Зберегти правки**. Every save creates a separate edit version. Recognition text, original media and initial timestamps are preserved. Editing plain text does not realign the subtitles automatically.
6. Choose **Експорт** and a new directory. The export includes verified TXT, phrase SRT, phrase VTT and timed JSON, plus word SRT/VTT when selected, a manifest and separately labelled edited text. An existing directory is never overwritten.

The window uses named native controls, keyboard mnemonics and the application's existing background/progress service. Unsaved changes must be saved or explicitly discarded before switching or closing. Actual NVDA usability is a separate manual acceptance step.

Folder checks and source retention run in the shared cancellable background operation. The existing intake allowance is 64 media files, 8 GiB per file and 32 GiB per job; the shared scanner also bounds directory entries and depth. Select smaller folders when a limit is reached. Unsupported sidecars are excluded by extension; a supported extension does not certify that the worker can decode the contents.

The running indicator counts files reported by the worker; these are not accepted exports until their receipts are verified. After Stop, the indicator retains the measured count while the job can truthfully show zero verified results pending explicit resume. Only a completed, verified job reaches 100 percent. Optional progress failures cannot undo a saved job, trigger a second transcription, or interrupt cooperative cancellation.

## Durable jobs and restoration

Jobs belong to their original active library. Every operation rechecks that scope. Retained media, durable receipts, results and edit versions live in application state and are included by the existing complete backup. Large jobs require sufficient backup space; retained inputs are not the optional generated-audio cache.

Exports and restored completed jobs verify retained bytes and each current-run receipt again. A receipt must agree with the selected worker/interpreter, exact settings, source hash, all requested outputs and timed JSON hash. A worker exit code or an existing same-name TXT alone cannot mark a job completed. Partial or failed exports remain visibly partial or failed.

A restored engine configuration may point to runtime/model locations that are unavailable on the new computer. Select the installed runtime there before resuming. Completed results and edits can still be read/exported without the old engine. Moving an interrupted job can require recognizing its incomplete inputs again because the worker's cache identity includes its bound source location.

## Shared command line

The native workspace and command line call LibraryApp.transcription and the same service.

Run python -m carr_library --list-libraries --data-dir DATA to obtain the active library ID. Then:

- python -m carr_library transcribe --data-dir DATA --workspace ID configure --python PYTHON --worker WORKER --models-dir MODELS
- python -m carr_library transcribe --data-dir DATA --workspace ID configure --protocol whisper-v5-manifest-v1 --python PYTHON --worker WORKER --models-dir MODELS
- python -m carr_library transcribe --data-dir DATA --workspace ID create recording.wav --title Lecture --language auto --model small
- python -m carr_library transcribe --data-dir DATA --workspace ID create-folder RECORDINGS_DIRECTORY --title Lectures --language auto --model small
- python -m carr_library transcribe --data-dir DATA --workspace ID run JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID jobs
- python -m carr_library transcribe --data-dir DATA --workspace ID verify JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID export JOB_ID NEW_DIRECTORY

The create and create-folder commands also support --protocol, --multilingual, --no-word-timestamps, --device, --compute-type and --max-attempts. New jobs persist the configured profile unless explicitly selected. An exact --model-revision commit is supported only by receipts-v1; V5 binds its installed model directory by content instead. create-folder returns the new job ID, retained-file count and excluded unsupported-file count. The edit command takes a retained source filename from show and a UTF-8 --text-file. Recognition is original-language transcription; translation is a separate existing application workflow.

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

Jobs created before profile support remain receipts-v1. Switching engine configuration cannot reinterpret their results or resume them under a different contract. Completed jobs, including restored V5 jobs, remain readable and exportable without the old interpreter, helper files or model installation. Unknown profile names fail explicitly. Supplied V4.3 output-only workers are not implicitly treated as either supported profile.

## Evidence boundaries

The public regression worker is deliberately synthetic. Its actual subprocess tests cover retention, isolation, receipts, cancellation, timeout, restoration, edits and CLI/native integration. Those tests are not Whisper inference or acoustic-quality evidence.

The unchanged supplied repaired V5 worker and both helpers were also executed through this service with a controlled backend and dummy model files, outside public Git. That check covers protocol interoperability, actual wrapper exports, same-language retry and cache-preserving Stop/resume. It does not exercise Whisper acoustic inference. Runtime configuration and these protocol tests do not mean that model quality, mixed-language alignment, diarization, owner cloning or Windows11/NVDA acceptance has passed. Recognition requires a compatible offline faster-whisper/CTranslate2 installation and complete local models. No automatic replacement recognizer or remote fallback is selected.

The launch uses isolated Python startup, the existing site-path bootstrap, a minimal environment, offline library settings, blocked Python network/child-command events and Python writes confined to the job. This is not an operating-system sandbox for arbitrary native libraries. Only a reviewed explicitly selected local runtime should be configured.

## Windows live status publication

The host can briefly hold a JSON snapshot open while the selected worker publishes
the next generation. Windows can refuse that replacement even when the reader
allows delete sharing. The bridge therefore installs a narrowly scoped publisher
shim in its existing audited bootstrap: on Windows only, an `os.replace` call
with its default signature is eligible only when its destination is exactly this
job's `job_status.json` and its source is in the same job directory.

Only native sharing/access refusals with Windows error 5, 32 or 33 are retried.
The monotonic deadline is half a second, with waits of at most 10 milliseconds.
Every attempt still passes through the existing write audit and native replace
operation. A persistent refusal raises the original error; unrelated paths,
additional arguments, other errors and non-Windows platforms keep their ordinary
behavior. The host's existing `atomic_bytes(..., replace=True)` publisher uses
the same bounded Windows refusal policy. Create-only publication remains unchanged.

This changes the public bridge and state publisher, not the selected private
worker's source or identity. It does not qualify a speech model or acoustic
quality. Native acceptance must distinguish a transient reader that closes from
a reader that remains open through the entire deadline; indefinite-open
replacement is not promised.
