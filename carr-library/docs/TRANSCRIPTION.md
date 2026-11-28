# Local transcription with the supplied Whisper worker

This workspace connects the owner's existing local Whisper worker to the current library application. It does not introduce a second recognizer, download models, or send recordings to an online service.

## Native workflow

Open an active library, then **Library → Local transcription…**. The existing semantic command is **transcription.open**, so saved shortcuts keep their command identity.

1. In **Local runtime**, choose the exact worker protocol, its Python executable, reviewed worker and existing model directory. Choose **Inspect files**. Inspection hashes the selected runtime/model inventory without running an inference engine. Review the fingerprint and file origin, explicitly check the trust confirmation, then choose **Register trusted runtime**. Multiple profiles can coexist; registration does not change a saved job or select a replacement engine globally.
2. The supplied repaired V5 uses **whisper-v5-manifest-v1** and requires its unchanged local_models.py and transcript_features.py beside worker.py. The receipt-producing integration uses **receipts-v1**. Choose the registered profile in **New transcription**. Enter title, local model name, language or auto, CPU/CUDA, beam size, bounded parallel-file count, VAD, previous-text context and available word timestamps. V5 rejects unsupported headers and contradictory explicit-language/multilingual choices. These admitted protocols retain the source language; speech translation is visibly unavailable.
3. Choose **Add files…** and/or **Add folder…**. Folder recursion is an explicit checkbox in this workflow. **Prepare saved job** retains and publishes the reviewed inputs without starting inference; the status reports retained and excluded counts. **Start / resume** can also prepare a new job and run it after retention. Links, reparse points, unsafe sources, scan limits and size limits stop the intake before a job is published. Folder-relative names remain visible and duplicate basenames remain distinct.
4. **Start / resume** on a saved job uses its saved settings and exact runtime profile. **Pause at checkpoint** and **Cancel operation** durably record intent. A live worker must acknowledge closure before the job is reported paused/cancelled; those commands do not certify that an orphan has stopped. Completed files stay retained, while an interrupted file is replayed from its beginning. Closing during work requests a checkpoint and waits; progress does not move focus.
5. In **Saved jobs and results**, choose a job, a source and **Load transcript**. The reading copy exposes **Undo reading edit**, **Redo reading edit**, **Save edits**, Ctrl+S and the main window's **Save All**. Each successful save creates a separate derivative. Save All waits for the durable acknowledgment and reports newer or failed edits as unsaved. **Read aloud…** uses the existing local voice reader, bound to this job and exact source. Plain-text edits never realign timed subtitles or become authenticated quotations.
6. Choose the required export formats and **Export results…**, then select a parent directory. The service creates a new subfolder without replacing an existing destination. All four original formats remain retained internally regardless of the chosen export subset; available word subtitles follow their subtitle format. The manifest and separately labelled edits accompany the selected verified files.

Native controls use Tab/Shift+Tab, mnemonics and Ctrl+Tab between pages. Profile, source, settings and destination choices are captured before the shared background operation. Late callbacks cannot publish into a closed dialog or another library/workspace. Unsaved text must be saved or explicitly discarded before switching or closing. Actual NVDA usability remains a separate manual acceptance step.

Folder checks and source retention use the existing bounded scanner: 64 media files, 8 GiB per file and 32 GiB per job, plus directory-entry and depth bounds. Unsupported sidecars are excluded by extension; a supported extension does not certify that the worker can decode its contents. Runtime/model files remain separate from the backed-up research library.

Progress distinguishes measured file counts from verified exported results. Runtime hashing, retention and recognition use different units, so the overall task remains indeterminate rather than inventing one combined percentage. A saved job exposes its real outcome and effective retry settings. Optional observer failure cannot undo a saved result or start another recognition run.

## Durable jobs and restoration

Jobs belong to their original active library. Every operation rechecks that scope. Retained media, durable receipts, results and edit versions live in application state and are included by the existing complete backup. Large jobs require sufficient backup space; retained inputs are not the optional generated-audio cache.

