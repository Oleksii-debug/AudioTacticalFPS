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

## Participating services in this composition

This adapter keeps the accepted application services and saved formats:

| Operation | Existing service and journal | Shared view and controls |
| --- | --- | --- |
| Transcription | `carr_library.transcription.TranscriptionService(app, workspace)`; `state/transcription/<workspace-sha256>/jobs/<32-hex-id>/job.json` | Stored host status and file counts; open the selected saved job in its original workstation. Stop uses the active run callback. No invented cross-process pause/cancel API. |
| Studio | `studio.render.ProductionService`; `state/studio/jobs/<id>/job.json` | Stored accepted node count, existing pause/cancel requests, original run/resume workflow. |
| Translation | Existing `TranslationService`, `JobStore`, Gateway and usage ledger; `translation/1` or source-bound `translation/2` | Validated receipt counts and authorized source workspace; view only. Remote resume and local export remain in the original translation dialog. |
| Assistant | Existing assistant checkpoint and control files | Stored step counts and existing pause/cancel methods. |
| Import | Existing import-batch journal | Stored input counts; view only. |

Only **ASR and Studio** join heavy-work admission in this composition. The other
rows are views over existing stores. This does not add an import/OCR adapter,
recording-device enforcement, translation provider or alternate queue.

The broker, models and facade reuse the bounded canonical contribution from
PR169 `c2ec4284e9527114d8df27683b2d8d1f02872fd8`. Native lock preflight also
includes the already reviewed normal-release race correction from PR112
`40c58b4f631e48b863ef2867d61100ee8445092a`: a lock removed by its previous
owner before acquisition is retried only after parent validation. Acquired
descriptor/path identity checks remain strict. Metadata cleanup and actual
standalone process fixtures are retained.

The existing workspace-scoped `receipts-v1` transcription module continues to
own recognition and retained inputs. This adaptation does not replace it with
another transcription package, worker protocol or model manager. Studio keeps
its current clip/input approvals, runtime byte identity and owned voice lifecycle.

## Accepted supplied-worker admission

A completed ASR job verifies its existing artifacts and returns before constructing
a broker, reserving capacity or starting a worker. A new or stopped job retains
its original settings and protocol. Before a native launch it reserves the whole
configured CPU/RAM pool, the existing bounded output/staging estimate plus its
disk reserve, and the explicitly configured generic CUDA pool when selected.
The worker's real thread count and model peak have not been measured here.
Admission coordinates declared allowances; it does not impose a physical limit
on the worker or silently change its settings.

ASR has no reviewed physical GPU index capability. Its `cuda` pool and Studio's
`cuda:0` pool therefore also reserve the same `accelerator:cuda` token.
Participating CUDA work cannot overlap merely because those policy names differ.
Missing configured capacity is a visible refusal before launch. This does not
prove a model fits in the declared memory.

No database transaction remains open while waiting. After admission, ASR
reauthorizes the current workspace, compares the current verified interpreter/
worker configuration, selected model root, and retained sources. The current
`receipts-v1` protocol delegates model resolution to its selected worker; its
existing returned receipt establishes that identity. This adapter does not
fabricate a host model-file identity or add a V5 protocol.

The existing runner still sends the stop flag, allows its bounded grace period
and reaps the actual child. The resource lease encloses that cleanup and output
verification. A cancelled wait starts no child and preserves the previous
checkpoint. Explicit resume uses the original service, with its original
configuration and receipt checks. Stopped cached files are not accepted exports.

The native progress callback has a separate prelaunch resource-wait phase.
Existing Windows live-progress behavior is preserved: the host does not hold
the producer's mutable status file open while it runs. Accepted counts still
come from the existing terminal receipt verification; a stopped result does not
grant export authority to unverified cached files.

## Studio admission and retained results

The existing production DAG keeps its node hashes, project/script identity,
original sources, model validation and atomic output publication. Its resource
request uses the runtime's actual configured FFmpeg thread count, a declared
memory estimate and cache/export disk estimates. When no explicit runtime
thread budget was supplied, the default is selected within the local CPU policy.
Explicit caller budgets are retained and rejected if they exceed that policy.

