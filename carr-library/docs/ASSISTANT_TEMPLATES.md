# Named assistant workflows, batch presets and typed macros

These catalogue entries reuse the current local assistant's typed plan and jobs.
A workflow, batch preset or keyboard macro is an ordered sequence of existing
allowlisted operations. The category is a label for finding the intended
sequence; it does not introduce a second queue, a scripting language, OS
keystroke injection or unattended execution.

## Save and use

1. Open **Tools → Local assistant**, choose the current library and input/output
   folders, write the guided command or typed JSON, and choose **Preview plan**.
2. Inspect the actual operations, source references, output names and selected
   scope. Enter a template name, choose its category, and choose **Save reviewed
   template**. Replacing an existing entry requires a confirmation whose default
   is No. A new name saves a separate copy. Saving a template does not submit or execute the plan.
3. Choose **Named templates…** and select the saved entry. Its editable typed
   steps are loaded into the existing goal field. Input/output folder approval
   and complete-backup permission are cleared; model planning is not contacted.
4. Select the current scope and folders, edit any old source references or
   output names, then choose **Preview plan** again. Only the separate **Run**
   action submits a fresh job. Existing jobs still use **Saved jobs** and their
   original reviewed checkpoint; template loading never resumes one implicitly.

**Tools → Named assistant templates…** has semantic command ID
`assistant.templates`. Assign a shortcut through the existing keyboard settings
or invoke it from the existing command palette. It opens the same selector and
review flow; a shortcut does not authorize saved operations. No default key
replaces another command.

A batch preset can contain an existing multi-input conversion, import or other
typed operation, or a bounded sequence of such operations. A typed keyboard
macro uses precisely the same allowlist. Neither form accepts shell commands,
Python, arbitrary URLs or hidden executable callbacks.

## What is retained

The single private catalogue is
`state/assistant/workflow-templates.json`. It contains schema/revision, stable
entry IDs, names, categories, summaries and typed steps only. It does not contain
a Scope, job ID, job status, approval, results or credential fields. Existing
step references, including a source path or a reviewed content hash, remain
visible data; they are never rewritten into authority for a different source.

Loading is passive. Fresh preview and submission use the current
`AssistantService.validate`, source/path policies, runtime availability and
selected budgets. A restored source path may no longer exist; choose and review
the intended input instead of silently retargeting it. Live-reader player
operations still require the current qualified reading-window owner and exact
session/source identity.

The catalogue permits at most 256 uniquely named entries, 10,000 typed steps per
entry and 8 MiB total encoded JSON. The current plan's own step/file/time/byte
budgets remain authoritative and may be stricter. Catalogue edits use the
existing atomic state writer, a producer lock and exact snapshot CAS. A stale
window must reload instead of overwriting a newer revision.

Full backup validates this exact passive shape without opening saved external
paths, loading a model, resolving a vault or executing a plan. Restore preserves
the catalogue, including its non-authorizing references. The exact producer lock
is excluded from backup; unknown state files are still rejected.

## Validation scope

`tests/assistant/test_workflow_templates.py` covers real catalogue changes,
fresh scope/permission review, separate new jobs, stale-window conflicts,
typed-macro rejection and an actual full-backup/restore round trip.
`tests/assistant/test_workflow_template_handlers.py` exercises the real dialog
closures and semantic keyboard binding with native-control stand-ins.
The existing `scripts/windows_assistant_smoke.py` additionally saves and reloads
a template using real native controls, verifies zero implicit jobs and requires
a fresh preview before Run.

This component's newly authored source and tests require execution on the
composed CI tree. Native screen-reader acceptance, actual model quality and
paid provider qualification are separate evidence; these tests claim none.


## Typed bindings, repeated items and project capture

The same catalogue now accepts optional parameter declarations on a record.
Old records keep their original five fields and load without rewriting their bytes.
Parameters replace an entire argument value through `{"$parameter":"name"}`.
Their declared kinds are text, integer, number, boolean, path and voice. Tool
names stay fixed; there is no expression language, command interpolation, key
recording, arbitrary code or inherited Scope.