The canonical format-4 backup validates transcription data through its explicit passive adapter. Retained media uses a separate 32 GiB allowance and is included even when generated audio is excluded. Job settings, input hashes, receipt/output hashes, V5 manifests and separately saved edits must agree before a backup or restore is published. Historical source paths and receipt bytes remain unchanged; external interpreter, wrapper and model references are declarations only. Transient locks and stop/pause/live-setting commands are not restored, and no recognition starts automatically.

Unrecognized worker files, caches or result formats need their own reviewed adapter. They cause a visible backup refusal with the member name; they are not silently dropped or treated as arbitrary trusted JSON. A stopped V5 run with its valid typed result manifest can be retained as unaccepted history without promoting its output counters.

Exports and restored completed jobs verify retained bytes and each current-run receipt again. A receipt must agree with the selected worker/interpreter, exact settings, source hash, all requested outputs and timed JSON hash. A worker exit code or an existing same-name TXT alone cannot mark a job completed. Partial or failed exports remain visibly partial or failed.

A restored engine configuration may point to runtime/model locations that are unavailable on the new computer. Inspect and explicitly trust the local profile there, then choose **Rebind runtime…** for the saved job. Rebinding verifies the original worker/model content; it does not reinterpret old results under a different engine. Completed results, edits and export remain readable without the old engine.

A previously running orphan requires **Review interrupted worker…** and explicit confirmation that the old worker has stopped, under an available run lock. Pause, Cancel and Continue do not silently clear this review or a rejected-cache veto. **New job from retained originals…** is the explicit route to a separate ready job when an old workspace cannot safely continue; it does not run automatically or promote rejected output.

Moving an interrupted job can require recognizing its incomplete inputs again because the worker cache identity includes the bound source location. Saved old source paths are provenance, not authority to read arbitrary external files on the new machine.

## Shared command line

The native workspace and command line call LibraryApp.transcription(workspace) and the same service. Run the following with the active library ID from python -m carr_library --list-libraries --data-dir DATA:

- python -m carr_library transcribe --data-dir DATA --workspace ID inspect-runtime --python PYTHON --worker WORKER --models-dir MODELS --protocol receipts-v1
- python -m carr_library transcribe --data-dir DATA --workspace ID register-runtime --name PROFILE_NAME --python PYTHON --worker WORKER --models-dir MODELS --protocol receipts-v1 --trust-runtime --fingerprint EXACT_INSPECTED_FINGERPRINT
- python -m carr_library transcribe --data-dir DATA --workspace ID runtimes
- python -m carr_library transcribe --data-dir DATA --workspace ID submit recording.wav RECORDINGS_DIRECTORY --recursive --profile PROFILE_ID --language auto --model small
- python -m carr_library transcribe --data-dir DATA --workspace ID run JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID pause JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID cancel JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID continue JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID resume JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID acknowledge-interruption JOB_ID --worker-stopped
- python -m carr_library transcribe --data-dir DATA --workspace ID rebind-runtime JOB_ID --profile PROFILE_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID copy-job JOB_ID --profile PROFILE_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID jobs
- python -m carr_library transcribe --data-dir DATA --workspace ID show JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID verify JOB_ID
- python -m carr_library transcribe --data-dir DATA --workspace ID read JOB_ID --source RETAINED_FILENAME
- python -m carr_library transcribe --data-dir DATA --workspace ID save-edit JOB_ID --source RETAINED_FILENAME --text-file UTF8_FILE
- python -m carr_library transcribe --data-dir DATA --workspace ID export JOB_ID NEW_DIRECTORY --format txt --format json

Submission alone retains inputs without inference. Use the explicit --run option on submit, or run/resume separately, to execute. Read accepts --edited for the latest separate revision; --source can be omitted only for a single-source job. Export accepts either its existing positional new directory or --output, never both. Repeated --format values are deduplicated. Original retained formats/settings are unchanged.

