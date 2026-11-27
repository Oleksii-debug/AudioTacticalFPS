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

Choose explicit local audio files or **Choose audio folder**, a local model directory name, source language
(`auto` or an explicit code), recognition or English translation, compute settings
and the required outputs. `tiny.en` is English-only; another source language
requires a corresponding installed multilingual model. The engine processes one
file at a time. An English translation target does not qualify other language
pairs or establish translation quality.

Folder selection runs in the shared background job runner and shows a finite,
sorted list before Run. **Include subfolders when choosing a folder** is off by
default. Unsupported extensions, links/reparse points, hard links and excluded
subfolders are reported in status. Nothing is recognized or copied during this
review. The scan is bounded to 20,000 directory entries and 64 directory levels;
exceeding a scan, file-count or byte budget rejects that whole new selection.
Cancel keeps the previous selection. Files added later are not included: choose
the folder again to review a fresh list. Submitting it uses the ordinary immutable
input snapshots, receipts, cancellation and resume behavior described below.

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

On Windows, running progress remains indeterminate until the owned worker exits.
The adapter does not open the live status file: the unchanged reviewed worker
uses ordinary atomic replacement, and Windows cannot replace that destination
while a reader holds it open, even with delete sharing. **Pause requested** and
**Stopping** describe pending requests, not confirmed worker states. Per-file
completion counts and results become available after exit and full verification.
On POSIX systems the adapter can also display live worker phases and counts.
This platform limit does not relax final job/run identity, exit-code, source-hash
or output-hash checks. The Windows behavior follows Microsoft's documented
[open-target rename rules](https://learn.microsoft.com/en-us/windows-hardware/drivers/ddi/ntifs/ns-ntifs-_file_rename_information).

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
attachments. A backup refuses an active transcription operation without reading
its live outputs or stopping its worker. Wait for completion or stop the job;
pausing still leaves the operation active. Backup retains the existing job
admission and run locks through the full state snapshot. Queued jobs, completed
jobs and interrupted jobs with no live owner remain eligible for backup. Other
changing state still causes a clear retryable failure.

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
warnings, editable revision and export. Native Space activates the folder and Run
buttons; a deterministic test-only picker supplies a synthetic folder to the
actual background selection handler. It explicitly reports manual keyboard,
native picker and NVDA acceptance as not run. Actual-model inference is a
separate qualification and must name the worker, runtime, model and device.

The [actual two-file folder execution record](evidence/transcription-folder-310e6e7-20261127.json)
binds source commit `310e6e7cffc9e91949b4c76248842d151c2a30f4` and the hashes of
the service, CLI, shared selection utility and native dialog. With the unchanged
reviewed worker and pinned `tiny.en`, 69 assertions passed: the CLI submitted two
short synthetic English recordings from one folder, disclosed an unsupported
entry, then recognized both retained inputs after the original folder was
deleted. All eight requested outputs were verified. A completed Run revalidated
the same result without replacing the worker receipt. Execution used the exact
31-distribution replay freeze inside a Linux network namespace without an
external route. This is functional English CPU evidence; it does not establish
native Windows/NVDA behavior or general recognition quality. The earlier
82-assertion interruption and destructive-restore record remains separate.

The [composed folder and completed-backup replay](evidence/transcription-folder-recovery-646a15e-20261127.json)
binds source commit `646a15e38bf7b56349632011e28e7e1b65e9bd3e` and seven
service/selection/UI/backup module hashes. All 107 assertions passed with the
unchanged reviewed worker, all five pinned model components, the exact
31-distribution runtime freeze and a successful `pip check`. The original
two-file folder scenario was repeated inside a Linux user/network namespace
with only loopback and an unreachable external route. A full v4 backup retained
both input snapshots and all eight verified outputs; after deleting the original
test library and selected source folder, restore to another location required
explicit engine selection. The same completed receipt then revalidated without
replacing the worker configuration or result. Catalog originals also survived.

A second actual two-file inference passed with the no-live-progress policy
explicitly forced in the Linux test host. It reported indeterminate progress
and accepted all eight outputs through the unchanged strict terminal checks.
This exercises the policy with the supplied worker; native Windows file-sharing
behavior still requires the Windows gate. The public record contains hashes and
sanitized facts, without private source, paths, recordings, transcripts or model
weights. Earlier 69- and 82-assertion records remain unchanged.

## Command line

The installed `transcription` command group uses this same service and the
application's saved data-directory selection. An explicit `--data-dir` follows
`transcription`, before the operation. For example:

```text
python -m carr_library transcription --data-dir LIBRARY capability
python -m carr_library transcription --data-dir LIBRARY configure --python PYTHON_EXE --worker REVIEWED_WORKER.py --worker-sha256 5379b889b8019c7ad9f427a7bc8db268dccd747469abc72db4f3001f9f82306c --models-dir LOCAL_MODELS
python -m carr_library transcription --data-dir LIBRARY submit audio.wav --language en --title "Interview"
python -m carr_library transcription --data-dir LIBRARY submit --folder AUDIO_FOLDER --language en
python -m carr_library transcription --data-dir LIBRARY submit --folder AUDIO_FOLDER --recursive --title "Reviewed folder batch"
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

`--folder` can be repeated and combined with explicit files. Explicit file order
is preserved, followed by folders and their entries in stable name order.
`--recursive` applies only to the selected folders and is off by default.
Overlapping folder selections include each path once; distinct files containing
identical bytes are still rejected by the existing job admission policy.
Skipped entries are disclosed on stderr (up to 100 details plus the total).
The worker receives only retained file snapshots, never permission to discover
more files in the selected folders.

Output is JSON on stdout; measured progress and errors use stderr. Successful
operations return 0. Failed or partial runs return 1, including valid terminal
worker failure receipts. Ctrl+C requests the same bounded cancellation as the
native UI and returns 130 after retaining the saved job and completed files.
Run that job ID explicitly to resume. `pause` and `continue` acknowledge the
requested flag change; the worker's response is separate. `status` revalidates
saved receipts and is cancellable. Existing files are never overwritten by
export. Syntax, settings and explicit source-path argument errors are rejected
before initializing a new library.
