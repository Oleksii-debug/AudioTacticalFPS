# Independent reading tabs

The standalone **Voice reader and reading copy** can keep up to 16 editable
copies open in a native notebook. **Ctrl+T** creates a copy; **Ctrl+Tab** changes
the selected page; **Ctrl+W** closes the current page. New, Close and Rename
buttons expose the same operations. One tab remains open until the reader
window closes. Source-bound book readers retain their existing source scope.

Each page owns its ReadingDocument, immutable original, edit/undo history,
explicit voice identity, synthesis rate and volume, Unicode cursor and last
numeric listening position. Missing voices remain visibly unselected. Changing
pages stops playback and invalidates delayed playback callbacks. An active
worker or an unconfirmed device stop prevents the page change. A changed text
invalidates its old numeric audio position; restoring a number never starts
audio automatically.

**Ctrl+S** saves the selected draft through the existing reader recovery store.
Save As exports an independent reading project. The application Save All
coordinator visits each nonempty dirty copy and keeps a canceled or failed save
open. Closing the window asks about each unsaved nonempty copy. If a reader worker
is active, Close first requests cancellation; choose Close again after that
worker finishes so the per-copy reviews cannot change a still-running job. Closing a tab
removes its membership only: saved projects and prior recovery bytes remain.

The passive state/speech/reading-tabs.json index stores only ordered validated
IDs, labels and the selected ID. Each ID resolves to an existing hashed reader
draft name. There are no paths, output permissions, credentials or automatic
engine activations in the index. Both draft and membership saves compare the
exact previously read bytes under the existing state locks. A competing window
produces a visible save conflict and keeps the current editor open. Old manual
recovery becomes the first tab; opening explicit new text preserves it as a
separate copy. Invalid recovery is retained and the reader offers Save As.

Normal typed library backup includes the index and its referenced drafts.
Restore rejects missing references or unexpected fields and does not reinstate
playback authority. The existing reading appearance applies to the selected
native editor without editing its text.

## Verification and limits

The portable tests exercise real exact-byte conflicts, index limits, directory
identity, independent undo/cursor/settings and backup/delete/relocate/restore.
Actual UI handlers also cover competing windows, busy and failed-stop vetoes,
late and reentrant native events, confirmation, Save All and legacy recovery.
These are required source/installed checks. This composed increment is
NOT_RUN until its exact source passes CI; neither interrupted runs nor donor
results count as a pass for this build.

scripts/windows_reading_tabs_smoke.py requires actual Windows, actual installed
module hashes and native wx controls. It drives Ctrl+T, Ctrl+Tab and Ctrl+S with
UIActionSimulator, edits two copies, closes/reopens the reader and checks
independent undo and cursor recovery without an installed voice. Only a
successful run on the exact installed revision establishes this evidence.
Speech quality, acoustic output and manual NVDA acceptance remain separate.
