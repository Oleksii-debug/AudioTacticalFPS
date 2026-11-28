# Cooperative runtime resources and saved operations

The shared resource broker coordinates **participating local operations**. It
reuses the pinned `filelock` native lock implementation and the existing private
JSON helpers. Import journals, assistant checkpoints, studio jobs, translations
and transcription jobs remain in their original stores. There is no replacement
queue or universal job database.

## Admission contract

```python
from carr_library.operations import ResourceBroker, ResourceRequest

broker = ResourceBroker.for_app(library.root)
request = ResourceRequest(
    cpu_slots=2,
    memory_bytes=2 * 1024**3,       # declared estimate, not measured consumption
    disk_bytes=512 * 1024**2,
    disk_path=chosen_staging_directory,
    additional_disks=((chosen_export_directory, 256 * 1024**2),),
)
with broker.reserve(request, operation_id='asr:' + job_id,
                    cancelled=cancelled, timeout=60) as lease:
    # The existing worker must receive this cancellation callback, and must
    # terminate/close its native process before this context exits.
    result = existing_worker(cancelled=lease.cancelled)
    lease.checkpoint()
```

All CPU, memory, disk and device fields in a request are admitted atomically.
Additional disk directories account for cache/output on different filesystems
without holding one partial reservation while waiting for another. Multiple
directories on one filesystem share the same free-space accounting. The broker
checks the configured aggregate disk budget and queries actual free disk bytes;
outstanding peak-byte reservations and the configured free reserve are deducted
conservatively, even if a running job has already written part of its output.

`gpu_device='cuda:0'` with a positive `gpu_memory_bytes` requests one configured
GPU exclusively. `devices=('microphone:<canonical-id>',)` requests an exclusive
device. The actual device adapter must supply the same canonical, non-secret ID
for the same device; a hash may represent an OS endpoint identifier. Admission
does not enumerate devices or establish that a microphone or GPU exists.

An impossible request raises `ResourceUnavailable` immediately. A busy request
waits up to `timeout`, then raises `ResourceTimeout`. Cancellation before or
during the wait raises `ResourceCancelled`. Duplicate live operation IDs raise
`ResourceConflict`; the owning service still handles durable retry/idempotency.
All are subclasses of `ResourceError`.

The optional `run_timeout` is a cooperative execution deadline. The lease's
`cancelled()` becomes true and `checkpoint()` raises the precise timeout or
cancellation exception. **A timeout never revokes a live reservation.** The
adapter must finish process cleanup before leaving the context. Cancellation
callbacks must be short and must not recursively call broker operations.

`ResourceBroker(directory, limits=None, policy_path=None)` supports explicit
embedding. `ResourceBroker.for_app(library_root, limits=None)` places temporary
lease records under `runtime/resources`, outside the backup state tree, and
saves only the non-secret policy in `state/operations/resources.json`.

## Saved policy

```python
from dataclasses import replace
from carr_library.operations import ResourceLimits

current = broker.limits
broker.configure(replace(current, cpu_slots=2, memory_bytes=4 * 1024**3))
# Only after the actual chosen GPU has been reviewed/configured:
broker.configure(replace(broker.limits,
    gpu_devices=(('cuda:0', 8 * 1024**3),)))
```

`ResourceLimits` contains `cpu_slots`, `memory_bytes`, `disk_bytes`,
`min_free_disk_bytes`, `gpu_devices` and `max_leases`. `to_dict()` / `from_dict()`
provide a strict versioned-policy payload for native settings and CLI tools.
Construction with different limits never silently overwrites an existing policy;
use `configure()` explicitly. A reduction that would invalidate a live
reservation is rejected.

The default CPU policy uses at most four detected logical CPUs, leaving one
logical CPU outside the heavy-work allowance when possible. The defaults of
4 GiB declared RAM capacity, 16 GiB declared disk capacity and a 256 MiB free
disk reserve are policy defaults, **not a hardware audit**. GPUs are disabled
until explicitly configured. A worker's configured thread count must fit the
selected policy; the broker does not silently rewrite engine settings. The
active reservation limit bounds runtime bookkeeping, not development workers,
branches, PRs or repository coordination.

## Locks, crashes and recovery

Every reservation has an unpredictable lease ID, a process-local random token,
an informational PID and a native OS file lock. The lock, not the PID or a wall
clock deadline, is the authority for liveness. Reused PIDs therefore cannot make
an abandoned lease appear live or cause a current lease to be reclaimed.

