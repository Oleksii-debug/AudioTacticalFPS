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

The focused suites exercise actual regex subprocesses for Unicode/group
expansion, timeout, cancellation, cleanup, limits and redirected legacy stdout.
Handler tests use real worker threads and native-control stand-ins that reject
cross-thread or post-destruction access. The real reader closure is executed
against UTF-16/CRLF controls and the existing source/recovery behavior.

The integrated Linux run passed all 336 cases in 16 strict suites with zero
failures, skips, expected failures or unexpected passes. This includes the
existing source/session, keyboard, Save All and player-cleanup regressions,
plus the new find/cursor/navigation/palette tests and real ffplay transport.
The exact integrated Windows candidate still requires its own evidence;
native Windows and NVDA remain `NOT_RUN`. Donor counts are not used as evidence
for this composition.

This integrates the bounded Find/Replace portion of workflow W06. Tabs, saved
presentation profiles and speed-reading modes remain separate requirements.
See [the requirement snapshot](RESONANCE_REQUIREMENT_STATUS_20261127.md) and
[reader integration evidence](READER_INTEGRATION_20261127.md).
