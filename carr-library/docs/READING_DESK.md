# Reading desk

The Reading menu opens the full text of an imported book without requiring a
search phrase. The reader presents one complete source page or logical segment
in a native, read-only text control. It supports previous/next, direct navigation,
named page bookmarks, session back/forward, and resuming the last opened page.
The Where am I action describes the source, its actual location, reading order,
and current verification status as ordinary text.

## Keyboard commands

Open the Reading menu with **Alt+D**. Select a source in Books and Collections
and press **Ctrl+R**, or use **Reading,
Read selected book**. If no source is selected, a named book chooser opens.
Use **Alt+D** to open the Reading menu from the main window.

- **Ctrl+PageDown / Ctrl+PageUp:** next / previous source location.
- **Ctrl+G:** jump to a reading-order position.
- **Alt+Left / Alt+Right:** back / forward in this reading session.
- **Ctrl+B:** add a named page bookmark.
- **Ctrl+Shift+B:** open, rename, or delete a bookmark.
- **Ctrl+Shift+L:** focus the textual location and verification status.
- **F1:** reader keyboard guide. **Escape:** close the reader.

Every shortcut also has a named native button. Ordinary text-navigation keys
remain available in the source text control. To start a changed source afresh,
choose **Start at first source location** explicitly; an unavailable old resume
position is reported rather than silently redirected.

## Recorded EPUB audio

For an imported EPUB with local recorded SMIL overlays, use **EPUB synchronized
recorded audio** in this reader. It opens the current source section without
starting playback. See [recorded EPUB reading](EPUB_RECORDED_READING.md) for the
supported subset, named playback controls and source-preservation checks.

## Source locations

Physical PDF pages and logical TXT/DOCX segments stay distinct. The reading-order
number is not a printed page number. Printed labels are used only if the existing
citation service independently approves them. An extracted page can be read
while unverified; reading and bookmarking it never approve it for quotation.
The existing Search and Evidence workflows remain responsible for saving and
exporting verified quotations.

A saved position contains the library, edition, exact active revision, original
file hash, page/segment index, and page-text hash. The reader validates the retained
original and current authorization again when a page is opened. A changed,
missing, archived, or damaged source cannot be silently substituted for the saved
one. Reopening a newer revision requires an explicit fresh start.

Resume remembers the **opened page or segment**, not the NVDA review cursor,
spoken word, or exact text caret. Opening the reader focuses its ordinary text
control. Closing it restores focus to the invoking library view. Native control
and keyboard tests do not replace actual NVDA acceptance.

## Personal reading state

Names and positions are saved locally in `reading-state.json` in the selected
library data folder. They are separate from source text, saved evidence, research
notes, and citation approval. File writes use a cooperative library lock, a
flushed temporary file, and atomic replacement. Damaged existing state is reported
and preserved instead of being silently cleared.

Use the reader's export/import actions to back up reading state for the active
library. Exported files contain bookmark names and source identifiers, not source
passages. Import validates every referenced source before making one atomic merge.
It preserves existing resume positions and rejects conflicting bookmark identities.
Importing the same file again does not duplicate its bookmarks.

The full library backup includes this file, together with every library's
bookmarks and resume positions. It checks the source identities against the
same database snapshot and binds the saved bytes to the backup manifest. Restore
creates a new data folder and preserves bookmark names, times and locations.
Archived libraries and historical reading revisions remain unchanged; opening
an old position never silently jumps to a newer revision. Damaged state or a
location that does not match the saved source stops the backup or restore before
publication, keeping the existing data intact.

Older version-one and version-two backups remain readable; they do not contain
personal reading state. For those backups, keep the separate reading-state
export when moving to another computer. A restored library retains the same
source identities; a different library cannot accept the export. No cloud
account or network retrieval is involved.

The same backup also preserves the existing application-state and pending-import
records. When personal reading state exists, version five binds those records and
the reading sidecar in one manifest. Without a reading sidecar, the existing
version-two, version-three or version-four format is retained. Both historical
version-three layouts remain readable: application state and reading-state-only
backups are distinguished by their exact declared fields. A mixed or incomplete
manifest is rejected. Generated audio remains an explicit backup option; pending
source inputs needed to resume an import remain part of the checked snapshot.

## Implementation and primary references

The feature uses the existing authorized SQLite snapshots, retained-source
verification, text/PDF attestors, native wx controls, local writer lock and guarded
export writer. It adds no runtime dependency and does not modify the research
schema. Limits are explicit: 10,000 source pages, 2,000,000 characters in one
displayed page/segment, and an 8 MiB reading-state file. Larger sources remain
available through the existing Open original source action.

The workflow is informed by these primary product documents; no code from them
is copied:

