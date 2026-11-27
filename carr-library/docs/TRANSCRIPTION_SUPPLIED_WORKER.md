# Supplied transcription worker adapter

This increment adapts the actual supplied Whisper V4.3 worker to the canonical
Resonance application. It does not import PR136's alternative in-process engine,
audio job store or model registry. `audio/transcription.py` uses the existing
`durable_state`, `audio.process`, application data directory and state backup.
The supplied source, recordings, model files and private installation paths are
not part of this repository.

## Native desktop workflow

Open **Audio Studio → Local transcription**. This is a view over the same
supplied-worker service and state used by the CLI.

The first native notebook page selects a reviewed local runtime manifest,
checks its complete installation inventory, selects files or adds the **direct
contents of a folder**, and selects the recognition language, transcription or
English translation task, explicit device/compute type and TXT/SRT/VTT/JSON.
Folder scanning never recurses. It admits at most 1000 selected files, inspects
at most 20000 directory entries, supports cancellation and applies the existing
job input byte allowance. Selection does not start inference. No runtime or
model is downloaded or installed by this window.

**Verify and connect local dependencies** saves an explicitly selected local
connection only after verification. **Disable saved connection** stops future use
while preserving jobs, outputs and editable reading copies. A restored connection
is disabled until reviewed again; opening the dialog does not launch inference.

The second page lists saved jobs and completed results. Refresh, dependency
checks, input retention, inference and receipt checks all use the existing
desktop background executor. **F6** starts or resumes; **F8** requests pause;
**Ctrl+Alt+G** focuses the readable status. Cancel stops an active operation.
Closing while busy asks the user to wait for the saved terminal status.
Progress is indeterminate during an active file; no percentage or ETA is
invented. Resuming retains the job's original settings and verified runtime
identity. An interrupted active file is restarted; completed verified files are
reused.

**Edit TXT reading copy** opens the existing reader, with a distinct recovery
slot for the job and audio input. The first opening seeds an independent draft;
later openings recover the user's text, undo history, voice settings and cursor.
The draft has no library-source or quotation binding. Deleting or modifying a
generated transcript does not make a saved user draft inaccessible. Initial
draft publication uses the reader store's locked save-if-absent operation, so a
concurrent saved edit wins. Generated transcripts are never edited in place by
this action.

Damaged generated outputs are shown as **invalid / needs review**, preserved,
and cannot be presented as verified results or reused for recognition. The
native result-folder action checks a local directory and invokes the established
wx system launcher. Existing user drafts remain independently recoverable;
creating a new draft requires a verified TXT output. Opening a local edited
text file for a separate reading copy remains available in the existing reader.

The new controller regression file is
`tests/ui/test_transcription_dialog.py`; the real wx probe is
`tests/ui/native_transcription_smoke.py`. The latter runs the authored synthetic
supplied-worker fixture and checks native event handlers, shared-executor
completion, pause/cancel/resume, and reader handoff/reopening. It explicitly
reports real-model quality and NVDA as NOT_RUN. Source/controller tests, native
Windows execution, real inference, human NVDA and distribution rights remain
separate evidence; the presence of this probe is not a PASS result.

## Create a local draft runtime manifest

The existing transcription CLI can inspect an already installed local worker,
Python interpreter, runtime package directory and Whisper model directory into
the same manifest schema used by the adapter. This removes the need to type
complete file hashes by hand. For example, with your actual local paths:

```powershell
python -m carr_library transcribe draft-runtime --python "C:\LocalASR\python.exe" --worker "C:\LocalASR\worker.py" --runtime-directory "C:\LocalASR\site-packages" --models-directory "C:\LocalASR\models" --model small --output "C:\LocalASR\runtime-draft.json"
```

All paths are explicit selections. The worker must still match the pinned
supplied source; use its unmodified original, not a prepared copy. The helper
reads files and prepares source text without executing the worker, interpreter,
runtime imports or model. It rejects links/reparse points, standalone bytecode,
changed files, incomplete required inventories and over-budget inspections.
Canonical runtime cache directories are excluded using the same policy as the
adapter. Default bounds are 100000 files, 150000 directory entries and 64 GiB
of selected file bytes. Cancellation publishes no partial manifest.

The output must be outside the selected runtime and model trees. It is created
atomically under a new filename; an existing file or concurrent destination is
preserved. No application database or transcription job is created by this
inspection command.

