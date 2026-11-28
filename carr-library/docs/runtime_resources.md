# Shared local resource admission and saved operations

## Source provenance and scope

This component reuses the native-lock broker, typed policy, task-view controller
and native keyboard probe from prepared PR112 commit
[5ba8389a193f1907810b8f00aba2e386993fd7a3](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/5ba8389a193f1907810b8f00aba2e386993fd7a3).
The earlier reviewed resource component was 43fc4d2385c412565e31599009b81adeaee0498b.
Its test result applies to that earlier component. This composition adapts the
current library-scoped transcription service, import supervisor and Studio;
its source, installed-wheel and native acceptance must be run independently.
It retains current V5 recognition, source authority, receipt checks, typed backup,
owned voice cleanup, pronunciation and M4B/LRC export.

The coordinator is not another job queue. Original services retain job identity,
state transitions, recovery, progress, publication and explicit resume.
No provider call, automatic retry, source transfer or model download is added.

## Policy and reservations

ResourceBroker.for_app(data_root) uses native file locks in runtime/resources.
Its operator policy is state/operations/resources.json. Only that saved policy
is included in ordinary application-state backups. Live lock files and lease
records stay outside backed-up state; restore creates a new empty coordinator.
The same pinned filelock==3.29.0 native backend is reused. Unsupported native
locking fails visibly instead of falling back to soft lock files.

Adapters declare CPU slots, estimated RAM, selected GPU capacity, estimated disk
bytes on each output volume and exclusive device IDs. Admission checks the
combined declared amounts and actual free disk space. A GPU requires an explicit
configured allowance; admission never chooses a replacement device. Policy
changes cannot invalidate a live reservation. The native editor also compares
the exact policy the user reviewed, so a concurrent change requires refresh.

These values are cooperative allowances, not measured CPU/RAM/VRAM usage or
operating-system memory limits. Waiting is cancellable and bounded. A lease
remains held through the owning operation's existing child cleanup and receipt
publication. Time passing or a stale-looking PID cannot revoke a live lock.
Native lock ownership decides crash recovery; the next admission or snapshot
reaps unlocked stale records. The broker does not kill orphan descendants or
replace existing process containment and network restrictions.

## Current service adapters

**Studio.** The service admits the actual configured AudioRuntime thread count.
Only its otherwise unspecified default thread count is reduced to fit the
operator allowance; an explicit runtime budget is preserved and may be refused.
A verified completed job returns without starting a runtime or acquiring capacity.
Approved clip inspection takes a short CPU/RAM lease before estimating duration.
The subsequent render acquires its cache and output-volume allowances atomically.
Estimates account for speech text, pauses, clip trim/placement and requested
speaker/clip stems. Source bytes, project fingerprint and output identity are
checked again after waiting. Existing pause/cancel semantics and receipt reuse
remain authoritative. Model peak memory is unknown, so neural work reserves the
configured RAM/GPU pool exclusively.

**Supplied-worker transcription.** The current TranscriptionService(app, workspace)
and its workspace-hashed jobs are retained. Admission occurs after the verified
completed-result shortcut and before reading the worker configuration, retained
sources or model contents. The existing transcribe lock and lease enclose the
original V5/bootstrap, child supervision, stopped-state handling and final
receipt admission. Unknown model memory reserves the configured RAM pool.
CUDA work requires the configured cuda:0 allowance. The declared CPU estimate is
parallel_files multiplied by the selected host thread hint, between one and two.
The same hint is passed as OMP_NUM_THREADS and OPENBLAS_NUM_THREADS. These
environment settings are not proof of a worker's actual thread count; a supplied
worker may have other pools. Explicit parallel-file and recognition settings
are unchanged. The original AudioBudget controls execution time, including valid
allowances longer than one day. No shorter broker execution timer is imposed.

**Isolated document import.** One parser-process CPU slot, 3 GiB declared memory
and 512 MiB temporary disk are admitted before the current request attempt starts.
The memory allowance matches the existing POSIX address ceiling; it is only a
declared allowance on Windows. Native library threads are not measured by this
slot. Existing source authority, isolated child launch, journal and recovery are
retained. Cancellation or admission timeout saves a resumable request outcome
without starting a child. A completed request reuses its existing publication
without requesting capacity. A cleanup diagnostic after committed publication
cannot relabel the durable request as failed.

Small direct text operations keep their existing path. The shared broker does
not claim universal admission for every background service or third-party process.

## Saved-operation view and CLI

Open Tools, Tasks and resources, or find that action with Ctrl+Shift+P.
The native dialog and CLI read the same bounded canonical journal view. They
show Studio, assistant, import-batch, translation and transcription checkpoints.
Stored completion is a journal observation. Original workstations still verify
actual outputs and authorize resume. An active cooperative reservation is
positive evidence of ownership; absence is unknown, not evidence of termination.

Current transcription jobs live at
state/transcription/<sha256(workspace)>/jobs/<job-id>/job.json.
The view reports ready, running, stopped, partial or completed as recorded and
uses accepted receipt counts, not a worker's unverified live progress. Selecting
a library restricts ASR discovery to its exact scope directory before any journal
read. Explicitly enabled shared controls now resolve the exact selected transcription
service and expose its durable pause/cancel methods. A restored execution review,
uncertain interruption or rejected attempt suppresses these controls. An accepted
request remains distinct from worker acknowledgement. Open
workstation selects the existing job in its original active library and starts
no recognition. The current workstation's stop/continue controls remain available.

Studio and assistant controls forward only their existing explicit methods,
with fresh library/data-root checks. A requested stop is not reported as an
acknowledged stop. Import and translation expose no invented control operation.
Source-bound translations retain their source workspace in filtering. No
checkpoint source text, transcript or credential is copied into the shared view;
malformed records receive fixed recovery text.

Examples, using the existing installed entrypoint:

    python -m carr_library operations --data-dir <data-folder> list
    python -m carr_library operations --data-dir <data-folder> --workspace <library-id> get asr <job-id>
    python -m carr_library operations --data-dir <data-folder> resources
    python -m carr_library operations --data-dir <data-folder> configure-resources <policy.json>

The configuration file contains the exact ResourceLimits fields. Invalid policy
preserves the last committed allowance. This interface contains no Run or Resume
command. CLI execution uses the existing installation lease.

## Native lifecycle and acceptance

Policy reads, journal refresh and controls use the desktop's existing worker,
not another executor. The native close control stays available during a pending
view request and cancels only that request. Disposal detaches callbacks before
deferred native destruction and closes only services created by this controller.
Successful durable policy saves and accepted controls remain successful even if
an optional refresh is cancelled. Unchanged form fields preserve exact saved
byte values despite rounded display units.

The reused scripts/windows_operations_smoke.py exercises the actual installed
wx main loop and command inventory: keyboard entry through the palette, assistant
pause/cancel without resubmission, concurrent policy rejection, reviewed save,
close while a native admission lock is held, and deferred destruction.
It reports NOT_RUN on non-Windows systems. It makes no manual NVDA, acoustic
inference or measured hardware-capacity claim.

Required portable coverage includes actual native-lock process contention and
crash recovery, canonical synthetic ASR child/receipt/stop tests, actual isolated
PDF import and completed replay, Studio child cleanup and FFmpeg output, backup
policy restore without live leases, source-scoped discovery, and native-controller
lifecycle. The fork-inheritance test in test_resources_posix.py is POSIX-only;
keep it in the Linux gate rather than weakening it for a zero-skip Windows gate.
