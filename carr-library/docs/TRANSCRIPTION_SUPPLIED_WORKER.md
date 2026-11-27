# Supplied transcription worker integration

Resonance uses one local transcription service, `audio/transcription.py`, for the
native dialog, console commands, saved jobs and typed backups. The integration
reuses the public PR143 service and native controls and adapts the separately
supplied, exact V4.3 worker. The application process does not import
faster-whisper or download a model. Private worker source, weights, recordings and
installation paths are not published in this repository.

## Implemented workflow

Open **Tools → Local audio and video transcription**. Choose a reviewed local
runtime manifest and use **Verify and connect local dependencies**. File and
direct-folder selection, recognition settings, queueing, run/resume, pause,
cancel, saved-job inspection, result access and a separate editable reading copy
use the same service. Selecting a folder performs a bounded, non-recursive scan.
Choosing a runtime or opening saved jobs does not start recognition.

The library-scoped `transcription-runtime.json` declaration is off by default.
Verification runs on the background worker before saving an enabled connection.
**Disable saved connection** clears that authority without deleting retained
inputs, transcripts or jobs. A restored declaration is always disabled. The
native connection controls do not require environment-variable edits.

TXT, SRT, VTT and JSON can be selected together. The JSON records measured
segment timings and provenance. Whisper's translate task targets English;
general document translation uses the application's separate translation
workflow. This adapter does not offer diarization or word timestamps.

Reading-copy edits use the existing `ReaderDraftStore`, including atomic
`save_if_absent`. Reopening a transcript does not overwrite an existing edited
draft. The derived copy is distinct from immutable source audio and generated
transcript receipts; it does not become original quotation evidence. Reader
save/export operations retain their own destination and overwrite controls.

## Exact supported source and protocol

The preferred supplied source has SHA256:

`5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c`

It declares protocol `carr-asr-v43-hardened-1`.
`audio/supplied_whisper.py` accepts those exact bytes unchanged. The public
bootstrap loads that source privately; it contains no replacement recognition
engine. The separately supplied output-name archive is an older source lineage
and is not substituted for this worker.

Compatibility with original source
`1df2a1694b35bdf69dab614f91f1e8dea99716dfb039182ef5b83ca5ccb46ec9`
remains through the existing exact-source preparation. That path applies the
reviewed strict-setting, subtitle-carry and deterministic-newline fixes. Its
prepared source identity is calculated for the actual interpreter; a hash from
one AST/unparse implementation is not asserted for every Python build.

The manifest records the selected interpreter checksum, full SDK and model
inventories, exact worker and a separate local-use review. Admission and cache
identity include the public adapter dependencies, process runner and Windows job
implementation. Changed source, interpreter, SDK, model, recognition settings or
selected input bytes cannot silently reuse another recognition result.

## Process, result and recovery boundaries

The selected interpreter runs with `-I -S -B`, UTF-8 mode and a private bytecode
prefix. Only the inventoried SDK directory is added explicitly. Site
customization and executable `.pth` files are not loaded. Loose standalone
bytecode, links and undeclared installation files are rejected.

The hardened worker uses a spawned inference child. Its public bootstrap
reinstalls Python audit denial before that child loads the reviewed worker:
internet sockets, name resolution and arbitrary child commands are denied;
local multiprocessing pipes remain available. This policy is defense in depth,
not proof that every native library is network-isolated.

Windows process ownership is established at creation with a Job Object and an
explicit standard-handle list. The runner checks termination, root exit and the job's active-process count before
releasing its last handle. A failed native call retains the job for bounded daemon
retries and reports `AudioCleanupPending`. The same owner retains the transcription
job/runtime locks and private staging until confirmed quiescence; it never blocks
a ThreadPoolExecutor worker indefinitely. POSIX uses a new process group.
Cancellation, time, diagnostic and staging-byte budgets apply to the owned
operation. The current adapter starts a fresh worker/model for each remaining
input file; persistent model reuse across a long batch is not claimed.

Each selected audio/video file is retained by SHA256, size and format. Same
basenames do not collide. A completed file is accepted only when the terminal
run identity and counts, retained source, model/runtime receipt, selected
settings, actual device/compute choice and every requested artifact agree.
`audio/transcription_protocol.py` checks the hardened recognition and artifact
receipts. Process exit zero alone is insufficient.

TXT/SRT/VTT bytes are checked against the accepted segment result. The host
writes bounded normalized JSON and publishes the complete per-file directory
with a receipt. Failed or cancelled attempts leave no partial final transcript.
If the worker reports a real inference error, a bounded terminal explanation is
preserved before staging cleanup. No failed receipt becomes reusable work.

Pause/cancel interrupts the active owned process. Resume restarts an incomplete
file; it does not resume inside a recognition segment. Accepted earlier files
are verified and reused. A late progress-observer failure does not undo a
durably completed result. Saved jobs and result inspection do not need a working
model installation.

## Console access and installation manifest

The installed entry point routes to the same service:

```powershell
research-library transcribe --data-dir "D:\Research" run --runtime-manifest "D:\Engines\whisper-runtime.json" --input "D:\Audio\chapter.wav" --language uk
research-library transcribe --data-dir "D:\Research" jobs
research-library transcribe --data-dir "D:\Research" status JOB_ID
research-library transcribe --data-dir "D:\Research" pause JOB_ID
research-library transcribe --data-dir "D:\Research" cancel JOB_ID
research-library transcribe --data-dir "D:\Research" run --runtime-manifest "D:\Engines\whisper-runtime.json" --resume JOB_ID
```

