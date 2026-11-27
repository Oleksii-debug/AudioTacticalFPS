# Local transcription with the supplied Whisper worker

This workspace connects the owner's existing local Whisper worker to the current library application. It does not introduce a second recognizer, download models, or send recordings to an online service.

## Native workflow

Open a library, then **Library → Transcription: local audio and video...**.

1. Choose **Підключити Whisper** and select the reviewed worker's Python interpreter, its repaired worker.py, and the folder containing already installed local models. Model/runtime files remain separate from the backed-up research library.
2. Enter a title, model name, language code (or auto), CPU/CUDA profile, and whether to request per-word timestamps or mixed-language recognition.
3. Choose **Додати записи**. The program makes checked private copies; original filenames remain visible and duplicate names are retained as separate inputs.
4. Choose **Запустити / продовжити**. The shared background-operation controls remain available. A stop first requests cooperation, then terminates a worker which has not stopped within the grace interval. Completed files remain reusable; an interrupted file is replayed from its beginning. A stop defers large receipt/source verification until the next explicit resume, so unverified saved files are not counted as completed exports.
5. Open the selected transcript and save corrections with **Зберегти правки**. Every save creates a separate edit version. Recognition text, original media and initial timestamps are preserved. Editing plain text does not realign the subtitles automatically.
6. Choose **Експорт** and a new directory. The export includes verified TXT, phrase SRT, phrase VTT and timed JSON, plus word SRT/VTT when selected, a manifest and separately labelled edited text. An existing directory is never overwritten.

The window uses named native controls, keyboard mnemonics and the application's existing background/progress service. Unsaved changes must be saved or explicitly discarded before switching or closing. Actual NVDA usability is a separate manual acceptance step.

The running indicator counts files reported by the worker; these are not accepted exports until their receipts are verified. After Stop, the indicator retains the measured count while the job can truthfully show zero verified results pending explicit resume. Only a completed, verified job reaches 100 percent. Optional progress failures cannot undo a saved job, trigger a second transcription, or interrupt cooperative cancellation.

## Durable jobs and restoration

Jobs belong to their original active library. Every operation rechecks that scope. Retained media, durable receipts, results and edit versions live in application state and are included by the existing typed complete backup. Stop transcription before backing up. This host currently retains its 512 MiB total sidecar allowance: a larger transcription collection makes backup fail visibly before publication, rather than omitting recordings. Retained inputs are not the optional generated-audio cache. Rebuildable recognizer caches, runtime homes, temporary files and diagnostic logs are excluded; incomplete recordings may require recognition again after restore.

Exports and restored completed jobs verify retained bytes and each current-run receipt again. A receipt must agree with the selected worker/interpreter, exact settings, source hash, all requested outputs and timed JSON hash. A worker exit code or an existing same-name TXT alone cannot mark a job completed. Partial or failed exports remain visibly partial or failed.

A restored engine configuration may point to runtime/model locations that are unavailable on the new computer. Select the installed runtime there before resuming. Completed results and edits can still be read/exported without the old engine. Moving an interrupted job can require recognizing its incomplete inputs again because the worker's cache identity includes its bound source location.

## Shared command line

The native workspace and command line call LibraryApp.transcription and the same service.

Run python -m carr_library --list-libraries --data-dir DATA to obtain the active library ID. Then:

- python -m carr_library transcribe --data-dir DATA --workspace ID configure --python PYTHON --worker WORKER --models-dir MODELS
- python -m carr_library transcribe --data-dir DATA --workspace ID create recording.wav --title Lecture --language auto --model small
- python -m carr_library transcribe --data-dir DATA --workspace ID run JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID jobs
- python -m carr_library transcribe --data-dir DATA --workspace ID verify JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID export JOB_ID NEW_DIRECTORY

The create command also supports --multilingual, --no-word-timestamps, --device, --compute-type and an exact --model-revision commit. The edit command takes a retained source filename from show and a UTF-8 --text-file. Recognition is original-language transcription; translation is a separate existing application workflow.

## Evidence boundaries

The public regression worker is deliberately synthetic. Its actual subprocess tests cover retention, isolation, receipts, cancellation, timeout, restoration, edits and CLI/native integration. Those tests are not Whisper inference or acoustic-quality evidence.

The supplied private worker needs its own reviewed repair and real local-runtime tests. Runtime configuration does not mean that inference, language quality, mixed-language alignment, diarization, owner cloning or Windows11/NVDA acceptance has passed. Recognition requires a compatible offline faster-whisper/CTranslate2 installation and complete local models. No automatic replacement recognizer or remote fallback is selected.

The launch uses isolated Python startup, the existing site-path bootstrap, a minimal environment, offline library settings, blocked Python network/child-command events and Python writes confined to the job. This is not an operating-system sandbox for arbitrary native libraries. Only a reviewed explicitly selected local runtime should be configured.
