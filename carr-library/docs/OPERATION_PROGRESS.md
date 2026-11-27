# Operation progress and optional feedback

The main window contains a named native gauge and read-only progress details.
Background updates do not move keyboard focus. **Options → Query current
operation progress** explicitly moves focus to the details. That command and
the feedback preferences remain available during main-window worker jobs.
The shared command registry exposes `progress.status`, `progress.settings`
and `progress.mute`; none takes a default shortcut from another feature.

## Connected operations

| Producer | What is measured | Completion boundary |
| --- | --- | --- |
| Main-window `run_job` tasks | Total remains unknown when the host has no measured units | The worker returns and the host accepts its result |
| Portable snapshot packing and checking | Actual bytes read or written within the named phase | Packing alone does not complete the snapshot |
| Snapshot encryption and authentication | Actual plaintext bytes processed by the existing crypto producer | Authentication, final record and publication checks still follow |
| Snapshot transfer | Verified chunk bytes, including reused verified chunks | The vault receipt and local history must both succeed |
| Restore | Verified transfer bytes and copied library bytes | A new destination is published and directory durability is checked |
| Audio Studio worker jobs | Named real producer phases; total remains unknown | The Studio controller validates the output and the dialog accepts the result |

A percentage describes only the current phase and its known total; it is not
an estimate of the whole job. Counts stay monotonic within an unchanged phase
and total. Processing every known unit displays **final verification pending**,
not 100%. Only the host's accepted completion reports 100%. Paused, resumed,
cancelled and failed states remain distinct. A source requiring independent
review is explicitly described as such; finishing an import does not approve
its quotations.

Portable operations keep their own worker, independent of the main job queue.
Their progress handles belong to one operation and data-folder lifetime.
Changing inputs, closing a window or replacing the active data folder withdraws
stale callbacks. A late cancellation cannot hide an already committed snapshot
receipt. A partial publication shows its recovery receipt and remains failed,
without reporting verified completion.

## Feedback preferences

**Off** is the default. The optional modes are Beep, Speech and Beep+Speech,
with volume, minimum interval, mute and background controls. Preferences are
stored in the active data folder as `operation-progress.json`; malformed files
are preserved and feedback stays off. Switching data folders reloads that
folder's preferences and invalidates old task handles.
Complete library backups validate and preserve the same preference file.

The progress worker coalesces updates into bounded latest-task state. Beeps
are short generated PCM notifications with distinct known and unknown states.
Windows playback uses a separate notification process, and optional speech
checks actual installed SAPI availability. Neither route uses an Audio Studio
render or microphone pipeline. Media owners can suppress notifications for the
duration of playback or recording; each owner must release its own flag.
Audio Studio suppresses notifications before starting its rendered-master
preview and waits for any previous notification process to release output.
If output has not stopped within the bounded handoff, playback stays stopped.
Its suppression ends when the Studio player confirms Stop or closes. The
current `LocalPlayback` backend conservatively keeps this ownership until Stop
even if an asynchronous Windows preview has reached the end. No microphone
recording producer is connected by this change.

## Portable audio scope

Portable snapshots offer **Include generated audio saved in this library**,
unchecked by default. Project documents remain in the supported library backup;
the option additionally includes registered generated audio from projects
explicitly saved inside the library. External exports and arbitrary files are
excluded. Confirmation, the publication receipt and verified preview show the
chosen audio scope. A changed option invalidates the preview and is rechecked
after nested confirmation/password dialogs.

Prepared transfer identities and their private ready records bind this option.
A mismatched ready record is rejected. Changing the option prepares a separate
payload and leaves an earlier incomplete cache untouched, rather than silently
resuming a payload with a different audio scope. Both plain and encrypted
transfers are checked through the same complete-backup producer.
Older ready records without the audio field are accepted only through the exact
previous cache identity for a False/default request. They are never resumed as
an audio-inclusive payload. Invalid records stay unchanged.

The same choice is available only on the CLI's `sync publish` subcommand:

```shell
research-library sync --data-dir ./library publish --folder ./portable --empty --plain --include-generated-audio
```

Omit the final flag to keep the default metadata-only audio scope. Choose the
existing hidden-password option instead of `--plain` when encryption is wanted.
The flag is rejected for `sync list`, `inspect` and `restore`.

## Producer contract

Create an `OperationProgress(frame, label)` for a real operation. Producers may
call `update(phase=..., completed=..., total=..., state='running')` from a worker
thread. Unknown totals use `None`; do not estimate counts from status strings.
Call `complete(result)` only after result validation, or `failed(error)` after
failure. `cancel_requested()` describes a pending cooperative cancellation;
the final cancelled state follows actual worker termination.

`SyncService.publish`, `inspect` and `restore` accept an optional `on_progress`
callback with those keyword fields. The callback receives fixed phase names
and counts, never passwords. Reporting errors cannot change the authoritative
transfer outcome or conceal a committed receipt. Cancellation uses its separate
callback and safe stopping points.

## Verification

Required source checks are `tests/test_progress.py`,
`tests/ui/test_operation_progress.py`, `tests/ui/test_progress_producers.py`,
`tests/ui/test_audio_progress.py`,
`tests/sync`, `tests/ui/test_portable_sync.py` and
`tests/ui/test_desktop_handlers.py`. Run them through `scripts/ci_check.py`,
which rejects missing suites, skips and expected failures.

The installed native probe is
`python -I tests/ui/probe_operation_progress_native.py --native`. It requires
the real desktop-installed panel, native controls, actual menu events and main
worker execution; it checks focus, cancellation, preference reload and destroyed
window callbacks. A required Windows gate must require exit 0. Exit 77 means
NOT_RUN and is not acceptance. Its recording adapter observes generated PCM
and worker isolation; it does not verify physical sound, SAPI speech or NVDA.
