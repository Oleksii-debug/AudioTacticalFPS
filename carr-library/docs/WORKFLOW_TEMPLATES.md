# Named workflow templates and typed keyboard macros

Workflow templates save reusable settings for the existing local assistant, Studio projects and document batches. Each use opens a new review. A saved template has no permission to read a folder, overwrite an output, spend a provider budget or execute a job.

## Use a saved workflow

Open **Tools → Workflow templates and macros…**. Choose a named workflow, enter the values for this use and choose **Review new use…**.

For assistant and project templates, the existing local assistant opens with the typed request. Select the current library, input directories and output directory, then choose **Preview plan**. Inspect the concrete operations and their current scope before choosing **Run / resume**. Preview does not create a queued job.

For a document-batch preset, the existing document-batch window opens with split, naming, grouping and production settings. Source files, destination, captured source identities, output approvals and completed-job status are cleared. Choose the current sources and destination, prepare them, inspect the exact split and output names, approve that preview and then run the batch.

A template can also be created from:

- **Save as template** in the local assistant, after reviewing a typed plan or selecting a saved job.
- **Save batch preset** in the document-batch window.
- **Use Studio project…** in the workflow window, after choosing a saved project JSON.

These actions copy settings for review. They do not run the original job.

## Typed parameters

A parameter replaces one complete JSON value. It cannot supply a tool name or execute an expression. For example, this assistant definition uses the current query:

~~~json
{
  "steps": [
    {
      "tool": "library.search",
      "arguments": {
        "query": {"$parameter": "query"}
      }
    }
  ]
}
~~~

Its parameter declaration is:

~~~json
{
  "query": {
    "kind": "text",
    "label": "Search in the currently selected library",
    "example": "railway"
  }
}
~~~

For a particular use, enter:

~~~json
{"query": "harbour"}
~~~

The supported parameter kinds are text, integer, number, boolean, path and voice. Each declaration has exactly kind, label and example. Examples validate the saved structure; they are not automatic defaults for execution. **Use example values** copies them only after an explicit action.

Every declared parameter must occur in the definition. Missing or extra values, unknown operations, non-finite numbers, unbounded iterators and unsupported project or batch fields are rejected. Template names and operation names are separate: a friendly name never changes which fixed typed operation is allowed.

### Per-item settings

The **Per-item overrides** field accepts an array of parameter objects. Each item is merged with the explicitly entered common values. For the query above:

~~~json
[
  {"query": "railway"},
  {"query": "harbour"}
]
~~~

The assistant expands this into a single new, bounded plan with two concrete search steps. The existing plan budget still applies; a template cannot enlarge it.

The same mechanism can parameterize typed project fields such as role voice IDs, language or output format. It does not bypass installed-voice checks, source admission, clip validation or the current output scope. A document-batch template with several item settings asks which item to open in the existing batch review. It does not silently enqueue several batches.

Document output names still use the existing document-batch naming-pattern validator. Ordered DSP presets remain in the existing effect-preset store; workflow templates do not introduce another effect engine or preset format.

## Project identity and source provenance

A reusable project receives a new project ID and revision on every use. Old source references are removed when a project is captured as a template. They cannot become quotation authority in a different project.

Retained clip paths are declarations only. The new plan must explicitly admit the current input directories, and the existing production service checks the actual inputs and selected voices when used. A template does not carry old input approval, destination consent, resource leases or a completion receipt.

## Keyboard macros

Saved templates appear under **Tools → Saved workflow macros**. Their command IDs have the form workflow.template.<template-id>, registered with the same command manager as the other application commands.

Assign a shortcut through the existing keyboard settings. A macro opens the selected workflow for a new review; it does not record keystrokes, execute Python, launch a shell or run the plan automatically. Native editing, operating-system and screen-reader reserved combinations retain their existing protection. The command palette sees these same registered commands.

If the library changes while a menu from the previous library still exists, that old macro refuses to open it. Reopen the workflow view for the current library. The saved shortcut and template bytes can be backed up without executing either.

## Command line

The root command is workflows. Its direct module is carr_library.assistant.templates_cli; both use the existing installed-entry ownership mechanism when portable managed installation is enabled.

Save the query definition and parameter declaration above as query-definition.json and query-parameters.json, then use:

~~~text
python -m carr_library workflows --data-dir LIBRARY save --name "Named search" --kind assistant --definition query-definition.json --parameters query-parameters.json
python -m carr_library workflows --data-dir LIBRARY list
python -m carr_library workflows --data-dir LIBRARY show TEMPLATE_ID
~~~

Save explicit current values in query-values.json, then request or preview a new use:

~~~text
python -m carr_library workflows --data-dir LIBRARY request TEMPLATE_ID --values query-values.json
python -m carr_library workflows --data-dir LIBRARY preview TEMPLATE_ID --values query-values.json --workspace CURRENT_WORKSPACE_ID
~~~

The first command prints a typed review request without a scope. Save that JSON explicitly as a new local instruction file if you want to use the existing assistant CLI. Supply the current scope again there:

~~~text
python -m carr_library assistant --data-dir LIBRARY --instructions reviewed-request.json --workspace CURRENT_WORKSPACE_ID
python -m carr_library assistant --data-dir LIBRARY --instructions reviewed-request.json --workspace CURRENT_WORKSPACE_ID --run
~~~

Only the final command requests execution. Relevant workflows also need fresh --input-root, --output-root, --allow-create or --allow-full-backup arguments. A missing permission is an error; the template does not supply it.

Other settings-only commands are from-plan, from-project, from-job and batch-preset. There is no workflow-template run command or independent workflow executor. Replacing a saved template through the CLI requires both its ID and the exact previously reviewed SHA256 using --replace and --expected-sha256.

## Persistence, limits and interrupted saves

The passive producer stores exact typed records at state/workflows/templates/<32-lowercase-hex-id>.json. Each record is limited to 256 KiB; the collection allows 64 templates. One template has at most 32 typed steps, 64 parameters and 128 explicit per-item rows. JSON depth and total node count are bounded. Expansion must fit the currently selected assistant step budget.

A save compares the previously reviewed identity and revision under the existing state lock. A conflicting edit is preserved. If an interruption occurs after publication and bounded readback finds the exact intended bytes, the message reports that the template is present but completion could not be confirmed. That message does not assert that the lock was released or that the interrupted caller alone wrote those bytes.

The existing common backup validates the exact template schema, filename identity and collection bounds. Restoration copies those bytes and the existing keyboard preferences as passive data. It does not create a job, resolve an external input, discover a voice, launch a process or inherit an approval.

## Qualification

The implementation uses the existing plan, project, batch, command-manager, state-lock and common-backup contracts. Its focused regressions exercise actual producers and saved bytes, including CAS conflicts, post-publication interruption, no authority inheritance, backup/restore and pending native-owner disposal.

The installed Windows acceptance script is scripts/windows_workflow_templates_smoke.py, run with -I -B -X utf8 and no additional arguments. It uses actual wx keyboard/menu dispatch, the actual shared desktop executor and one actual local assistant job. It also holds a real template-list result until direct or parent-driven native destruction has completed, then checks the late completion.

A source review or a saved script is not runtime qualification. The exact integrated commit still requires the portable, installed-package and native CI gates. The scripted native evidence does not claim physical keyboard, NVDA, private model, speech or translation-quality acceptance.
