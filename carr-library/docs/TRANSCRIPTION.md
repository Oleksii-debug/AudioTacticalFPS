# Local transcription

Open **Tools → Local audio transcription**. Select an existing local Python
interpreter, the reviewed private worker and the parent directory of already
installed CTranslate2 models. The application does not distribute the private
worker, install models, or download model files in this flow. Without that
explicit profile it displays **waiting-input**.

The reviewed worker entrypoint for this version has SHA-256
`5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c`.
Its digest admission is distinct from qualification of the selected Python
runtime, dependencies, model, language and device. The profile requires regular
local files/directories, without symbolic links, Windows reparse points or
hardlinked files. A Linux venv may need an actual interpreter binary inside its
`bin` directory (`venv --copies`), preserving its own `pyvenv.cfg` and packages.

## Select and run

Choose explicit local audio files, a local model directory name, source language
(`auto` or an explicit code), recognition or English translation, compute settings
and the required outputs. `tiny.en` is English-only; another source language
requires a corresponding installed multilingual model. The engine processes one
file at a time. An English translation target does not qualify other language
pairs or establish translation quality.

Run creates private, content-addressed input snapshots under
`state/transcription/jobs/<id>/inputs`. Selected originals remain unchanged. The
limits are 128 files, 2 GiB per file and 8 GiB per job. Selecting the same content
twice is rejected before the job is published. All requested TXT, SRT, VTT and
JSON outputs must be present and byte-verified before success is announced.
Per-file warnings, including any model timestamp clamp, remain in the receipt
and visible job status. A valid worker protocol alone is not acoustic-quality
qualification.

**Pause** stops the incomplete recognition unit; continuing restarts that file
from its beginning while retaining completed work. **Cancel**, closing the dialog
or the desktop's shared cancellation signal requests `stop.flag` and waits for
the owned worker to exit. A nonresponsive worker and its children are stopped
after a bounded grace interval. Each run has a 24-hour wall-time limit, including
pauses. A later Run explicitly resumes the saved job. Completed output files
remain on disk even when a run stops before it can publish a terminal receipt;
unverified files are not presented as completed transcripts.

The application uses its existing background-job and OperationProgress controls.
Copying source inputs is a distinct phase. Terminal completion requires the child
process to exit successfully, matching current execution/job identities and
verified source/output bytes. A stale status or missing final receipt cannot
reuse an earlier success.

## Read, edit and export

Choose a verified output in the transcript list. TXT edits are saved as a new
**derived revision** with an explicit link to the original output digest. The
worker output and original audio are not replaced. Export writes a new file;
existing destinations are rejected. Larger transcripts can be exported even
when they exceed the two-million-character editor limit.

## Backup and restore

The ordinary complete-library backup carries managed input snapshots, durable
job manifests, transcript outputs and saved revisions through the existing state
backup path. The private worker's temporary executable copy is physically
outside the backed-up library, including during an active job. Python, native
libraries and model files are external runtime inputs, not automatic backup
attachments. As with other changing state, a backup detecting concurrent writes
fails clearly and can be retried after the operation settles.

After a library is moved or restored, the old engine profile becomes
**waiting-input**. Select the current reviewed runtime and model locations again.
Only an explicit Run can execute a job; its configuration is regenerated with
fresh absolute paths to the restored managed inputs and results. Saved output
receipts are revalidated as a complete source/output graph before accepted use.

## Verification

`tests/transcription/test_transcription.py` executes clearly labelled synthetic
worker processes for protocol, cancellation, artifact integrity, source snapshot,
backup/restore and revision boundaries. `tests/transcription/test_native_handlers.py`
executes the actual dialog handlers with native-widget stand-ins and the real
shared progress tracker. These tests do not claim model recognition accuracy.

`scripts/windows_transcription_smoke.py` separately requires an installed Windows
package and actual wx Windows controls. It exercises the native dialog, shared
background execution, synthetic process receipt, verified outputs, readable
warnings, editable revision and export. It explicitly reports manual keyboard,
native picker and NVDA acceptance as not run. Actual-model inference is a
separate qualification and must name the worker, runtime, model and device.

## Command line

The installed `transcription` command group uses this same service and the
application's saved data-directory selection. An explicit `--data-dir` follows
`transcription`, before the operation. For example:

```text
python -m carr_library transcription --data-dir LIBRARY capability
python -m carr_library transcription --data-dir LIBRARY configure --python PYTHON_EXE --worker REVIEWED_WORKER.py --worker-sha256 5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c --models-dir LOCAL_MODELS
python -m carr_library transcription --data-dir LIBRARY submit audio.wav --language en --title "Interview"
python -m carr_library transcription --data-dir LIBRARY jobs
python -m carr_library transcription --data-dir LIBRARY run JOB_ID
python -m carr_library transcription --data-dir LIBRARY status JOB_ID
python -m carr_library transcription --data-dir LIBRARY pause JOB_ID
python -m carr_library transcription --data-dir LIBRARY continue JOB_ID
python -m carr_library transcription --data-dir LIBRARY edit JOB_ID --artifact results/TRANSCRIPT.txt --text-file corrected.txt
python -m carr_library transcription --data-dir LIBRARY export JOB_ID --artifact results/TRANSCRIPT.txt --revision REVISION_ID --output new-transcript.txt
```

`submit` creates a queued job without running an engine. Choose individual
`--model`, `--language`, `--device`, `--compute-type`, `--task` and repeated
`--format` flags, or a `--settings` JSON file containing the exact
`TranscriptionSettings` fields. These two settings mechanisms cannot be mixed.
The JSON route also exposes beam size, voice activity detection, context,
multilingual decoding, TXT grouping and headers. Unknown settings are rejected.

Output is JSON on stdout; measured progress and errors use stderr. Successful
operations return 0. Failed or partial runs return 1, including valid terminal
worker failure receipts. Ctrl+C requests the same bounded cancellation as the
native UI and returns 130 after retaining the saved job and completed files.
Run that job ID explicitly to resume. `pause` and `continue` acknowledge the
requested flag change; the worker's response is separate. `status` revalidates
saved receipts and is cancellable. Existing files are never overwritten by
export. Syntax, settings and explicit source-path argument errors are rejected
before initializing a new library.
