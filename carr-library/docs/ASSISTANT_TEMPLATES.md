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