The coordinator serializes inventory changes. Readers probe each lease lock;
an unlocked record is reclaimed on the next admission or snapshot. A live lock
with missing/corrupt metadata fails admission closed. Normal exceptions release
the lease in `finally`; abrupt process exit releases the OS lock. Expired or
unresponsive workers are never killed by PID. POSIX fork hooks close only the
child's inherited lock descriptors, without unlocking the parent's open file
description. A lease cannot be transferred to or released by a forked child.

The implementation uses native `UnixFileLock` / `WindowsFileLock` from the pinned
filelock 3.29.0 dependency already used by ingest. A soft-lock fallback is rejected.
Local regular files and directories are required; links, hardlinked state files,
reparse points and explicit network paths are rejected. These are cooperating
process boundaries in a trusted private directory, not protection against a
hostile administrator replacing paths or a guarantee for arbitrary network
filesystems.

The reservation belongs to the **owning application process**. It does not
manage independently orphaned native descendants after that process crashes.
Normal native-worker cancellation/timeout cleanup remains the existing runner's
responsibility. A future process supervisor or worker-held lease must be tested
before claiming orphan-process containment. No OS CPU affinity, whole-process
RAM cap, measured VRAM usage, thermal control or network sandbox is provided by
this broker. `capabilities()` states these limits explicitly.

## Canonical studio integration

`ProductionService(..., broker=shared_broker)` uses this policy. Its default is
the application's shared broker for the canonical `state/studio` directory.
Already completed outputs are checksum-verified and returned before requesting
heavy resources or constructing an audio runtime.

New/resumed work reserves the actual `AudioRuntime.budget.threads` value, a
declared RAM estimate, and cache/export disk estimates. The job records both the
request and its calculation basis in `resource_plan`. Disk estimates use source
character count, explicit pause/minimum durations and PCM/intermediate/stem
copies; they do not claim measured TTS duration or a whole-job hard output cap.
The existing native runner's byte/time/free-space checks remain in force.

Neural music takes the configured RAM pool and configured whole GPU capacity
exclusively because this adapter has no measured model peak. This is not an
assertion that the model fits. A selected CUDA/MPS device without explicit policy
capacity is rejected before rendering. Mixed GPU-device projects require one
selected device per job in this implementation. Music model hashes, runtime
identity, rights and actual-inference readiness remain the existing music
adapter's separate checks.

Pause/cancel while waiting is written to the same existing studio control file.
The admitted context surrounds native worker execution and cleanup. Completed
node receipts and output verification are unchanged, so resource admission does
not create a second production job or duplicate accepted audio on resume.

## Reader Read and Save WAV

The reader uses the same local resource policy for **Read** and **Save WAV**
when the selected provider is Piper, a local helper or another heavy adapter.
The decision follows the actual registered provider and route. A voice's
display name, language or engine label cannot grant a basic-voice exemption.
The built-in SAPI, Flite and eSpeak reading routes remain available while
heavy work holds these reservations, subject to their own existing engine and
playback checks. Studio production keeps its existing admission.

The reader shows **Preparing local speech. F8 cancels.** while it waits for
resources and checks the captured request. **Rendering locally** appears only
after admission and the current reader has confirmed the same source, voice
and native voice options. If you close the reader, change libraries or cancel,
a queued confirmation cannot revive that request. A changed source or runtime
requires a fresh request; no replacement voice is selected.

For a heavy route the reader conservatively reserves the complete configured
CPU and RAM allowances. It also reserves space on the selected output's volume
for the combined WAV and a current utterance, plus bounded protocol scratch.
These are cooperative policy allowances, not measurements or operating-system
CPU/RAM limits. A positive saved RAM allowance is required. This reader
integration declares no GPU device or GPU memory reservation and does not
qualify GPU inference.

F8 is checked while waiting, during supported asset verification and by the
existing native runner. Known model/hash adapters receive the cancellation
signal; a trusted legacy identity callback without that interface can only be
checked before and after it returns. Cancellation does not revoke resources
from a process that has not confirmed shutdown. The reservation and exact
temporary audio remain retained until every causal native cleanup owner is
quiet, including when the reader window has already closed.

## Selected progress voice

**Use voice for progress...** keeps the explicitly selected voice and the
existing notification worker. A heavy selected provider now uses the same
library resource policy as Read and Save WAV. Waiting shares the notification's
existing 30-second deadline; synthesis retains its 20-second and 2 MiB limits.
The built-in basic voice routes keep their existing admission behavior.

Cancelling a queued notification prevents late playback. If its native process
has not confirmed shutdown, the exact temporary audio, active registry use and
notification serialization remain retained until every causal owner is quiet.
A failed playback close still retains its uncertain player and audio. No other
voice, output device or progress mode is selected automatically.

## Shared operation view