Both `runtime_reviewed` and `rights_reviewed` are always **false** in a generated
draft. The adapter rejects it until a separate review of the exact code, native
dependencies and model rights is recorded. Hash collection is not an approval,
a model download, a runtime import test, a quality test or a distribution grant.
The helper is implemented in `audio/supplied_runtime_manifest.py` and covered by
`tests/audio/test_supplied_runtime_manifest.py`.

## Implemented boundary

The adapter accepts two separately pinned supplied sources. The original V4.3
source uses the strict preparation below. The separately supplied hardened worker
has SHA256 `5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c`
and protocol `carr-asr-v43-hardened-1`; its source bytes remain unchanged. For that
protocol, the host additionally verifies the terminal run, retained recognition
receipt, model/runtime declarations, actual inference device/compute type and
every requested artifact against this exact job. Both routes use the same job
store, source retention, resource admission and output publication service.

- The original worker must match SHA256
  `1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`.
  `audio/supplied_whisper.py` reuses PR135's exact-source strict-settings change
  corrects subtitle millisecond carry and fixes deterministic text newlines on
  Windows. In the exercised CPython runtime its
  prepared source SHA256 is
  `597ad8182adc99a826798aa530fa792784679a5c2755c62a27c9bf0cf55dc2a6`.
  This helper does not rewrite the owner-supplied model or inference engine.
- A reviewed local manifest identifies the Python executable, supplied source,
  complete runtime and model inventories, model directory and separate rights
  review. The desktop process never imports faster-whisper or downloads models.
- The selected interpreter runs with `-I -S -B -X utf8`, a fresh private bytecode prefix,
  and only the explicit verified runtime package directory added to its path.
  Executable `.pth` and site customization are not loaded. Standalone unreviewed
  bytecode is rejected; cached bytecode beside source cannot substitute for the
  verified source. Python network/child-command audit denial and offline flags
  are defense in depth, **not native process network isolation**.
- Each selected file is retained by content hash, size and format under the
  existing application state tree. Same basenames do not collide. Changing
  bytes with unchanged size/mtime, recognition settings or model/runtime identity
  cannot reuse a previous result. Renaming an identical selected file can reuse
  its existing verified job.
- One supplied-worker subprocess handles each remaining file, with fixed
  settings, parallelism one and a new private job directory. This prevents its
  old basename adoption and path/mtime cache from authorizing results. It also
  prevents live settings from resetting an in-flight model. The current adapter
  reloads the model between files; persistent batch model reuse and measured
  book-length throughput are not qualified by this increment.
  The cooperating runtime lock is scoped to this application state directory and
  selected runtime identity. Shared cooperative resource admission additionally
  coordinates this canonical service, studio production and isolated imports.
  It reserves declared CPU/RAM/disk and configured device allowances; it does
  not measure model peaks or enforce OS-wide limits on other engines. A pending
  native cleanup keeps the same resource reservation, runtime/job locks and
  private staging until the process owner confirms termination. See
  `runtime_resources.md` for the participating boundaries and policy.
- The host checks actual worker done/failed counts, selected language/settings,
  source identity, timestamp order and every requested export. Worker exit zero
  or its textual completion status alone cannot pass. TXT/SRT/VTT and a bounded
  JSON provenance record are assembled with a completion receipt and published
  together by a directory rename. Failed/cancelled publication leaves no partial
  final per-file result. Existing edited results remain intact and block reuse.
- File-level checkpoints survive later failure or restart. Pause/cancel kills
  the active worker through the existing subprocess runner. Resume restarts that
  file; it does not yet resume within a recognition segment. Completed files
  are reverified and reused. Progress observer or post-commit cleanup failures
  do not change successful completed work into failed work.
- Runtime identities include the exact public adapter/supervisor files as well
  as the selected worker, executable and inventories. Updating that implementation
  invalidates incompatible inference reuse; previous transcripts and reading
  drafts remain preserved. Resume requires the matching reviewed identity.
- Reading `status` revalidates completed receipts and artifact bytes. Missing or
  modified results cannot be reported as a valid completed job. Status, saved
  jobs and pause/cancel commands do not require a working runtime installation.

Whisper's translate task targets English. This adapter does not implement speaker
diarization or claim word alignment. A valid silent result may contain empty text.
Unsupported multilingual settings fail without falling back to another language
or silently removing that setting.

## Text-accessible commands

The installed application entry point routes to the same service:

