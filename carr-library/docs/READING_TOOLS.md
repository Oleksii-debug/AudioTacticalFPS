# Reading-copy bookmarks, paragraphs and listening speed

The native voice reader opens **Bookmarks and paragraphs…** for explicit actions on the active reading copy. A bookmark stores its exact text checksum, a Unicode character offset, a label and a random identity. It does not grant quotation, source, engine or output authority. Up to 256 bookmarks fit in one copy.

**Go to bookmark and close** restores the saved character only when the current text matches the bookmarked version. Changed text is visibly marked and requires Undo to that version or a new bookmark. No guessed relocation is performed. Removing or adding a bookmark changes the copy's saved state without discarding its remembered audio position.

**Split paragraph at current cursor** inserts one blank-line boundary as a single existing reading-history edit. **Join next line or paragraph break** joins the next boundary as one edit. Source text, retained-source locators and existing quotation decisions remain unchanged. Undo/Redo, separate reading tabs, Ctrl+S, Save As and typed library backup all use the same ReadingDocument representation.

Reading projects without bookmarks or a content policy retain their v1/v2 wire formats. Bookmarked projects use the strict additive carr.reading-copy.v3 shape, with the same original-source checksum and edit history. Passive recovery needs no installed voice. Bookmarks and their labels do not contain executable actions or destination paths.

## Read or skip explicitly reviewed content

Select a nonempty region in the editable reader, open **Bookmarks and paragraphs…**, then **Read or skip reviewed content…**. Choose **Footnote**, **Table**, **Formula or mathematics**, **Code**, **Heading**, or **Other explicitly selected content**, give the selection a name, and press **Mark selected reading content**. The selection uses the reader's Unicode character coordinates, including native Windows line-ending and supplementary-character conversion.

The application does not infer semantic categories. Unmarked text is always read. Automatic table, footnote, formula and code classification remains unavailable in this control. A marked region is the user's explicit description of that exact text version, not an extraction correction or quotation approval.

Choose a category that has reviewed regions, select **Read** or **Skip**, and press **Apply category Read or Skip**. Read is the default. **Preview exact narration** shows the whole reading copy, the selection captured when the dialog opened, or the text from that captured cursor. Close and reselect in the main editor for another range. The preview never starts a voice. If every selected character is skipped, it clearly reports empty narration; speech and WAV publication refuse that empty result.

F6, Read from cursor, Read current word/sentence/paragraph, selected-text reading and Save narrated WAV all use this same projection. A gap between retained slices inserts a newline marked as derived narration. Every retained slice records its reading range and the existing source map's exact or approximate range; the inserted separator has no original-source claim. The original text and the editable reading copy remain byte-for-byte unchanged by Read/Skip. Changing a policy stops the current recording. Filtered narration has no whole-selection audio follow or recovered audio position; no character-to-audio timing is invented.

Regions bind the exact complete text checksum, their selected bytes, category and range. Any changed text makes an enabled Skip region stale and blocks narration until the user re-marks it or chooses Read. Undo back to the exact text version makes the old region applicable again. Changing only a label or Read/Skip setting also invalidates pending narration and live-player review, even when the spoken characters would be identical. The existing speech writer checks the same source, owner, revision and policy immediately before no-clobber WAV publication. The editor is read-only during the serial speech task; Stop and normal owner cleanup remain available. A delayed quiet-feedback playback admission repeats the same checks. If an explicitly saved WAV completed before a later reader change, the interface reports its recorded path and checksum as saved, with the changed-state warning; it refuses to adopt or play that result.

Up to 256 non-overlapping regions per text version and 256 KiB of policy data are admitted. Different historical versions may retain passive regions, but none is silently relocated. Remove a region to discard its classification. Read re-enables its category without deleting the annotation or rewriting text history.

Ctrl+S, Save As, current-tab recovery and the existing complete library backup retain the passive policy in the reading-copy v4 representation. Closing and reopening requires no voice or model. Removing the last annotation returns to the prior v1/v2/v3 shape; documents without a policy preserve their original serialization. No engine, running timer, output destination or quotation authority is restored.

This is a policy of the selected **reading copy**. Book Audio, headless Book commands and assistant retained-source tools continue to review their own independently captured source; they do not silently inherit private reading-copy annotations. To narrate these reviewed exclusions, use this reader's explicit playback or Save narrated WAV action.

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

The Read/Skip increment adds `tests/speech/test_narration_policy.py` and `tests/ui/test_reading_narration.py`, retaining existing reading-tools/focus/navigation coverage. The same native tab probe adds actual keyboard classification, Read/Skip, empty and restored previews, Escape, unchanged originals and restart persistence. Its empty voice fixture does not claim acoustic quality. These added bytes are source-reviewed and **NOT_RUN** until exact-head CI completes.