Repeat `--input` for a batch and `--format` to choose exports. `--queue-only`
retains selected input bytes without inference. The `draft-runtime` command
inventories explicit local files without executing them and writes an
**unapproved** manifest; it does not grant runtime or redistribution rights.

The schema-1 `supplied-whisper-v43` manifest requires:

| Fields | Meaning |
| --- | --- |
| `python`, `python_sha256` | Explicit interpreter path and exact executable checksum |
| `worker` | Exact supported supplied source |
| `runtime_directory`, `runtime_files` | Complete bounded SDK inventory, excluding source caches |
| `models_directory`, `model`, `model_files` | Explicit model parent, simple child name and complete file inventory |
| `runtime_reviewed`, `rights_reviewed`, `rights_notice` | Separate review declarations and recorded local-use basis |

The inventory requires the seven runtime package entry points and the local
model's `model.bin`, `config.json` and `tokenizer.json`. An inventory identifies
bytes; it does not establish native dependency compatibility or language quality.

## Backup

Full typed state backups include `state/transcription`, immutable retained
inputs, saved requests/settings, accepted transcript bytes and receipts,
including valid receipts left by a crash before the job checkpoint advanced.
The producer's `validate_saved_job`, `validate_saved_snapshot` and
`restored_job` functions are the shared schema authority.

Restore pauses queued/running jobs and requires explicit review for every
restored job. Saved runtime declarations are disabled without probing their
external paths. Workspace-scoped backups omit this independent namespace.
Runtime and model installations remain external. Restored jobs do not start
processes or downloads. Resuming requires the reviewed runtime identity bound to
the saved request; a different installation requires a newly selected job.

## Observed evidence, 2026-11-27

The current Linux strict source checks completed **126 passed, zero skipped or
failed** across these six suites:

| Suite | Passed |
| --- | ---: |
| `tests/audio/test_supplied_transcription.py` | 54 |
| `tests/audio/test_supplied_runtime_manifest.py` | 15 |
| `tests/ui/test_transcription_dialog.py` | 29 |
| `tests/audio/test_owned_process_tree.py` | 5 |
| `tests/audio/test_transcription_settings.py` | 8 |
| `tests/audio/test_hardened_transcription_protocol.py` | 15 |

These include actual isolated child processes and authored synthetic protocol
fixtures. The hardened parser tests contain no private worker source. The
transcription suite includes preservation of both admission locks and staging
across an uncertain native shutdown. The separate cleanup suite passed **14** cases covering actual
owned stdin/Stop, reader-thread admission failures, deferred lock release and
injected native API failures. The
separate typed-backup owner observed **16 passed** for the composed backup
contract; that count is a separate gate, not added to the table above.

A private qualification also ran the actual unchanged 5379 source with the
actual tiny.en model and native faster-whisper stack on Linux. The sanitized
record is
[`transcription-supplied-5379-linux-20261127.json`](evidence/transcription-supplied-5379-linux-20261127.json).

The model checksum was
`1a5afae06a4db91c975c9a9d78be5cc110ee4ea022ad57d55492e4550e936b2a`.
The 7.69-second authored synthetic English fixture produced nonempty TXT, SRT,
VTT and JSON, one accepted timed segment, and a reusable receipt without a
second worker. Explicitly asking that English-only model for Ukrainian failed
with the real language-substitution explanation. The original audio was
unchanged.

The measured environment used Python 3.12.14, faster-whisper 1.2.1,
CTranslate2 4.9.1, PyAV 16.0.1, NumPy 2.5.5, ONNX Runtime 1.32.0,
huggingface_hub 2.5.0 and tokenizers 0.23.3. Retention, verification and inference
together took about 13.58 seconds; this is not a pure inference benchmark.
The command ran in a Linux user/network namespace. Interface enumeration was
unavailable, so the report does not invent an observed interface list.
The report binds the actual adapter hash used for that measured run.

A separate Windows candidate has a checked **35-wheel Windows x64/CPython 3.11
metadata closure**, totaling 93,118,349 bytes. Thirty-one wheel hashes match the
application's existing pinned Windows dependency set. The four additional exact
inputs are faster-whisper1.2.1, CTranslate2 4.8.0, PyAV 17.1.0 and
setuptools 80.9.0. This is packaging evidence only. It uses different SDK versions
from the measured Linux environment and must receive its own native
qualification.

## Acceptance still pending

- Actual execution of this composed native Windows dialog/probe and owned
  process/pipe behavior on Windows. The probe checks installed module origins
  and uses explicit synthetic fixtures; it is separate from production-only
  startup checks.
- Final private Windows companion assembly and a real supplied-worker/model
  run using that exact interpreter and dependency closure.
- Physical or long-form recordings, manual listening and sustained throughput.
- Ukrainian or other multilingual model inference and accuracy. The tiny.en
  result does not qualify them.
- Human NVDA and full accessibility acceptance.
- GPU execution, diarization, word timing and public redistribution approval.

The native controls and public adapter are implemented. These remaining
execution and quality checks are not represented as completed release gates.

