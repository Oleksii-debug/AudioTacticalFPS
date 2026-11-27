# Named assistant workflow templates

The existing Local assistant now keeps named, reusable typed drafts. Open **Tools → Local assistant → Templates…**. The same window remains available from an explicitly selected live reader through its existing assistant action.

## Review and use a saved workflow

1. Preview a plan using the ordinary assistant controls, or choose an existing saved job.
2. Open **Templates…**, choose **Save plan as new**, and give it a distinct name.
3. Select one or more saved workflows and choose **Load selected as draft**. A batch follows the order shown in the list and expands to at most 128 ordinary steps.
4. Choose the current library and input/output folders, inspect literal paths and arguments, and choose **Preview plan**.
5. Only the separate **Run / resume** action may submit the newly reviewed plan.

Loading clears the previous dialog's selected workspace, folder permissions, complete-backup permission, and budgets. It never restores an old approval or submits a job. Templates can contain literal source paths, text, hashes or native-session identifiers as ordinary arguments. These are not permission grants: current preview checks the current source, scope and native target. A stale reader session must be replaced with a newly selected target before a corresponding interactive plan can run.

The settings window also supports **Update selected from plan**, **Rename selected**, **Remove selected**, **Import template**, and **Export selected**. Update, rename, removal and export check the exact reviewed stored revision. Import creates a new local identity. A newly selected export destination is create-only; replacing an existing export requires confirmation bound to that exact destination.

Writes use the assistant's existing background executor, application writer and publication lock. Closing the view cancels uncommitted work. A write that already committed remains completed if its view closes before the UI callback. Save All waits for an outstanding explicitly requested settings write; it does not run a workflow or replay the write. Shared progress receives the result after the service settles. A failed directory sync after publication is reported as a present change with unconfirmed durability and must be reviewed before another attempt.

## Command line

Use the existing assistant command. Put general options such as `--data-dir` before `--templates`; everything after that flag belongs to the named-settings command.

```text
research-library-assistant --data-dir C:\Library --templates list
research-library-assistant --data-dir C:\Library --templates save "Daily search" --draft C:\Drafts\search.json
research-library-assistant --data-dir C:\Library --templates save "Reuse saved steps" --from-job JOB_ID
research-library-assistant --data-dir C:\Library --templates load TEMPLATE_ID
research-library-assistant --data-dir C:\Library --templates rebind TEMPLATE_ID --workspace WORKSPACE_ID
research-library-assistant --data-dir C:\Library --templates update TEMPLATE_ID --draft C:\Drafts\revised.json
research-library-assistant --data-dir C:\Library --templates rename TEMPLATE_ID "New name"
research-library-assistant --data-dir C:\Library --templates export TEMPLATE_ID C:\Exports\workflow.json
research-library-assistant --data-dir C:\Library --templates import C:\Exports\workflow.json --name "Imported copy"
research-library-assistant --data-dir C:\Library --templates remove TEMPLATE_ID
```

`load` prints an unapproved JSON draft. `rebind` previews that draft against explicitly chosen current permissions and prints the plan; neither command submits or executes a job. A separate ordinary `--instructions FILE --workspace ID --run` invocation is still required for execution. `--run` is rejected for template commands. `--replace-existing` is an explicit option of `export`, with the same immutable destination check as native exports.

A minimal draft for saving is:

```json
{
  "schema": "carr-assistant-plan/1",
  "steps": [{"tool": "library.list", "arguments": {}}],
  "summary": "List the currently available libraries"
}
```

Only the existing typed tool allowlist is accepted. No script, shell, executable macro or background scheduler is created.

## Storage and recovery

Records use `carr-assistant-template/1` under `state/assistant/templates/<32hex>.json`. They contain only identity, name, revision and a draft of typed steps plus summary. Scope, credentials, execution approval, native-dispatch receipts and replay intents are not accepted as record fields.

The collection allows at most 256 records, 128 steps per draft, 8 MiB per record and 64 MiB in total. The existing 64 MiB assistant job/control budget remains separate. Unknown committed files, links, duplicate names and malformed records fail closed. Only exact uncommitted atomic-write scratch names are excluded from committed inventory.

Complete backup/verify/restore includes the typed records through the existing assistant namespace. Incremental snapshots inherit that same verified full-backup route. Restored templates remain inert. Foreign historical state stays in its existing inert archive; explicitly importing a selected compatible template record creates a new validated local identity.

Native controls, functional tests and installed-package qualification are tracked separately from language-model quality or human screen-reader testing. No number of supported or qualified languages is inferred from this feature.
