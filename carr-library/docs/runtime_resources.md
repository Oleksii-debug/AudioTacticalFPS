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
selected policy; the broker does not silently rewrite engine settings. When
Studio constructs a previously unconfigured default audio runtime, its default
FFmpeg thread count is selected within the CPU allowance and recorded in the
resource plan. Caller-supplied runtimes retain their exact requested count and
are rejected if that count exceeds the selected allowance. The
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

## Canonical supplied-worker transcription

The existing `audio.transcription.TranscriptionService` retains its canonical
`state/transcription/<64-character-job-id>` requests, retained input snapshots,
control files, receipts and private-runtime manifest checks. There is no additional
ASR engine or job store. Each file needing work waits for shared admission before
the existing isolated supplied worker starts. Verified completed files are reused
without a new heavy-work reservation; completed-job resume still verifies its
artifacts through the original service.

The supplied worker's native thread count and model peak RAM are unmeasured by
this integration. Its request therefore reserves the whole configured CPU/RAM
allowance, a bounded per-file staging/output disk estimate, and an explicitly
configured generic CUDA pool when selected, for example `cuda=4096` in the native
MiB field. The supplied runtime has no validated device-index capability, so this
pool does not identify a physical card or measure VRAM. Studio retains its explicit
`cuda:0` policy. Both adapters reserve the same exclusive `accelerator:cuda` token;
different policy names cannot permit concurrent participating CUDA work. The
recognition device/settings and inherited device mapping are not rewritten, and
admission never switches CUDA requests to CPU. Unconfigured generic CUDA is
rejected before launch. The existing runtime-specific lock remains in force.

The saved job records the estimate and its basis. Pause/cancel while waiting uses
the same control file as the transcription workstation and CLI. The lease covers
worker execution and cleanup; completed receipts and atomic output publication
remain authoritative. Source text, model files and interpreter files are not
copied into the resource policy or dashboard.

## Resource cleanup and saved policy

If removal of disposable lease metadata or its directory sync fails after native
work, the native lock still releases and the lease reports deferred cleanup.
Completed supplied transcripts and Studio outputs retain successful completion
with a cleanup notice. Changed lease identity and native-lock release failures
remain errors. Later admission reaps unlocked records through the existing path.

Only `state/operations/resources.json` is durable policy. The existing complete
library backup snapshots and restores that committed state file. Live leases,
coordinator state and their path binding remain under `runtime/resources` and
are not restored as active reservations; this uses the existing backup format.
The restore regression checks exact policy bytes and an independent new admission
while the original broker still holds a live lease, with generated state both
included and excluded.

## Isolated import and OCR

The existing isolated-import supervisor reserves resources around its native
child invocation, after completed-result replay. It holds no database transaction
while waiting or parsing. After admission it rechecks workspace authorization
before launch, so archiving a workspace while its request waits cannot start
newly unauthorized work.

This boundary reserves the whole declared CPU/RAM pool because parser/OCR native
thread and peak-memory use are unmeasured. Disk admission covers both the data
volume and actual temporary-directory volume, using the saved image-pixel limit
and bounded worker reply/log sizes. These are estimates; existing parser limits
and process supervision remain in force. Resource cancellation, waiting timeout
and rejection become the original structured import result/journal states,
preserving request IDs and resumability. The V5 attempt identity, request
fingerprint, committed-publication receipt and reconciliation after a stopped
worker remain unchanged.

Direct callers of standalone audio conversion, capture or other engines outside
these three participating service boundaries are not automatically enrolled.
The policy does not restrict ordinary reading or measure physical device capacity.

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

An injected broker or service must belong to the same data folder. The facade
checks that identity at construction and again before reading or controlling a
job; identical content-addressed IDs in a copied library cannot redirect controls.
ASR rows read canonical checkpoint metadata and require a complete checkpoint's
input/completion inventories to match. They do not hash every transcript merely
to show a saved status. Opening the transcription workstation retains its own
artifact verification.

The view returns at most its explicit `limit` in stable kind/ID order and scans
at most 10,000 checkpoints. It is a bounded dashboard, not a replacement large
corpus job index. No capability is inferred from a format name or a stored
completed status alone.

## Native Tasks and resources

The Tools menu opens a standard wx dialog with named Jobs and Resource allowances
tabs, a saved-job list, status field, pause/cancel controls, local policy fields
and an action to open a selected job in the existing transcription workstation.
That action selects the saved job for inspection; it does not start inference.
The local CLI remains available with `python -m carr_library operations --help`.

Dialog construction performs no broker filesystem or lock work. Initialization,
refresh, policy saves and controls use the desktop's existing background executor.
Close remains responsive while initialization waits; it requests cancellation and
closes after the worker request completes. A destruction request immediately
detaches callbacks before wx's later native-deletion event. Child-control
destruction does not detach the whole dialog.

The desktop retains its single-worker coordination: during work already owned by
that window, use that operation's current pause/cancel controls. The dashboard can
inspect saved work and request supported controls for other cooperating
application processes; it introduces no parallel executor. If a policy save commits
and the following optional refresh is interrupted, the UI reports the saved
policy and incomplete refresh separately.

## Evidence and remaining native checks

`tests/operations` runs real spawned-process contention and abrupt exit,
PID-reuse metadata simulation, cancellation, live timeout behavior, failed state
publication, corrupt live metadata, policy conflicts, filesystem/device/GPU
admission contracts, POSIX fork inheritance, existing LibraryApp import and
assistant journals, and actual FFmpeg studio output/reuse. GPU admission tests
exercise declared capacity and exclusivity; they are not GPU inference tests.

Additional tests use the actual canonical supplied-worker subprocess fixture
for partial completion, same-store control/resume, completion-inventory
consistency, resource-wait cancellation and cross-library rejection. Isolated
import tests run real PDF children, including a workspace archived while queued.
UI controller tests use a real thread pool and held native admission lock to
prove foreground close dispatch during initialization. The actual POSIX fork
case lives in `test_resources_posix.py`; strict Windows gates select the other
supported files without silently passing or removing the fork check.

`tests/ui/native_operations_smoke.py:run_native_operations(frame, directory)`
is the separate native wx acceptance helper. It uses the provided installed frame
and existing executor, dispatches actual named-control events, saves the same
policy, and observes native destruction in a bounded GUI event loop. It contains
no source-path override. PASS results require executing that helper; controller
tests do not establish native Windows or manual NVDA success.

The canonical existing studio suite still exercises real installed Flite speech,
music, mix/stem export, pause/restart and corruption recovery. Linux evidence is
not transferred to Windows: native Windows lock behavior and keyboard/NVDA
acceptance need their own execution on the target build. No models, heavyweight
dependencies or paid API calls are required by these resource tests.

Primary implementation evidence is the installed pinned filelock source
(`filelock/_unix.py`, `_windows.py`, `_api.py`), the existing
`ingest/writer_guard.py`, `durable_state.py`, canonical job stores and
`audio/process.py`. The broker does not introduce another lock library.