A neural score requests the declared RAM/GPU pool exclusively because there is
no measured model peak. Current configured model manifests and existing
runtime/model validation still determine acceptance. Admission never declares
inference qualification, licensing clearance or acoustic quality.

Pause/cancel while waiting uses the original Studio control file. Existing
verified completed jobs return without heavy admission. After output validation,
a disposable resource metadata cleanup failure is reported as a notice while
the successful committed outcome remains successful. Native lock-release or
changed reservation-identity failures remain errors.

The current process runner is preserved. Its reader-thread startup failure
cleanup after `Popen` owns and reaps the native child before the caller can
leave its resource context; the reused two actual-child regressions cover this
boundary without replacing current process-tree cleanup.

Disk estimation includes explicit clip start/end times and separate clip stems.
An untrimmed cue without a declared duration remains explicitly unmeasured before
admission, as recorded in `untrimmed_cues_without_measured_duration`. The shared
estimate is not a promise that arbitrary compressed input fits. Existing bounded
audio staging/free-space checks continue to enforce the actual worker limits.

## Native Tasks and resources

The existing desktop executor opens a native dialog from Tools → Tasks and
resources. Named Jobs and Resource allowances tabs expose saved counts, supported
controls and the cooperative policy. Initialization, refresh and policy writes
run on that same executor. There is no new worker pool or scheduler.

The controller captures the selected library and data folder. A changed selection
or closing window detaches queued work. ASR jobs are discovered through active
catalogue workspace identities and their existing hashed directories.
A copied 32-character job ID in a different workspace has a different shared
operation key. Directory names never grant source authority.

Only the selected active library can hand an ASR job to its existing transcription
workstation. The handoff rechecks the saved job in the background, selects it for
inspection, and does not start recognition. ASR stop remains in its running
workstation. The shared view does not manufacture a pause/cancel method or rewrite
a journal to pretend a real child stopped.

Translation views use the existing validated loader and 64 MiB checkpoint
allowance. They preserve the canonical source binding, UUID version, approved
data class and decoded-output usage evidence. They do not renew provider
permission, access a credential, replay a paid request or grant export authority.
The saved source/account/budget selection and fresh profile checks remain in
the original translation UI. A restored pending approval cannot acquire remote
authority merely by appearing in Tasks.

`live=True` means the broker currently observes a corresponding held native
reservation. `live=None` means unknown; legacy or lightweight work may run
outside the broker. Stored completion is a journal observation, not a newly
verified original, transcript or provider result. Actual opening, resume and
export retain their owning service's checks.

The view excludes other or archived workspaces from a selected-library list.
A corrupt global journal without a validated workspace cannot be assigned to
that selection. Other malformed checkpoints show a fixed attention message
without original text, provider profiles, credentials or raw exception bodies.
Incremental discovery counts every encountered directory entry, including invalid
names and empty job directories, within a shared 10,000-entry limit. Cancellation
is checked between entries; the returned row count has its own explicit limit.

## Backup and execution evidence

Only the non-secret policy under `state/operations/resources.json` is durable.
The existing complete backup includes it. Live leases and coordinator files
remain under `runtime/resources` and are not restored as active reservations.
No new backup version or job migration is introduced.

The focused regressions retain donor native lock contention, process death,
policy/disk/device checks, Studio output and reuse, metadata cleanup and UI
lifetime cases. Added fixtures run the **actual accepted ASR service** with its
existing synthetic protocol worker while another process owns a real Studio
reservation. They cover successful serial admission, cancellation before launch,
and an owning Studio process dying at a controlled point before a render child
exists. That last case verifies lock recovery, not orphan-child containment.

Other cases change the workspace, retained source, reviewed worker or selected model root
while admission is blocked; require actual child reaping before lease release;
retain completed receipts after cleanup notices; and verify workspace mappings
and source-bound translation views without provider calls. These are authored
fixtures, not real recordings or model inference.

Source and installed execution are reported against exact commits in the PR/CI
record. A static review or prepared test is not a PASS result. The native wx helper
separately executes named controls, policy save, foreground close while queued and
actual parent-driven destruction. Human Windows 11/NVDA, acoustic quality,
hardware capacity and long-model benchmarks remain distinct, unclaimed gates.
