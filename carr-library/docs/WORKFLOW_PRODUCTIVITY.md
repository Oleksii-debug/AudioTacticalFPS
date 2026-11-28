# Commands, reusable workflows and diagnostics

The desktop exposes these features through its existing native menus and local
services. They do not add another command registry or execution store.

## Find a command

Open **Help → Find a command**, normally with **Ctrl+Shift+P**. Type words from a
menu path, command name or current keyboard shortcut. Tab reaches the native
results list; Up and Down choose a command. Enter runs it, and Escape closes the
palette.

The palette displays current shortcut assignments and unavailable commands.
It checks the selected menu item and its parent menus again after the palette
closes, then dispatches the original menu handler. Cancellation restores focus
to the originating control. A changed library, replaced command or disabled
item cannot execute a stale selection.

If an older saved assignment already owns Ctrl+Shift+P, that assignment remains
active and the new palette has no shortcut. Choose another key in **Options →
Keyboard shortcuts**. Existing preference files are retained.

## Named workflows and typed macros

Open **Tools → Saved tool plans and jobs**. A workflow is a saved typed assistant
plan, stored in the existing local job store. Multi-step plans serve as typed
macros: only the existing allowlisted library and audio tools can run. Shell
commands, Python, arbitrary network requests and raw keystroke recordings are
not executable workflow operations.

Choose the library and any allowed input/output folders. Enter a guided goal
or a typed JSON plan, give it a short name and choose **Preview plan**.
**Save named workflow** saves a pending plan without running any tools.
**Run / resume** is the separate authorization to execute the displayed plan.

**Reuse saved workflow** loads the name and typed steps into a new draft. It
clears historical input/output folder grants and full-backup permission.
Choose the current scope, review or edit the typed steps, and preview again.
Execution creates a new job identity with the new permissions and ordinary
output, budget, cancellation and checkpoint rules.

**Saved jobs** inspects an original job and its exact plan. Resuming it retains
its original scope and checkpoints. Restored original jobs remain blocked:
reuse their recipe for fresh review and submission. Listing and inspecting a
recipe does not activate missing folders from the previous computer.

Closing or pausing requests cancellation. Work already committed remains
committed; a completed plan save remains available even if the window closes.
The running service owns its durable checkpoints.

## Redacted diagnostics

Open **Help → Diagnostics preview and export**. The existing shared background
operation runs the application’s isolated synthetic installation self-test and
reads versions for a fixed list of installed packages. It does not scan the
user’s library or run neural model inference. The preview explicitly records
native GUI, NVDA and model inference as **NOT_RUN**.

The report includes only allowlisted system and package versions, synthetic
check results and fixed status categories. Document text, research notes, book
and workspace names, user paths, provider endpoints, account identifiers,
credentials, environment variables, raw exceptions and logs are omitted.
It does not attempt to sanitize arbitrary logs.

Review the native read-only preview before choosing **Export exact preview**.
Export writes those exact UTF-8 bytes to a new explicitly selected file using
the shared safe export writer. Existing files and files inside the application
data directory are rejected. No report is sent to a service automatically.

Automated portable tests and native Windows probes are separate from manual
keyboard, NVDA and listening acceptance. A synthetic check does not establish
real model inference or accessibility acceptance by a screen-reader user.
