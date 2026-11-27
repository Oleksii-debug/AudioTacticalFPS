# Shared runtime resource admission

The resource broker reuses the native-lock implementation reviewed in
[PR143, e6225630](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143/commits/e6225630f88c3aa85814e488da863345d10faa15).
Its models, locks, broker and two resource suites are retained from that exact
revision. The canonical package initially exposes only the resource core;
service integrations are separate changes against their current contracts.

`ResourceBroker.for_app(data_root)` shares a coordinator in
`runtime/resources` and saves the operator policy in
`state/operations/resources.json`. Temporary reservations remain outside backed-up
state. All cooperating services must use that exact root and policy. The
reviewed backend is the existing pinned `filelock==3.29.0`; unsupported native
locking fails visibly and never falls back to soft lock files.

Adapters declare CPU slots, estimated RAM, configured GPU capacity, estimated
peak disk bytes on selected volumes, and exclusive canonical device IDs. The
broker also checks actual free disk bytes. These are admission estimates,
not measured RAM/VRAM utilization or operating-system resource limits. A GPU
without explicit configured capacity fails; no device fallback is selected.

Waiting is cancellable and bounded. Active cancellation and timeouts are stop
signals: the owning adapter must finish child-process and device cleanup before
leaving the reservation. A live reservation is never revoked because its PID
looks stale or time elapsed. Native lock release determines crash recovery;
the next admission or snapshot reclaims unlocked records. The broker does not
kill orphan engine descendants and is not a network or security sandbox.

Use `ResourceLimits`, `ResourceRequest` and
`with broker.reserve(request, operation_id=..., cancelled=..., timeout=...)`.
The lease exposes `cancelled()` and `checkpoint()`. Policy changes cannot
invalidate active reservations. Snapshot output labels usage as declared
reservations, not hardware measurements. Native Windows acceptance remains
separate from portable source tests.

## Canonical Studio adapter

`ProductionService` uses this app's coordinator lazily. A completed job verifies
its existing artifact receipt and returns without constructing a runtime or
requesting capacity. A caller-supplied broker must have the exact canonical app
root and policy path. Only an unspecified default audio thread count is adapted
to the saved CPU allowance; explicit runtime budgets remain unchanged.

Pending work records `queued` before cancellable admission. Selected clip
inspection takes a short CPU/RAM reservation, then releases it before one atomic
cache/output-volume reservation. Disk estimates include scene durations, pauses,
estimated speech duration, measured imported audio after trim and placement,
and both speaker and clip-role stems. Source hashes and job/output identity are
checked again after waiting; a wait grants no new source authority. The estimate
does not claim speech/model throughput or physical memory qualification.

Pause/cancel acknowledgement preserves the original typed outcome. Genuine
source/render errors remain failures even if a stop arrives concurrently. The
render lease encloses existing synchronous engine cleanup and remains held when
an interrupted child is reaped. This does not measure or reclaim memory retained
by an independently managed persistent engine service after a request ends.

Portable acceptance includes actual FFmpeg clip trim/placement/stem output,
completed receipt reuse with a fully occupied pool, explicit GPU policy rejection,
source mutation before render admission, queued pause/cancel and actual child
termination while the reservation is still held. Windows native gates remain
separate evidence.
