# Saved workflow templates and finite batches

Resonance can retain named sets of the assistant's existing typed operations. A template is reusable task data. It does not carry library, input-folder, output-folder, full-backup or execution permission.

## Open and save

Open **Tools → Saved workflow templates**, or open **Local assistant** and choose **Templates**. The same command appears in the existing command palette and can be assigned a shortcut through the current keyboard settings.

To create a template, first select the current library and allowed folders in the assistant, enter a supported goal and choose **Preview plan**. Review the operations. Open **Templates**, choose **Save plan as new**, and enter a name. Selecting an existing saved assistant job also allows its steps to be saved as a template; the original job and its approval remain unchanged.

Saving a template does not submit a job, start a model, read the paths inside its arguments or execute a tool. A name identifies the settings; each update increments the revision. If another window changes the same record, refresh and review it before updating, removing or exporting it.

## Load and run

Select one or more templates in the native list and choose **Load selected as draft**. Selected templates are combined in the order displayed. This produces a finite sequence of the existing typed steps, with a maximum of 128 steps. There are no arbitrary loops, scripts, shell commands or executable keyboard macros.

The assistant opens the resulting JSON as an editable, unapproved draft and disables **Run**. Choose the current library and input/output folders, edit literal paths or names as needed, then choose **Preview plan**. Review the complete sequence before separately choosing **Run / resume**.

The normal assistant checks remain in force: allowed tools and argument types, selected library, input and output paths, time/file/byte budgets, required runtimes and no-overwrite publication. A template cannot enlarge those permissions. Combining templates that each create a library, or that require incompatible current permissions, can be rejected at Preview. A batch is one ordinary assistant plan with its existing checkpoints and cancellation behavior.

## Update, rename, remove and exchange

**Update selected from plan** replaces exactly one selected template with the currently reviewed or selected saved plan. **Rename selected** changes its name and revision while retaining its steps. Removal deletes only the template; existing jobs, source documents and outputs remain.

**Export selected** writes one versioned JSON template through the normal destination confirmation. It contains the literal text, tool arguments and paths in the template. Review that content before sharing it. Scope, execution approval, credential-vault contents and job checkpoints are not copied into the export.

**Import template** validates a selected local JSON file and stores it under a new identity at revision 1. It cannot overwrite another template or restore an execution approval. If the imported name is already used, choose a distinct name before creating the additional record. Unsupported tools, unknown authority fields, non-finite values, duplicate JSON keys and linked files are rejected.

## Recovery and limits

Complete library backups include the exact bounded template records. The legacy standalone state route validates the same records on already copied files. Restoration neither opens paths stored in tool arguments nor starts an operation. The user still selects current permissions and follows Preview → Run.

The inventory supports at most 256 templates, 8 MiB per record and 64 MiB in total. These are safety limits, not a measured large-catalog performance claim. A damaged or unknown record is reported instead of silently discarded. Runtime locks and incomplete temporary writes are not portable template records.

A save or removal can already be present if the filesystem reports a later durability error. In that case the application explicitly asks the user to reload the inventory before another change. An unrelated create-only competitor remains a conflict; its file is never claimed as this operation's publication.

## Qualification

Source review, regression execution, installed Windows controls and manual NVDA operation are distinct. The implementation uses the assistant's existing worker, service, typed executor, durable JSON primitives, command registry and export boundary. It creates no new queue, planner or catalog.

The automated native scenario saves through real controls, reloads the template, checks that old approval is cleared, requires a fresh Preview and separate actual Run, and reopens the exact saved revision. Its final report identifies the tested source and installed module bytes. A source review or this guide alone does not establish that the scenario passed. Manual NVDA and any model-specific output quality still require their own evidence.
