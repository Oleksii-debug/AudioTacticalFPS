# Supplied local transcription integration

## Implemented boundary

The existing desktop exposes **File → Transcription: local audio and video...**
through its original command and saved keyboard binding. One explicit profile
selector routes the two existing workspace profiles or the library-wide
`supplied-whisper-v43-hardened-1` protocol to their matching services. Existing
job identities and scopes are preserved. This profile supervises the reviewed
supplied worker; it contains no recognition implementation.
The public repository contains no supplied private worker, private recording,
transcript, model weights, virtual environment or executable runtime.

The dialog accepts explicitly selected files or folders and a reviewed local
runtime manifest. It exposes the protocol's seventeen typed settings, including
language detection or selection, transcription or English translation, model,
device, precision, beam size, parallel files, VAD, grouping and TXT/SRT/VTT/JSON.
Preparing and starting are separate actions. Preparation captures an exact
selection into independently owned input files and freezes settings; later files
appearing in a selected folder are not silently adopted.

Admission verifies the entire runtime inventory, required model files, accepted
rights/trust flags and declared language/device/task capabilities. Execution uses
the reviewed interpreter with `-B -I` and explicit job arguments. A shared,
job-local Windows status replacement startup permits an already open reader to
retain its old snapshot while the actual child publishes the next status. The
existing process termination and project lock contracts are reused. Pause, resume and stop
write only typed protocol controls. Closing the dialog requests cancellation and
waits for the operation to settle before releasing its controls.

Successful process exit and a completed status are insufficient. Completion must
bind the run ID, exact configuration bytes, owned input bytes, immutable settings,
runtime/model identities and every requested output to verified hashes. Output
formats and source coverage must match the selected grouping. A partial export,
changed input/output, missing receipt or nonzero exit remains failed/cancelled.

Saved jobs remain portable: their authoritative definition uses relative owned
paths; original source locations are inert provenance. Reading a previously
accepted transcript after relocation revalidates current saved bytes without
opening the old runtime/model paths or launching an engine. Restart requires a
fresh admitted manifest and derives a new configuration from the relative
definition. Previous execution records are retained in history. Opening a result
for editing creates an existing narration reading copy and preserves the verified
transcript as its source.

## Recovery and backup

The typed backup producer preserves queued inputs, immutable settings, verified
outputs, receipts and interrupted publication records. Unknown durable files,
escaping paths, unexpected links and inconsistent identities fail visibly. The
standalone worker's actual OS lock is also acquired during snapshot creation.
Restoring does not activate archived runtime paths or external services. See
[the backup contract](SUPPLIED_TRANSCRIPTION_BACKUP.md) for the exact namespace,
transaction rules, exclusions and recovery evidence.

## Actual evidence and its limits

On 2026-11-27 the portable source checks executed:

| Gate | Actual result | Scope |
|---|---:|---|
| Receipt and strict settings | 112 passed, zero skipped | Hashes, paths, settings and output coverage |
| Supervisor with real child processes | 42 passed, zero skipped | Owned capture, failure, stop, restart, relocated reading and held status snapshots |
| Typed transcription backup | 28 passed, zero skipped | Real backup/restore, lock contention, recovery and coexistence with both workspace profiles |
| Dialog/session contracts | 36 passed, zero skipped | Typed controls, explicit profile/scope, callbacks, close, reading-copy and shared progress |
| Shared command-line entrypoint | 13 passed, zero skipped | Explicit protocol/scope, all settings, control requests and verified reading copies |

The process fixture is explicitly synthetic and does not decode audio. The
native Windows probe drives actual installed wx controls with a synthetic
presentation service; its purpose is control naming, types, dispatch and
completion-aware cancellation. Its Windows execution is a separate CI gate.
The source and installed-package workflow gates require all five suites and do
not accept skips or expected failures.

Private worker review and execution evidence are retained separately from this
public code. They do not establish real-model transcription quality, all-language
coverage, native NVDA acceptance, diarization or redistribution clearance. Actual
reviewed runtime/weights, authorized audio, Windows offline execution and quality
assessment remain required for engine qualification. A Python audit boundary is
not a sandbox for native decoder/model code; system-level egress testing remains
part of qualification.
