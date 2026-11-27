# Retained local transcription from the command line

This interface uses the same `TranscriptionService`, reviewed runtime manifest,
machine-local engine profile registry, owned worker process, resource broker,
portable job definition and completion verifier as the desktop.

## Select the existing library and explicitly reviewed runtime

The library directory must already exist. No command downloads a model or runs
an executable merely because a profile, backup or historical receipt exists.

    research-library transcription --data-dir D:\ResonanceData inspect-runtime --manifest D:\LocalWhisper\manifest.json
    research-library transcription --data-dir D:\ResonanceData register-runtime --manifest D:\LocalWhisper\manifest.json --reviewed-sha256 EXACT_64_HEX_DIGEST --name "Reviewed local Whisper"
    research-library transcription --data-dir D:\ResonanceData runtimes

Inspection hashes the selected complete local inventory without executing it.
Its manifest SHA-256 must be passed unchanged to registration. The existing
manifest acceptance, license declaration and trusted-weight requirements still
apply. Registration is local to this library location; backups do not activate
machine-specific executable paths. Identity verification is not inference,
language or quality qualification.

For preparation or execution, choose either an enabled `--profile ID`, or
`--manifest PATH --reviewed-sha256 DIGEST`. The two forms cannot be combined.
The exact chosen pin is checked inside preparation, closing a selection-to-copy
replacement race. Execution additionally requires the immutable job's stored
runtime and model identities.

## Prepare, start and control

    research-library transcription --data-dir D:\ResonanceData submit D:\Audio\chapter.mp3 --profile PROFILE_ID --language uk --format txt --format srt
    research-library transcription --data-dir D:\ResonanceData submit D:\Audio\Book --recursive --profile PROFILE_ID --output-mode one_per_folder --run
    research-library transcription --data-dir D:\ResonanceData jobs
    research-library transcription --data-dir D:\ResonanceData status JOB_ID
    research-library transcription --data-dir D:\ResonanceData run JOB_ID --profile PROFILE_ID
    research-library transcription --data-dir D:\ResonanceData pause JOB_ID
    research-library transcription --data-dir D:\ResonanceData continue JOB_ID
    research-library transcription --data-dir D:\ResonanceData cancel JOB_ID
    research-library transcription --data-dir D:\ResonanceData resume JOB_ID --profile PROFILE_ID

`submit` only retains inputs unless `--run` is explicit. The prepared ID is
printed to stderr immediately after successful preparation; it remains useful
if a subsequent run fails. Folder recursion requires explicit `--recursive`.
All inputs retain the service's 1,000-file, 20,000-entry and 32 GiB job bounds,
independent-source identity checks and disk-space admission.

`run` and `resume` start a fresh controlled attempt from the existing owned
inputs. They archive previous absolute configuration/run bindings. They do not
reuse a foreign receipt as a new successful run. `continue` instead clears an
active worker's pause request. Pause/cancel responses report a request, not an
acknowledgement; the worker settles at its checked protocol boundaries.

Ctrl+C is converted to cooperative cancellation while native ownership remains
with the current supervisor. Cancellation exits with 130. Ordinary failures
exit with 1. Verified completion exits with 0. Progress uses bounded JSON on
stderr; final results use JSON on stdout.

Settings are the current 17-field contract. Available flags include local
`--model`, `--language`, `--task transcribe|translate`, `--device`,
`--compute-type`, `--beam-size`, `--parallel-files`, `--output-mode`,
repeated `--format`, `--multilingual`, `--no-vad`, `--include-headers`
and `--no-previous-text`. Whisper translation here means English output,
without a remote translation provider. The current run admission requires an
explicit CPU or CUDA selection and declared hardware allowances; an automatic
device value in a saved setting does not bypass that check.

`--timeout` accepts one second through seven days. It is a cooperative run
deadline, not a promise about a model's speed.

## Verify, read, edit and export after a move

    research-library transcription --data-dir D:\RestoredLibrary verify JOB_ID
    research-library transcription --data-dir D:\RestoredLibrary outputs JOB_ID
    research-library transcription --data-dir D:\RestoredLibrary read JOB_ID --output group/file.txt
    research-library transcription --data-dir D:\RestoredLibrary read JOB_ID --output group/file.txt --plain
    research-library transcription --data-dir D:\RestoredLibrary save-edit JOB_ID --output group/file.txt --text-file D:\Edits\reading.txt
    research-library transcription --data-dir D:\RestoredLibrary export-edit READING_PROJECT_ID --output D:\Exports\reading-copy.txt
    research-library transcription --data-dir D:\RestoredLibrary export JOB_ID --output D:\Exports\NewTranscription
    research-library transcription --data-dir D:\RestoredLibrary export JOB_ID --output D:\Exports\Subtitles --format srt

`status` reports recorded metadata and never asserts completion.
`verify`, `outputs`, `read` and `export` invoke the current saved-completion
verifier. It rehashes only current owned inputs/results and maps old absolute
bindings lexically. The original inputs, runtime installation and model weights
may be absent. The response remains explicitly historical and does not claim
that the current machine ran inference.

The output path is taken from `outputs`; it is never an arbitrary filesystem
read. Omitting `--output` is allowed only when exactly one accepted TXT exists.
Reading/editing retains the existing 8 MiB text boundary. A saved edit is an
ordinary separate reading project with its original accepted text retained.
Its ID is explicit; there is no implicit \"latest edit\" selection and no
replacement of recognition output.

Whole-result export holds the same namespace, job and standalone-worker native
guards. It copies bounded chunks into independent files under `files/`,
writes `transcription-export.json`, rechecks the accepted receipt, and
publishes one new directory. Selected formats must already be accepted; export
does not regenerate another format or rerun recognition. An existing directory
is never overwritten. Temporary disk is reserved through the current resource
broker. No original absolute location is copied into the export receipt.

A post-move directory-sync failure is reported separately as
`published_durability_unconfirmed`, with the retained destination. This is
not reported as cancellation or successful durability, and a retry cannot
replace that directory. A cancellation arriving after successful publication
does not revoke the completed export.

## Explicit replacement of a local runtime

    research-library transcription --data-dir D:\RestoredLibrary reprepare OLD_JOB_ID --manifest D:\ReviewedRuntime\manifest.json --reviewed-sha256 EXACT_64_HEX_DIGEST

`reprepare` creates a new current job from the old job's retained input copies.
The old definition, completion and history are unchanged. No old executable
path, live-setting file or approval is restored as authority. Start the new job
only after reviewing its ID/settings, or include explicit `--run`.

Canonical historical `state/transcription` recovery remains the separately
reviewed inert-history bridge and desktop action. This CLI does not import
the old service or make its recorded status a current completion.

## Acceptance evidence

The additive source suites are `tests/test_transcription_cli.py` and
`tests/test_transcription_exports.py`. They exercise the real current
filesystem/service, exact manifest pin, actual harmless synthetic protocol
child, cross-attempt receipts, offline reading/editing, independent exports,
cancellation, no-overwrite and post-publication durability boundaries. The
isolated CLI route asserts both installed product and bootstrap origins.

These cases do not decode real audio or qualify Whisper/model/language quality.
At this source checkpoint the executor is unavailable: new source, installed
and Windows test execution is **NOT_RUN**. Required zero-skip CI gates are
registered; only their actual exact-commit results may change that status.