- [calibre E-book viewer](https://manual.calibre-ebook.com/viewer.html): resume,
  named bookmarks, and navigation history.
- [Thorium Desktop reading](https://support.thoriumreader.com/hc/en-us/articles/26749951794460-Reading-Desktop):
  reading history, direct navigation, keyboard access, and textual location.
- [Thorium Desktop bookmarks](https://support.thoriumreader.com/hc/en-us/articles/26762511786524-Bookmarks-Desktop):
  named locations and explicit bookmark management/import/export.
- [Readium locators](https://readium.org/architecture/models/locators/): resource
  identity and saved locations. CARR uses its existing stricter revision and
  content-digest identities rather than adopting another reader dependency.

## Voice-reader text navigation

The existing Voice reader now selects a current, previous or next word,
sentence or paragraph. Choose **Navigation unit**, then use the named buttons
or Ctrl+Alt+Left / Ctrl+Alt+Right. Ctrl+Alt+Home selects the current unit.
Selection alone starts no speech. **Read current unit**, Ctrl+Alt+Enter or the
existing F6 command explicitly reads the selected text through the same local
voice registry and quiet-playback path.

Words keep Unicode letters, numbers and combining marks together, including
internal apostrophes and hyphens. Sentence units use punctuation and line
boundaries; abbreviation handling and languages without word separators are
not linguistic alignment claims. Paragraphs end at a blank line or Unicode
paragraph separator. An ordinary CRLF is one line break, not a blank paragraph.
Previous/next never wrap into another book or start whole-document playback
when no further unit exists.

Native positions are mapped through the control's observed Unicode prefixes,
so an emoji's UTF-16 units and displayed line-ending normalization cannot
silently shift the selected text. The same mapping now corrects reading from
the cursor and saved/recovered cursor positions. Original source bytes,
source anchors and quotation authority are unchanged; navigation does not
replace text or synthesize timing.

The prefix/cursor mapping reuses the canonical-compatible PR149 source
`906aa1a95a8f40ef119682d2d5c805c017c327f4` without its additional live-reader
or speech adapter. New deterministic navigation and actual-callback tests are
`tests/speech/test_navigation.py` and
`tests/ui/test_reader_navigation.py`. The existing installed
`scripts/windows_pronunciation_smoke.py` additionally drives the real reader
buttons and next-word accelerator, checks paragraph/Unicode selection,
unchanged source bytes and saved character offsets. This API-composed change
requires fresh execution; no new Linux, Windows, NVDA or listening PASS is
claimed from source review alone.

## Find and replace in a reading copy

Open **Find and replace** or press **Ctrl+F** in the reader. Enter a literal
pattern and replacement, or explicitly select regular-expression mode. Literal
replacement preserves backslashes as text. Regex replacement supports the
existing worker's capture-group expansion. Case-insensitive matching is optional.
Choose the whole reading copy or the selected text before pressing **Preview
matches**.

The preview shows the exact before/after scope and each occurrence's character
range. Previous/Next selects a reviewed occurrence without changing the text or
starting audio; it does not wrap at either end. **Replace selected match** changes
only that occurrence. **Replace all reviewed** applies the complete scope shown
in the preview as one undoable edit. Both invalidate the preview, so further
replacements require another explicit review. **Undo last replacement** restores
the preceding reading-copy revision; the reader's normal undo history and
recovery file remain available after the dialog closes.

Unicode selection uses the native control's observed prefixes, including UTF-16
and line-ending differences. A changed text, selection, form or library invalidates
the proposal. Search runs through the reader's existing worker and isolated
substitution process, with the existing text, result and match limits and a
one-second search allowance. Cancel or closing the dialog requests termination
of that owned search; a late result cannot apply an edit.

Source text, retained asset bytes and quotation authority remain unchanged.
Playback is stopped before a replacement; applying or undoing a text edit does
not start it again. A recovery-save failure keeps the change undoable in memory
and reports that **Save As** is needed before closing. Search itself never applies
a replacement.

The focused source suites exercise the real isolated worker, cancellation and
deadline cleanup, Unicode offsets, exact selected scope, queued callbacks, stale
authority and recovery. The existing installed Windows pronunciation/reader probe
also drives Ctrl+F, native match selection, single/all replacements and durable
undo using real controls. Prepared source requires its own CI execution; these
checks do not qualify a voice or NVDA behavior.

## Native playback transport guards

On Windows, seeking while paused sends a silent MCI seek and keeps the reader
paused until explicit Resume. It does not restart the recording just to pause it
again. A failed native Close retains the alias and its temporary audio for a
confirmed retry; a successful Close establishes silence even if Stop reported an
error. These narrow guards are reused from the reviewed canonical-compatible
PR149 player changes. The existing FFplay environment filtering is preserved.

The focused transport suite checks the real PCM input and MCI command/cleanup
contracts. `scripts/windows_reader_player_smoke.py` requires the installed package
and actual Windows MCI; it measures pause, silent seek, explicit resume, stop and
file release on a silent synthetic recording. A missing native output device
fails that gate. It does not establish audible quality, NVDA acceptance or
physical microphone behavior. This transport increment does not claim A–B repeat,
sleep-timer or configurable-skip completion.
