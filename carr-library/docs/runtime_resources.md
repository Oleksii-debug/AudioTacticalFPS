# Shared runtime resource admission

The resource broker reuses the native-lock implementation reviewed in
[PR143, e6225630](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/143/commits/e6225630f88c3aa85814e488da863345d10faa15).
Its models, locks, broker and two resource suites were adopted from that exact
revision. The broker's policy-save method now additionally accepts a reviewed
previous policy and cancellation callback for the canonical native editor.
Service integrations are separate changes against their current contracts.

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

## Shared saved-operation view

The facade reuses PR143's bounded journal view, adapted to the canonical
`transcription.TranscriptionService`, its 32-hex job identities, and
`state/transcription/jobs/<id>/job.json`. It does not read live worker status,
launch engines, resume jobs, contact providers or verify recorded output bytes.
Completion is labelled as stored journal state. Actual output verification and
resume remain in each original workstation. A live broker reservation is
positive evidence of cooperative ownership; absence is unknown, not proof that
an engine stopped.

`python -m carr_library operations --data-dir <folder> list` lists saved jobs.
`get <kind> <id>`, `pause <kind> <id>` and `cancel <kind> <id>` use the same view
and existing service controls. `--workspace <id>` restricts authorized workspace
views. Controls recheck scope immediately before dispatch. Import and translation
workflows without saved-job control methods expose no invented control API.
An ASR control must additionally provide `--attempt-id <active_attempt_id>` from
the selected job and is rejected if that exact host attempt ended or changed.
The worker's result `run_id` is a different identity and is never substituted.

`resources` reports declared reservations. `configure-resources <policy.json>`
accepts the exact `ResourceLimits` fields, retains the last valid policy on
rejection, and cannot reduce allowances below existing reservations. No command
in this interface executes a plan, resumes a paused engine or approves an
external transfer. Record bodies, transcript text and provider credentials are
excluded from the shared view. Invalid checkpoints appear as attention entries
without copying their diagnostic contents into the dashboard.

## Native Tasks and resources

Open **Tools → Tasks and resources**, or find that existing menu action with
**Ctrl+Shift+P**. The native saved-job list and read-only details use the same
facade as the CLI. Refresh, pause/cancel requests and policy persistence run on
the desktop's existing worker. The close control remains available while a
view request waits; it cancels that request without cancelling a different
operation. The owning workstation remains the place to resume or approve work.
Opening a selected transcription verifies its saved results in that existing
workstation and does not start recognition.

The Resource allowances page exposes CPU slots, declared memory, temporary disk,
free disk reserve, exclusive GPU capacities and the maximum reservation count.
Its usage summary is the last refreshed declared reservation snapshot, not live
hardware telemetry. Policy save is conditional on the exact policy reviewed in
the window. A concurrent edit requires refresh; cancellation while waiting
preserves the previous policy. A successful durable save or control request
remains successful if its optional refresh later fails. Unchanged native byte
fields preserve their exact stored values even when the display rounds units.

`scripts/windows_operations_smoke.py` requires installed native Windows wx. It
opens the action through the existing command palette, sends real keyboard
press/release pairs to the owned foreground window, records pause/cancel through
the actual assistant store, rejects a concurrent policy edit, saves reviewed
limits, and closes a request waiting on a real native lock. It also checks
deferred native destruction. It reports NOT_RUN on other platforms and makes
no manual keyboard, NVDA, model-inference or measured-capacity claim.