```powershell
research-library transcribe --data-dir "D:\Research" run --runtime-manifest "D:\Engines\whisper-runtime.json" --input "D:\Audio\chapter.wav" --language uk
research-library transcribe --data-dir "D:\Research" jobs
research-library transcribe --data-dir "D:\Research" status JOB_ID
research-library transcribe --data-dir "D:\Research" pause JOB_ID
research-library transcribe --data-dir "D:\Research" cancel JOB_ID
research-library transcribe --data-dir "D:\Research" run --runtime-manifest "D:\Engines\whisper-runtime.json" --resume JOB_ID
```

Repeat `--input` for a batch. `--queue-only` retains the selected input bytes and
creates a durable queued job without inference. Repeat `--format` to choose
TXT/SRT/VTT/JSON; otherwise all four are requested. The command prints the saved
job identity and output directory. Resume uses the saved settings. These jobs
are also available in the native desktop workflow described above. Native
Windows execution and human NVDA acceptance have separate evidence; a passing
console test does not establish either of them.

## Local installation manifest

The manifest sits outside the model and runtime inventories and uses schema 1,
engine `supplied-whisper-v43`. Its exact fields are:

| Field | Meaning |
| --- | --- |
| `python`, `python_sha256` | Explicit installed interpreter path and verified executable bytes |
| `worker` | Original supplied worker; its source SHA is pinned by the application |
| `runtime_directory`, `runtime_files` | Explicit package directory and complete relative-path to SHA256 inventory, excluding `__pycache__` |
| `models_directory`, `model`, `model_files` | Existing model parent, simple child name and complete SHA256 inventory of that child |
| `rights_reviewed`, `runtime_reviewed`, `rights_notice` | Separate explicit local-use review, both booleans true, and its recorded notice |

The inventories require faster_whisper, ctranslate2, av, onnxruntime, numpy,
huggingface_hub and tokenizers package entry points, and local `model.bin`,
`config.json`, `tokenizer.json`. These requirements and hashes identify selected
files; they do not qualify every native dependency, supported device, model
license, Python compatibility or recognition language. An actual reviewed runtime
pack is still needed. Inventories require materialized regular local files;
Hugging Face cache symlinks are not silently adopted or copied. No installer
silently declares these review flags or copies
the owner's virtual environment as a portable runtime.

## Backup and recovery

Jobs live under `state/transcription`. Existing typed state backups include their
retained input audio, request/settings, control state, completed transcripts and
receipts. Existing `.pending-*` and lock exclusions apply to temporary active
worker staging. A changing active job makes the backup fail for a safe retry.
Runtime and model installations remain external; every job explicitly records
that they are not included. Restore the state into a new destination and select
the same reviewed installation identity before resuming. No restored job starts
inference or a network operation automatically. Credentials are not used here.

## Evidence and open acceptance

`tests/audio/test_hardened_transcription_protocol.py` includes an authored
protocol peer launched through the actual hardened bootstrap and selected
interpreter. It spawns a real multiprocessing child, imports only its inventoried
fixture dependency, verifies isolated UTF-8/no-bytecode flags and rejection of
an Internet-socket attempt, then commits all requested formats through the
normal receipt checks. This qualifies process/protocol integration only; it
contains no private worker code, performs no acoustic inference and establishes
neither real model quality nor native network containment.

`tests/audio/test_supplied_transcription.py` executes actual isolated Python child
processes with authored synthetic worker/model fixtures. It covers cache and
source identity, language/settings preservation, same basenames, required output
failure, runtime/model mutation, pause/cancel, atomic publication and recovery,
backup of queued audio, bytecode substitution, network audit denial, completion
observers and cleanup failure. This is control-flow/filesystem/process evidence,
not recognition accuracy.

The actual private supplied source was separately prepared and executed in a real
subprocess with an authored synthetic `WhisperModel`: TXT/SRT/VTT/JSON, corrected
minute-boundary subtitles and reusable completion passed. The preparation check
retains PR135's baseline comparison: baseline 1 pass/2 fail, candidate 3 pass.
Private source and synthetic model outputs are not published.

Native Windows results are recorded by the checks for the exact head of
[PR143](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143). The native
probe still uses synthetic recognition. Real faster-whisper inference, approved
runtime/model pack closure, actual offline native process isolation, supported
language quality and human NVDA remain separate, unqualified requirements.
This adapter does not complete the full ASR or platform release gate.
