# Named typed workflow templates

The local assistant now keeps named reusable workflows in its existing state
namespace. The format reuses the canonical `carr-assistant-template/1`
record, including its existing identity, revision, name and typed draft. There
is one active assistant implementation and one complete-backup route.

## Review and run

Open **Tools → Local assistant**, select the current library and input/output
permissions, and Preview a plan. **Workflow templates... → Save plan as new**
stores its allowed steps and summary. The same dialog can rename, update,
remove, export or import a template. Import always chooses a new local identity.
Names must be distinct under case folding.

Select one or more rows and choose **Load selected as draft**. Multiple rows
expand in displayed list order into a finite batch of at most 128 steps. The
assistant clears the previously approved plan and job. Select current library
and folders, edit any literal paths if needed, then use **Preview** and **Run**.
Loading, importing and restoring do not execute a step.

A template never carries a saved Scope, execution approval, schedule, credentials,
resource lease or provider-operation receipt. Literal paths and text inside
allowed tool arguments are retained as data and may be sensitive; review a JSON
export before sharing it. Saved jobs can also supply their reviewed step text,
while their old root and approvals remain in the original job.

The existing typed operation list, current source/evidence checks and resource
admission still apply. A template is a named finite sequence, not an executable
script or an OS keyboard automation.

## State, concurrency and backup

Records live at `state/assistant/templates/<32hex>.json`. Save/remove uses
the application writer, current assistant state guard, and the original
`.workflow-templates.lock` in that order. A concurrent canonical template
writer conflicts with this same native lock. Reads never create the directory.

Each record is limited to 8 MiB, with at most 256 records and 64 MiB of retained
record bytes. Individual drafts and combined batches are bounded before full
expansion. Complete inventory rejects unknown files, directories and aliases.
Only the validated native lock and the producer's bounded private staging names
are omitted from snapshots.

Edits, deletion and export compare the exact reviewed template fingerprint.
New/imported identities publish without overwriting a competitor. Updates use
the current atomic JSON publisher. A final directory-sync or guard-release failure reports the
exact already-committed save/removal instead of claiming it was absent.

The existing complete-library backup validates this typed namespace and preserves
every original record byte. Its version4/5 path/sidecar layout remains unchanged.
Backup and restoration do not interpret old literal argument paths or activate
old permissions. Restored templates require a fresh current-scope Preview.
The current scheduler ignores nested template IDs when listing actual jobs.

## Native lifecycle

The template dialog borrows the assistant's existing executor. Queued and
completed work checks the current dialog/library/review identity. Closing cancels
further publication and revokes late UI callbacks; an already published record
stays saved. The dialog never shuts down a borrowed executor.

Both existing assistant work and template actions use a submission admission
token. If ThreadPoolExecutor enqueues an item and then fails to start a thread,
a later successful submission cannot accidentally execute the earlier rejected
mutation.

## Verification boundary

This source checkpoint was prepared while the executor was unavailable. The
new tests and native probe are **NOT_RUN** until CI records actual results.

Strict source/installed suites:

- `tests/assistant/test_workflow_templates.py`: real current plans, filesystem
  revisions, imports, bounds, cancellation, native-lock exclusion, exact-byte
  complete backup/restore/rebackup, and forged authority refusal.
- `tests/assistant/test_workflow_template_handlers.py`: existing worker/native
  handler seams, close/stale owners, real thread-start failure, committed export
  notices, and fresh parent-scope review before Run.
- `tests/assistant/test_scheduling_handlers.py`: the existing suite plus
  rejected-submission tests for both schedule saving and ordinary Run.
- Existing scheduling, assistant snapshot, complete-backup and desktop lifecycle
  suites must continue to run with their unchanged strict skip policy.

Native installed-product acceptance:

`python -I -B scripts/windows_assistant_templates_smoke.py --output <report.json>`

It drives actual wx save/name/rename/load controls, separately previews/runs the
loaded catalog step under a different current workspace, and checks exact-byte
complete backup/restore. It does not claim model inference, screen-reader
qualification, or native execution before a Windows report exists.