In **Typed parameters / project...**, review the fixed step JSON and parameter
declarations (kind, accessible label and example). Examples validate the recipe
shape and do not become accepted use values. Loading a parameterized recipe
opens a separate value review. You can explicitly copy examples, enter current
values, or supply up to 128 ordered per-item override objects. Every declared
value must be supplied. The existing current Scope step budget and an aggregate
8 MiB expansion limit still apply.

Use **Use Studio project JSON...** to explicitly select a saved project.
Project data is parsed by the existing Project type. Both `audio.produce` and
`audio.render` captures remove old line `source_ref` claims. Each new use
receives a fresh project ID and revision 1. Voice, scene, cue, timing and effect
settings remain editable data. Retained cue paths need freshly selected input
scope; no media bytes, old approval, completed render, voice/model authority or
output destination is inherited. Legacy saved audio recipes receive this same
lineage removal only when instantiated; passive load/backup leaves their bytes
unchanged.

Existing canonical Book batches remain the owner of document source splitting,
per-book settings and retained original checks. Repeated assistant templates
expand into ordinary typed steps; the donor's separate document-conversion
queue/catalogue was not installed.

## Per-template semantic commands

Each saved recipe exposes `assistant.template.<stable-id>` in the existing
keyboard manager. The command has no default key. Assign a key through the
existing Keyboard shortcuts dialog; all its conflict and reserved-key checks
still apply. A command opens the current recipe's value/review window. It never
executes a saved job. Deleting a recipe disables its menu command; a stale queued
callback rechecks the current catalogue and library and cannot run it.

A library change does not retarget a previously registered macro. Reopen the
application for that library's command inventory. Names in an already registered
command remain stable for that window; edited recipe names are shown in the
actual review and on next command inventory creation. Catalogue names retain
NFC/whitespace/case normalization and exact current-byte compare-and-save.

## CLI through the existing installed entry point

```text
python -m carr_library workflow-templates --data-dir ROOT list
python -m carr_library workflow-templates --data-dir ROOT show ID
python -m carr_library workflow-templates --data-dir ROOT save --name NAME --steps STEPS.json --parameters PARAMETERS.json
python -m carr_library workflow-templates --data-dir ROOT from-plan --name NAME --plan PLAN.json
python -m carr_library workflow-templates --data-dir ROOT from-project --name NAME --project PROJECT.json --tool audio.render
python -m carr_library workflow-templates --data-dir ROOT request ID --values VALUES.json --items ITEMS.json
python -m carr_library workflow-templates --data-dir ROOT preview ID --values VALUES.json --workspace WORKSPACE
```

A replacement or deletion requires the exact `catalog_sha256` returned by
list/show. A plan capture discards its old scope without reopening it. Request
produces portable proposal data; preview applies a freshly selected scope and
existing AssistantService validation. This CLI has no run/execute command. Use
the ordinary assistant review and explicit Run for a new job.

The typed-binding and editor logic reuse the reviewed prepared
`c8baf30c` implementation (donor source blobs `5891de9e` and `949fe358`)
through the current single catalogue/Plan/semantic registry. No donor
`state/workflows` directory or alternate batch executor is created.

## Additional acceptance

`tests/assistant/test_workflow_template_parameters.py`,
`test_workflow_templates_cli.py` and `test_workflow_template_editor.py`
cover real catalogue/backup, fresh project IDs for both aliases, source-ref
removal, explicit clip scope, ordered bindings, early structure/budget rejection,
default-No replacement, stale native callbacks, current semantic registration,
and no job creation before separate submit. Existing template/handler tests
remain required. `scripts/windows_workflow_templates_smoke.py [--installed]`
uses actual wxMSW controls and the real semantic accelerator, plus typed project
editing; installed mode verifies current source hashes and import origins.

New convergence execution is NOT_RUN until its exact CI head completes. Manual
NVDA and voice/acoustic quality remain separate qualifications.
