# Reading-copy bookmarks, paragraphs and listening speed

The native voice reader opens **Bookmarks and paragraphs…** for explicit actions on the active reading copy. A bookmark stores its exact text checksum, a Unicode character offset, a label and a random identity. It does not grant quotation, source, engine or output authority. Up to 256 bookmarks fit in one copy.

**Go to bookmark and close** restores the saved character only when the current text matches the bookmarked version. Changed text is visibly marked and requires Undo to that version or a new bookmark. No guessed relocation is performed. Removing or adding a bookmark changes the copy's saved state without discarding its remembered audio position.

**Split paragraph at current cursor** inserts one blank-line boundary as a single existing reading-history edit. **Join next line or paragraph break** joins the next boundary as one edit. Source text, retained-source locators and existing quotation decisions remain unchanged. Undo/Redo, separate reading tabs, Ctrl+S, Save As and typed library backup all use the same ReadingDocument representation.

Reading projects without bookmarks retain their v1/v2 wire formats. Bookmarked projects use the strict additive carr.reading-copy.v3 shape, with the same original-source checksum and edit history. Passive recovery needs no installed voice. Bookmarks and their labels do not contain executable actions or destination paths.

## Listening settings

The existing Playback controls modal now exposes listening speed and pitch. These are independent of synthesis settings. Apply explicitly stops the current transport and uses the reader's existing serial worker to prepare verified private PCM through the existing typed FFmpeg converter. Original rendered bytes are not overwritten. The user presses Play explicitly after successful preparation.

Cancellation, source/library changes, Stop and a closed modal invalidate late results. Temporary PCM stays owned until either the original WavePlayer adopts it or cleanup succeeds. Closing the reader schedules final cleanup behind its existing worker; failed native/resource closure retains the owned files for a retry.

See [LISTENING_SPEED.md](LISTENING_SPEED.md) for measured frame mapping and limits.

## Optional reading focus

**Reading focus…** opens a separate native, read-only view of the active reading copy. It starts paused. **Paced visual reading** advances deterministic word units at an explicit 40–1000 words per minute; Start, Pause and previous/next controls do not synthesize or start audio. No running timer is saved or restored. Up to 100000 word units are admitted in a chapter.

**Audio follow — approximate** follows only audio captured from the same exact reading-copy text, selection and measured PCM frame count. The position is an estimate over the original recording duration. Available voice adapters report no exact word timings, so the interface does not claim them. Listening-speed conversion keeps the player's public position in original-recording seconds, preserving this meaning.

The current word is highlighted without timer-driven keyboard focus changes. The status names its reading-copy character range and the original-source range where the existing source map provides one, marking approximate mappings. A text/tab/audio change stops following. Only **Use highlighted cursor and close** moves and saves the main editor's cursor. F7 and F8 remain explicit controls of the existing current audio.

## Verification status

This increment was prepared while the local executor was unavailable. New bookmark, paragraph, reading-hook, ownership and measured-duration suites are required by source/installed CI. The existing actual Windows reading-tab probe now exercises native bookmark creation/navigation, paragraph Undo, persistence and a fresh reader restart. The listening probe exercises the existing native player with the derivative path. These new scenarios remain **NOT_RUN** until their exact commit's CI result is recorded; this document does not claim manual NVDA or acoustic qualification.