```python
from carr_library.operations import OperationFacade

operations = OperationFacade(app, broker=broker,
                            studio=studio_service,
                            assistant=assistant_service,
                            transcription=transcription_service)
rows = operations.list(workspace=selected_workspace, limit=1000)
row = operations.get('studio', job_id)
operations.control('studio', job_id, 'pause')
```

Kinds are `import`, `assistant`, `studio`, `translation` and `asr`. An
`OperationView` contains `kind`, `id`, `workspace`, stored `status`, measured
checkpoint `completed` / `total` when present, a fixed `phase`, `updated`,
supported `controls`, relative `checkpoint` and `live`. The view neither loads
models nor executes jobs or provider requests. It does not copy document text,
credentials, raw error bodies or provider profiles into the dashboard.

`live=True` means a matching native broker reservation exists. `live=None`
means unknown: a legacy or light operation may legitimately run outside this
broker. Absence of a reservation never changes a saved job into failed or
interrupted. Likewise, a saved `complete` status is a journal observation;
opening/resuming/exporting through the owning service retains its original
artifact verification requirements.

Only explicitly supplied service instances expose their existing pause/cancel
methods. A stop request is not reported as an acknowledged stopped state.
Resume stays in the original workflow, where its exact saved plan and any
external-provider approval can be checked. Imports/translation lacking durable
cross-process stop controls remain view-only here. Corrupt checkpoints are
represented as attention rows without leaking their contents.

The view returns at most its explicit `limit` in stable kind/ID order and scans
at most 10,000 checkpoints. It is a bounded dashboard, not a replacement large
corpus job index. No capability is inferred from a format name or a stored
completed status alone.

### Read a saved Studio operation

Open **Tools → Tasks and resources...**, choose **Refresh**, then select a row
in **Saved platform operations**. **Operation details and resource status**
shows labelled, read-only lines from that saved snapshot. Refresh again when
you need newer progress.

The existing operation ID, workspace, status, update time, progress and
available controls remain visible. Studio rows can also show:

| Detail | Meaning |
| --- | --- |
| **Current canonical handler** | The reader used by this application to interpret the saved Studio job. It does not identify the engine or model used for an earlier render. |
| **Saved operation-plan SHA-256** | The recorded identity of the job's selected project fingerprint, output format and stems choice. |
| **Separate settings SHA-256** and **Selected engine or model adapter** | **Unknown; not recorded** when the checkpoint has no separate declaration. An old record does not acquire new identity evidence by being opened. |
| **Declared CPU slots**, memory, GPU and disk reservations | Saved resource allowances and estimates. They are not measurements of actual consumption or a promise that work can be admitted now. Paths and device identifiers are omitted. |
| **Recorded completed artifact identities** | Recorded SHA-256 digests and byte sizes for a job whose saved status is complete. The view shows at most 64 identities, with the full count and an explicit omitted count. |

Selecting a row does not open or hash its output files. An artifact digest can
therefore remain visible after the file has been removed or changed. Return to
the original Studio workflow when you need its current output verification;
the task view does not confirm that a recorded file still exists.

**Unknown; not recorded** and **Unavailable in this saved checkpoint** preserve
missing historical information. A malformed canonical record becomes an
**attention** row without action controls. Its private error body is not shown.

A restored job can show **Saved restore marker requires a new output: Yes**.
Review and choose a new output through Studio before running it. Viewing the
record, or seeing its previous completion status, does not renew permission to
use the former destination. **Pause selected** and **Cancel selected** request
the existing owner's controls; the displayed status changes only when that
owner records its response.

## Evidence and remaining native checks

`tests/operations` runs real spawned-process contention and abrupt exit,
PID-reuse metadata simulation, cancellation, live timeout behavior, failed state
publication, corrupt live metadata, policy conflicts, filesystem/device/GPU
admission contracts, POSIX fork inheritance, existing LibraryApp import and
assistant journals, and actual FFmpeg studio output/reuse. GPU admission tests
exercise declared capacity and exclusivity; they are not GPU inference tests.

The canonical existing studio suite still exercises real installed Flite speech,
music, mix/stem export, pause/restart and corruption recovery. Linux evidence is
not transferred to Windows: native Windows lock behavior and keyboard/NVDA
acceptance need their own execution on the target build. No models, heavyweight
dependencies or paid API calls are required by these resource tests.

Primary implementation evidence is the installed pinned filelock source
(`filelock/_unix.py`, `_windows.py`, `_api.py`), the existing
`ingest/writer_guard.py`, `durable_state.py`, canonical job stores and
`audio/process.py`. The broker does not introduce another lock library.
