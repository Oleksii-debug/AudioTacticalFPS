# Reading-copy bookmarks, paragraphs and listening speed

The native voice reader opens **Bookmarks and paragraphs…** for explicit actions on the active reading copy. A bookmark stores its exact text checksum, a Unicode character offset, a label and a random identity. It does not grant quotation, source, engine or output authority. Up to 256 bookmarks fit in one copy.

**Go to bookmark and close** restores the saved character only when the current text matches the bookmarked version. Changed text is visibly marked and requires Undo to that version or a new bookmark. No guessed relocation is performed. Removing or adding a bookmark changes the copy's saved state without discarding its remembered audio position.

**Split paragraph at current cursor** inserts one blank-line boundary as a single existing reading-history edit. **Join next line or paragraph break** joins the next boundary as one edit. Source text, retained-source locators and existing quotation decisions remain unchanged. Undo/Redo, separate reading tabs, Ctrl+S, Save As and typed library backup all use the same ReadingDocument representation.

Reading projects without bookmarks retain their v1/v2 wire formats. Bookmarked projects use the strict additive carr.reading-copy.v3 shape, with the same original-source checksum and edit history. Passive recovery needs no installed voice. Bookmarks and their labels do not contain executable actions or destination paths.

## Listening settings

The existing Playback controls modal now exposes listening speed and pitch. These are independent of synthesis settings. Apply explicitly stops the current transport and uses the reader's existing serial worker to prepare verified private PCM through the existing typed FFmpeg converter. Original rendered bytes are not overwritten. The user presses Play explicitly after successful preparation.

Cancellation, source/library changes, Stop and a closed modal invalidate late results. Temporary PCM stays owned until either the original WavePlayer adopts it or cleanup succeeds. Closing the reader schedules final cleanup behind its existing worker; failed native/resource closure retains the owned files for a retry.

See [LISTENING_SPEED.md](LISTENING_SPEED.md) for measured frame mapping and limits.

## Verification status

This increment was prepared while the local executor was unavailable. New bookmark, paragraph, reading-hook, ownership and measured-duration suites are required by source/installed CI. The existing actual Windows reading-tab probe now exercises native bookmark creation/navigation, paragraph Undo, persistence and a fresh reader restart. The listening probe exercises the existing native player with the derivative path. These new scenarios remain **NOT_RUN** until their exact commit's CI result is recorded; this document does not claim manual NVDA or acoustic qualification.