Existing configure, create, create-folder and edit commands remain available. configure explicitly selects the legacy/current local runtime; it is not invoked by inspection or registration. create accepts files; create-folder retains one selected folder recursively for compatibility and reports retained/excluded counts. Both accept an explicit --profile. The new mixed-source submit command recurses only with --recursive.

Recognition options include --protocol, --model, --language, --device, --compute-type, --beam-size, --parallel-files, --multilingual, --no-vad, --no-context, --no-word-timestamps, --include-headers and --max-attempts, subject to the selected protocol. An exact --model-revision commit is supported only by receipts-v1; V5 binds its installed model directory by content. Unsupported speech translation is rejected before running. Runtime profiles remain explicitly unqualified for acoustic/language quality.

An executed stopped/paused/cancelled job returns exit status 2, failed execution returns 1, and completed execution returns 0. Ctrl+C returns 130 and reports the saved job ID when available. Control requests, inspection and saved-result reads do not execute inference.

## Supplied V5 profile

The profile adapts the supplied result_manifest.json into the same retained jobs, verified receipts, editing, export and backup service. It does not install or replace the recognizer. Select a complete CTranslate2 model folder containing model.bin, config.json and tokenizer.json, or a parent containing the selected model-name folder. Links and ambiguous remote/cache selection are rejected; no model is downloaded.

Configuration binds the interpreter, worker.py and both helper files by SHA256. Launch and result admission recheck those identities. Each run records the exact local model files, retained input identities and requested settings. It uses a fresh results/runs directory with a host run ID, while the supplied worker's logs/cache_v5 directory stays in the existing job for explicit resume. An earlier run's manifest or a same-name TXT cannot complete a new run.

The upstream profile at [c669fe93](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/c669fe93a21cfc73cc42f7a211d84d8bf9170b8d) records a separate controlled-backend interoperability check using these supplied sources. This is upstream evidence, not a run of the current canonical composition. Configuration records the identities of the files actually selected and does not download them:

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

The upstream record reports execution of the supplied repaired V5 worker and both helpers with a controlled backend and dummy model files, outside public Git. It describes protocol interoperability, actual wrapper exports, same-language retry and cache-preserving Stop/resume. That historical check does not establish execution of this canonical composition or Whisper acoustic inference. Runtime configuration and these protocol tests do not mean that model quality, mixed-language alignment, diarization, owner cloning or Windows11/NVDA acceptance has passed. Recognition requires a compatible offline faster-whisper/CTranslate2 installation and complete local models. No automatic replacement recognizer or remote fallback is selected.

The launch uses isolated Python startup, the existing site-path bootstrap, a minimal environment, offline library settings, blocked Python network/child-command events and Python writes confined to the job. This is not an operating-system sandbox for arbitrary native libraries. Only a reviewed explicitly selected local runtime should be configured.


## Composition verification

The converged frontend reuses the canonical e69 native action layout while retaining the current workspace-bound flat service, V5 admission, source/receipt verification, selected virtual-environment identity and existing shared resource/progress ownership.

The required portable selections are tests/ui/test_transcription_dialog.py and tests/e2e/test_transcription_cli.py, alongside the existing supplied-worker, V5, backup and resource-admission suites. They include actual isolated synthetic process execution, exact original/edited separation, export subsets, scope changes, queued close, immutable resume choices, trust fingerprint binding and completion-aware Save All.

Run scripts/windows_transcription_smoke.py under the admitted source/installed wx environment for actual named controls, both protocol selections, explicit trust, recursive folder retention before inference, durable pause/cancel/resume, six original outputs, Undo/Redo/separate save, subset export after engine removal and normal desktop cleanup. Its picker paths are controlled synthetic answers; manual picker interaction, real-model acoustic inference, hardware coverage and NVDA are not claimed. New composition execution must be recorded by the current CI head; older component passes are not transferred.
