# Tasks and resources: current-store integration

This adapter uses the existing resource broker and current managed job journals.
It creates no second production, speech, assistant, translation or transcription
queue. Source attribution is PR143 d60d91c's OperationFacade/controller/probe,
adapted to the accepted 8a stores and the e1a830 broker.

## What a row means

| Kind | Current durable source | Progress unit | Controls in this shared view |
| --- | --- | --- | --- |
| Assistant | state/assistant/<id>.json | Completed approved steps | Existing pause/cancel methods, after the service's scope and reapproval checks |
| ASR | transcription-jobs/<id>/job.json and execution.json; correlated job_status.json | Input files, when current-run counts are present | Existing pause flag; Cancel maps to the worker's stop flag |
| Audio Studio | audio-projects/<id>/audio/render-journal.json | Stored render checkpoints; total unknown | None; this renderer has no durable shared pause controller |
| Document | import-requests/<id>.json | Stored result records, if present | None; use the current import workflow |
| Import batch | import-batches/<id>.json | Completed, duplicate or excluded files | None; use Import centre |
| Translation | state/translation/<id>.json | Accepted chunks | None; use the existing translation workflow and approval |

Status is the recorded journal status. Every row explicitly carries
verification=not_performed. A recorded complete row does not prove its original
or generated files still exist, approve a paid request, or qualify recognition,
translation, music or speech quality. Use the owning workflow to validate results.

A live=True marker requires a matching currently locked broker reservation.
The correlation IDs are asr:<32hex>, studio:<32hex> and document:<32hex>.
An absent lease leaves live=None: older workflows, non-heavy steps and external
workers may not reserve through this broker. Absence is not process-death proof.

ASR listing validates only the relative immutable job definition and execution
metadata. It never reads source audio, model/runtime files or transcript content.
A job_status record contributes counts only when protocol, run_id and known
input total match the current execution. A pause/cancel response means
requested=True, acknowledged=False. The worker records its own settled state.

The translation adapter calls the pure current translation validator; it
constructs no gateway, credential vault, budget ledger or network request.
Archived assistant approvals and moved translation root bindings remain inert.
No resume, resubmit, delete or force-stop action is added.

## Bounded metadata and privacy

Discovery counts every directory entry before filtering job names, with a total
limit of 10,000. A refresh reads at most 256 MiB of checkpoint metadata; individual
stores retain their current smaller limits. Cancellation is checked between
entries and reads. Large individual pure validators do not claim an interrupt
inside every Python expression.

The facade rejects symlinks/reparse points and hard-linked checkpoint files via
the existing local-file guards. A damaged recognized job becomes a fixed
attention row without dumping its JSON or exception body. Directory-wide
integrity/budget failures stop the refresh visibly.

Rows contain stable job/workspace IDs, fixed phase descriptions, counts,
timestamps, a relative checkpoint name and capabilities. Original text, provider
profiles, opaque credential references, raw source/output paths and messages are
not copied into the dashboard. Resource snapshots expose declared allowances and
reservation quantities, excluding disk paths, owner tokens and arbitrary
operation labels. An unrecognized lease label is reported as other. The UI
redacts worker errors before the shared main-frame error callback can print them.

## Native window and resource allowances

The Tasks and resources window uses the existing frame.run_job executor, native
wx notebook/list/text/spin controls, the shared Tools menu and command registry.
Construction does no filesystem/model/provider work. Resource initialization
and metadata reads run on the host executor.

While that same frame owns another operation, its existing controls remain the
place to stop it. The task window does not start a second hidden executor.
It can inspect reservations and request supported controls for jobs running in
another process when the host frame is idle.

Closing during a task-view request sets its own cancellation event, leaves an
unrelated frame cancellation flag unchanged, and waits for the owned operation
to return. An independently posted settlement handles the case where the host
suppresses its ordinary result callback after a library switch. Closed/stale
callbacks cannot update native controls.

CPU slots, declared RAM, temporary disk, free disk reserve, GPU allowances and
reservation count are explicit cooperative policy. They are not measured host
capacity, RAM enforcement, GPU enumeration or a native process sandbox.
Unlisted GPUs remain unavailable. Active reservations cannot be forcibly revoked
by shrinking a policy. Unedited MiB/GiB fields preserve their original exact byte
allowances instead of silently rounding a saved policy upward.

## Complete backup

Only state/operations/resources.json is added as a portable typed sidecar, with
the exact carr-resource-policy/1 schema, ResourceLimits validator and 32 KiB
budget. The snapshot holds the same native admission gate used by policy updates
through the backup. New admissions may wait or time out while that snapshot is
active; existing execution leases are neither acquired nor revoked by backup.

Complete backup, restore and rebackup retain the declared allowances. Verification
uses pure file/schema checks and creates no coordinator or lock in the archive.
Unknown durable files in state/operations fail visibly rather than disappearing.

Runtime leases, their native locks and policy-location binding remain transient.
Machine-local state/audio/engines.json registrations and executable/model paths
remain excluded. Restoring allowances does not restore execution authority or
claim the receiving computer has the declared capacity.

## Integration hooks

The candidate is additive to broker base e1a830. It intentionally does not edit
desktop_app.py, __main__.py, pyproject.toml or workflow gates.

1. Add carr_library.operations to the explicit package inventory if the core
   broker composition has not already done so.
2. In desktop_app.py import install_operations_actions from
   carr_library.ui.operations_dialog and call it after the shared Tools menu is
   created. Installation is idempotent. Stable command ID: operations.tasks.
   Update both installed desktop AST fixture installer registries alongside it.
3. Route the top-level operations verb to operations_cli.main(argv[1:]).
   The module also runs directly:
   python -m carr_library.operations_cli --data-dir PATH list
   python -m carr_library.operations_cli --data-dir PATH resources
   python -m carr_library.operations_cli --data-dir PATH configure-resources POLICY.json
4. Keep the resource broker's original tests and add the suites below to both
   source and installed-product gates. Run the native probe on Windows against
   the installed package without adding source paths.

The library_backup.py delta is limited to the exact policy sidecar allowlist,
byte limit, inventory, native snapshot guard, pure validation and final inventory
recheck. Preserve the separately reviewed schema-V5 publication changes when
composing that file.

## Acceptance evidence

New source and regression tests are prepared, but have NOT_RUN status while the
execution environment is unavailable. No Python, Windows, installed-wheel or
manual screen-reader success is claimed by this source checkpoint.

Required focused suites:

- tests/operations/test_resources.py (existing broker core)
- tests/operations/test_facade.py
- tests/operations/test_policy.py
- tests/ui/test_operations_dialog.py
- tests/e2e/test_operations_cli.py

The adapted tests/ui/native_operations_smoke.py exercises the actual installed
DesktopFrame/wx modal, close during initialization, named/focusable controls,
invalid policy rejection, durable policy changes and deferred native destruction.
It reports NOT_RUN/exit 77 outside Windows. Even a future PASS for that probe
does not establish manual NVDA usability, measured hardware capacity, actual
ASR inference or any audio quality claim.
