# Find and replace in a reading copy

Open **Voice reader and reading copy**, then **Find and replace**. Ctrl+F or
Ctrl+H opens the same dialog while the reader is active. No voice is required
for text editing, but an existing reader operation must finish or be cancelled
before opening the editor workflow.

The dialog supports literal text and bounded Python regular expressions,
optional case-insensitive matching, Find next/previous with explicit wrap
status, and either the whole reading copy or the selection made before opening
the dialog. Moving to a match does not narrow that original selected-text scope.
Empty regex matches advance by match index and cannot trap Find on one cursor
position. Spaces and line breaks may be searched literally; literal replacement
backslashes are preserved. Regex replacements support the worker's ordinary
capture-group expansion.

Use **Preview selected replacement** after selecting a match, or **Preview
replace all**. Neither action changes the reading copy. Review the current and
proposed text, then explicitly apply. One apply creates one existing
`ReadingDocument` undo step. Undo remains available in both this dialog and the
reader history. Applying a change stops old playback, invalidates the old rendered
audio, and saves the ordinary reading draft where available. A recovery-write
failure reports that the edit remains in memory and instructs the user to use
Save As; it does not pretend that the committed edit was rolled back.

Original text, retained-source anchors and quotation status remain separate from
the edited derivative. A changed library, withdrawn source authorization,
replacement document, different text/selection or changed search form invalidates
pending results. No match or replacement is applied after closing the dialog.

## Execution and limits

Both literal and regex matching use the existing isolated substitution worker,
with explicit UTF-8 protocol bytes, a one-second execution deadline and the
reader's existing cancellation event. Cancel/close terminates and reaps owned
worker processes. No additional engine, executor, network request or job queue
is introduced.

The existing reading limit is two million characters. Search input must also fit
the existing eight-million-byte worker envelope. Each search scope allows at
most 10,000 matches; the worker limits expanded-match output to one million
bytes including its per-match allowance. Patterns have at most 1,024 characters
and replacement templates 4,096 characters. A request outside these limits is
rejected; the reading copy is not partially edited.

The reader converts verified native text-control ranges to Python character
offsets and back using the existing `native_position` helper. UTF-16 surrogate
pairs and native CRLF positions are not treated as Python string indexes.

## Evidence and remaining scope

The focused composed run passed 157 cases with zero failures/errors/skips,
including 46 Find/Replace cases and the existing reading, session, pronunciation,
cursor and clipboard suites. Actual regex subprocesses exercised Unicode/group
expansion, timeout, cancellation, cleanup, limits and redirected legacy stdout.
Handler tests use real worker threads and native-control stand-ins that reject
cross-thread or post-destruction access. The real reader closure is executed
against UTF-16/CRLF controls and existing source/recovery behavior.

Native Windows keyboard evidence belongs to each exact candidate build; these
source tests do not establish Windows 11/NVDA acceptance. This completes the
bounded Find/Replace portion of workflow W06. Tabs, saved presentation profiles
and speed-reading modes remain separately tracked in
[the finite workflow inventory](BALABOLKA_WORKFLOW_ACCEPTANCE.md).
