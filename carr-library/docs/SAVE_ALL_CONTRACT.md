# Save All and close completion

The main File command and the explicit **Save all open drafts** buttons in the
reading editor and Audio Studio share one coordinator. They use the existing
research-note transaction, NarrationStore, and StudioController persistence.
They do not introduce another project format or job queue.

## What is saved

| Open draft | Existing durable writer | Completion condition |
| --- | --- | --- |
| Research note | Scoped evidence transaction in the existing desktop worker | The transaction commits and the visible saved baseline is updated. |
| Reading copy | NarrationController and NarrationStore | The hash-guarded project save returns and the editor reflects its saved baseline. |
| Audio Studio | The existing native Save / Save As action and StudioController | The action confirms its write; destination cancellation, hash conflicts and a busy Studio remain unsaved. |

Original source text and retained source files stay unchanged. Save As keeps
its contextual Ctrl+Shift+S binding inside an editor. The main-window
Ctrl+Shift+S command still uses the existing shared command registry. Modal
editors expose a separate named button so the global draft action is reachable
without overloading Save As.

Save All captures open participants once, runs their existing saves in order,
and waits for asynchronous completion. Starting a worker or returning a truthy
value is not a completion receipt. Failure in one draft does not skip the next
valid draft. A final pass checks every currently open participant, including
documents opened or changed during the operation. New typing remains dirty,
even if an older snapshot was successfully committed.

The operation reports each remaining draft and focuses the first live failed
participant where native focus is available. Closed editors unregister their
own participant without removing another window's registration.

## Closing during a save

A user Close or Exit while Save All is active is deferred. It does not cancel
the in-flight save or block the native event thread. After successful
completion, a posted close callback checks both window liveness and current
dirty state again. A write failure, source conflict, new edit, or failed dirty
validation keeps the window open. A destroyed window is never accessed by the
late callback.

The existing ordinary unsaved-close and recovery choices remain in place.
Forced operating-system shutdown that cannot be vetoed is outside this
deferred-close guarantee. This feature does not claim power-loss acceptance.

## Authority stays explicit

Assistant plans and translation jobs continue to use their current
preview/approval/run flows. Save All never calls their submit or approval
methods and never starts a model, provider request, import or render. Existing
saved jobs remain durable through their own services. Modal metadata and
connection forms retain their existing explicit confirmation controls.

## Reuse and evidence

The GUI-independent coordinator is reused from PR117 source
`ab58052ca846aa0261203fc88c523407178fa740`, with completion observers and
deferred-close handling added for the current application. The older parallel
speech, Studio and provider stores are not imported.

Required strict source gates:

- `tests/ui/test_save_all.py`
- `tests/ui/test_save_all_current_drafts.py`
- `tests/ui/test_desktop_evidence_jobs.py`
- `tests/ui/test_narration_editor.py`
- `tests/ui/test_desktop_handlers.py`
- Existing productivity, evidence recovery, Studio, and playback-control suites.

The tests exercise real note transactions and managed reading/Studio files,
rollback, external writer conflicts, duplicate and late callbacks, current
dirty state, and close cancellation. Synthetic widget boundaries are not
screen-reader or device acceptance.

The installed native Windows gate is:

```powershell
python -I -B tests/ui/native_save_all_smoke.py --native
```

It drives real wx controls/events and the current worker/store implementations,
holds an actual note transaction before its write, verifies pending modal and
main-window close, injects a transaction failure, and checks immutable source
bytes. Missing Windows is reported as NOT_RUN with exit code77. NVDA remains a
separate manual acceptance item.

This change was prepared through source APIs during executor unavailability.
Its new source and native tests require execution on the composed exact head;
this document does not assert that they have passed.
