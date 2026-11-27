# Save All in Resonance

**File → Save all** saves the active research note and registered open reading
copies and Audio Studio projects. The voice reader and Audio Studio also offer
**Save all open drafts**, so this action is available while their modal windows
are open.

The action uses one shared `SaveCoordinator`, adopted from the existing primary
implementation. Each document keeps its own storage service and validation.
The coordinator waits for a participant's completion callback; starting a
background save is not reported as a completed save.

## What is saved

- Research notes use their existing authorized evidence transaction. The callback
  follows the transaction's completion and the updated saved baseline.
- Voice reading copies use the existing recovery store, retaining the immutable
  original, mapped edits, undo history, voice selection and reading position.
  Save As still exports a separate reading project.
- Audio Studio validates and saves a project revision in its existing project
  store. An automatic recovery draft does not count as a saved project revision.

The existing research-note recovery timer and recovery command remain available.
Save All does not start speech, render audio, change a source document, or approve
a reading copy as quotation evidence.

## Completion and retained drafts

Success is reported only after all requested saves complete and the coordinator
rechecks the live drafts. If a user typed a newer note while an earlier snapshot
was being written, the newer draft remains unsaved and the summary says so.

A failed write, invalid audio script, unavailable source, changed view or closed
document produces an incomplete summary. Other independent documents can still
save. Closing a reader or Studio window unregisters its participant; a delayed
completion cannot write to its destroyed controls. Empty text produced by deleting
an existing reading copy remains a real edit that Save All must preserve.

The main File command keeps its existing menu path for keyboard preferences.
Ctrl+S retains its current meaning. Ctrl+Shift+S in the reader or Studio remains
Save As; existing custom shortcuts are not rewritten.

## Local engine lifecycle

Studio requests cancellation before closing. Its existing serial executor closes
an owned voice registry only after active work has ended, including discovery
that completes after the window closes. Closing an unused service does not
discover voices; a registry supplied by another owner is not closed by Studio.
The UI does not wait for a warm voice process to exit.

Portable tests exercise real note transactions, reading and project files,
worker completion and a small synthetic warm subprocess. Native controls in
those tests are explicit doubles; they do not establish Windows, NVDA or
physical audio acceptance.
