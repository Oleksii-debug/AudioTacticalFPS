# Optional foreground scheduling of reviewed local assistant jobs

The existing local assistant can save a concrete UTC start window for a reviewed,
never-started job. Saving a time does not execute or arm it. In the native assistant,
open **Local schedules**, read the exact plan, scope, plan hash and time window,
then choose **Arm selected for this window**. Up to 32 concrete jobs can be armed
in the open window. The same current assistant executor and progress view perform
at most one due job at a time.

**Disarm this window**, Pause during a scheduled action, closing the assistant,
a changed library identity, or UTC clock rollback removes the live arming.
There is no operating-system task, startup hook, service, daemon, remote trigger
or periodic automatic replay. For repeated work, explicitly approve separate
jobs and concrete time windows. The UI timer performs only an in-memory due check;
journal reads, runtime admission and execution remain on the existing worker.

## Durable state and recovery

The optional `schedule` object is part of the current `carr-assistant-job/1`
record, validated as `carr-assistant-schedule/1`. Unscheduled jobs keep the same
serialized keys. No second scheduling database or backup producer is added.
The current job lock, state write guard, complete backup and restore validator
remain authoritative.

A schedule binds the exact plan SHA256, the live library path and native directory
identity, UTC due/expiry and its event history. The foreground session additionally
pins the exact schedule hash. Before the first step, the existing run transaction
publishes a `dispatching` claim under that job's actual native lock. It then uses
the ordinary tool dispatch, source/runtime checks, selected permissions, operation
coordinator, output validation and journal.

A missed window becomes Attention without execution. A crash, rejected lock,
uncertain publication or interrupted attempt is not automatically dispatched
again. Inspect the retained job and choose ordinary Run/Resume explicitly; that
action disables the old schedule. Existing deterministic action/checkpoint recovery
still applies. A successful schedule is complete only when the ordinary assistant
job and all its steps are verified complete.

Complete backups retain the exact optional metadata, including a self-backup's
in-progress intent. Restored or moved permissions remain inert. Nothing in backup
verification or restore arms a foreground session or activates archived paths.
The new typed evidence-search result is also checked against the actual saved
notebook evidence ID and checksum, in addition to its historical source binding.

## Native workflow

1. Select the current library, input/output folders and budgets.
2. Preview a guided or typed plan. Local-model proposals still need their separate
   explicit current-scope preview; a proposal cannot arm a schedule.
3. Choose **Schedule reviewed plan...** and review the exact plan again. Enter an
   explicit UTC time and a start-window allowance from 1 to 86,400 seconds.
4. Choose **Save time only**. If the plan was new, its committed queued job ID is
   retained even if a later schedule publication fails.
5. Open **Local schedules...**, inspect the saved plan and choose
   **Arm selected for this window**.
6. Keep the assistant open. Existing Run/Resume, Pause, saved jobs and recovery
   remain available. Closing the window disarms live scheduling.

Time choices must be within 366 days. The native exact-plan review has a 256 KiB
display allowance; larger accepted plans can be reviewed with the existing CLI.
Arming reads and revalidates the selected journal off the event thread. A changed
scope, job, root or reviewed hash is rejected before dispatch.

## CLI

First save an explicitly reviewed current plan without execution:

`python -m carr_library assistant --data-dir LIBRARY --instructions plan.json --allow-create --queue`

The command prints the committed job ID, exact plan and `plan_sha256`.
Use the appropriate current scope flags for other tools; `--queue` cannot be
combined with `--run` or an unreviewed local-model proposal.

List schedules and their full plans without arming:

`python -m carr_library assistant schedule --data-dir LIBRARY list`

Save a concrete UTC window:

`python -m carr_library assistant schedule --data-dir LIBRARY create --job JOB_ID --plan-sha256 PLAN_SHA --at 2026-12-01T12:00:00Z --lateness-seconds 3600`

Replacing a never-started prior slot also requires its exact
`--previous-schedule-sha256`. This still does not arm the job.

Explicitly arm one reviewed slot in a bounded foreground invocation:

`python -m carr_library assistant schedule --data-dir LIBRARY run --job JOB_ID --schedule-sha256 SCHEDULE_SHA --run --wait-seconds 600`

Wait is finite, from zero to one day; default zero checks once. It exits 0 only
after verified job completion, 130 on cancellation, 3 when no selected slot
became due during the foreground wait, and nonzero for Attention/failure.
Every exit disarms that invocation. SIGINT reaches the same existing service
cancellation callback and retained checkpoint.

Disable a stored time without execution:

`python -m carr_library assistant schedule --data-dir LIBRARY disable --job JOB_ID --schedule-sha256 SCHEDULE_SHA`

## Verification boundary

New source suites:

- `tests/assistant/test_typed_local_tools.py`
- `tests/assistant/test_scheduling.py`
- `tests/assistant/test_scheduling_cli.py`
- `tests/assistant/test_scheduling_handlers.py`

Existing `test_local_assistant.py`, `test_state_validation.py`,
`test_canonical_backup.py`, `test_planning_handlers.py` and
`test_dialog_lifecycle.py` remain compatibility gates. The latter explicitly
checks that closing disarms the session, stops its native timer and retains the
existing worker cancellation/completion behavior.

`python scripts/windows_assistant_schedule_smoke.py --output REPORT.json`
exercises actual installed wx controls, exact review, save-without-run, separate
arming, a real UTC timer, one ordinary local job, verified completion and a later
tick without repetition. It uses no remote model, microphone, voice or private
source. Report creation itself is not execution evidence.

This candidate was prepared while the executor was unavailable. New source tests,
installed gates and native acceptance are **NOT_RUN** until their corresponding
exact source candidate is executed. No OS scheduler or unattended restart
qualification is claimed.
