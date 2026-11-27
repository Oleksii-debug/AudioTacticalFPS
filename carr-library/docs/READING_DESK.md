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

## Voice reading and pronunciation

Use **Voice, Voice reader and reading copy** (**Ctrl+Alt+V**) for an independent
reading copy. **Voice, Narrate selected book or chapter** (**Ctrl+Alt+R**) opens
an explicitly chosen retained-source range. Both use the same local voice reader.
Opening either window does not start speech.

**Read current logical line** reads the newline-delimited line containing the
caret, rather than a visually wrapped screen line. **Read current paragraph**
uses blank lines as boundaries. **Read clipboard once** takes one explicit text
snapshot and speaks it through the selected voice. It neither watches later
clipboard changes nor inserts the snapshot into a source-bound reading copy.

**Pronunciation dictionary** opens named native controls for adding, editing and
removing literal or regular-expression rules. Empty language and voice fields
match any selected voice; nonempty fields match its exact language code or voice
ID. General rules run before language-specific and voice-specific rules, retaining
list order within each scope. Import reads the existing dictionary JSON format;
it replaces the session rule list and does not apply it automatically. Export
writes a new file outside the private library and refuses existing filenames.
Export explicitly to retain this session dictionary after closing the reader.

**Apply to reading copy** makes one reversible change. **Undo reading edit** in
the reader restores the prior text; the retained original, its source locator
and citation authority remain unchanged. Up to 1,000 rules are accepted; output
and regex processing have bounded budgets. Cancellation, invalid expressions,
changed source scope or an exceeded budget leave the reading text unchanged.
These substitutions do not prove correct stress, phonemes or acoustic quality.

The Voice Reader uses the shared configurable keyboard registry. **F6** reads
the selection or copy, **F7** pauses/resumes and **F8** stops. **Find a voice
reader command** and **Voice reader keyboard shortcuts** expose its commands;
new line, paragraph, clipboard and dictionary actions can receive user-selected
shortcuts without taking native editing or screen-reader keys. Saved voice-reader
carets use Unicode text offsets and restore through observed native text ranges.
This is separate from the Reading Desk's page-level resume described above.

### Portable narration provenance

**Save WAV** writes an explicitly selected reading-copy range (or the complete
copy) as PCM with one bounded trailing RIFF provenance chunk. The receipt and
audio are staged and published together using the existing no-clobber writer.
It records the exact Unicode range, reading-project and text hashes, retained
source anchor (when present), voice identity hash, available runtime fingerprint,
settings and measured PCM hash/geometry. It embeds neither source text nor a
source filesystem path. Source identifiers travel with the WAV: review them
before sharing a local export. A portable source locator is not an authority
to open another workspace; reopened bound copies must pass retained-source
authorization before export.

Use **Inspect saved WAV provenance** in the voice reader (Tab or the shared
command finder). Inspection is local, read-only, cancellable and does not
play audio or resolve another workspace. It reports missing or damaged
provenance rather than inventing pages. A receipt checksum is not a signature,
acoustic attestation, source approval or quotation permission. A caller with
the saved reading project can also compare its exact snapshot through the
existing receipt reader. Legacy PCM without the chunk remains readable;
third-party converters may remove the chunk. Such a file is not verified
provenance. Exported WAVs remain user-owned external files, not managed
library backup inputs.

## Finding and navigating a reading copy

Choose **Word**, **Sentence** or **Paragraph** in **Navigation unit**, then use
**Previous text unit**, **Select current text unit** or **Next text unit**.
These commands select native text without starting speech or changing the copy.
**Read current text unit** is a separate explicit action. At a boundary the
selection stays in place; navigation does not wrap and speak the full document.
Sentence and word boundaries follow the existing deterministic text rules.
They are not a language-specific linguistic analysis or word-level audio timing.

**Ctrl+F** opens **Find and replace**. Choose the whole reading copy or the
selected range, enter a literal pattern or a bounded regular expression, and
press **Preview matches**. The dialog lists exact character ranges and shows
the proposed text. Moving through reviewed matches selects their corresponding
native ranges, including text after non-BMP Unicode characters. Preview does
not change the reading copy.

Use **Replace selected match** for one reviewed occurrence or **Replace all
reviewed** for the complete proposal. Either action creates one undoable
reading-copy edit and saves the existing recovery draft. **Undo last replacement**
restores that edit while it is still current. If recovery cannot be written,
the dialog reports that the edit is only in memory and asks you to use Save As.
The original source bytes and quotation authority remain unchanged.

Changing the reading text, selected range or search form invalidates a preview.
Switching the active library, changing its root, closing the reader, revoking its
source scope or cancelling search prevents a late search result from being
applied. The existing isolated regex worker enforces its time, match and output
budgets. Find and navigation use the same configurable reader command registry;
they create no additional job queue or reader state format.

The native control probe uses a declared synthetic voice and playback sink.
Actual installed voice quality, physical audio playback and Windows 11/NVDA
acceptance require their separate evidence.

## Playback of the current recording

After reading text, open **Playback controls** in the Voice Reader. This dialog
controls the same rendered WAV and does not start sound merely by opening.
Its command is also available in **Find a voice reader command** and can receive
a user-selected shortcut.

Choose backward and forward skips from 0.1 to 600 seconds, then press
**Apply skip intervals**. Skipping or seeking while paused keeps playback paused.
Changing a stopped position does not start it; use **Play current recording**
explicitly. **Pause or resume recording** and **Stop current recording** remain
available as named controls.

For **A–B repeat**, enter start and end seconds within the recording, at least
0.05 seconds apart, then apply the range. Applying or clearing a range while
stopped does not play it. An active repeat rechecks the original reader's current
library and source permission before starting its next pass. **Stop** cancels
pending repeat and sleep actions, including actions waiting for progress feedback
to become quiet.

The **Sleep timer** uses elapsed time in the open reader, up to seven days.
It stops playing or paused audio and suppresses further repeat when it expires.
Closing only the controls leaves the current playback session available; closing
the Voice Reader stops its owned playback. These controls do not modify or
overwrite the rendered WAV or the retained text source.

**Synthesis rate** changes a newly rendered voice request only when the selected
voice supports it. These playback controls do not promise independent playback
speed, pitch shifting, or pitch-preserving time stretch. Windows playback reports
native MCI positions; the existing ffplay fallback reports an elapsed-time
estimate. Actual MCI tests use silent synthetic PCM. Listening quality, real voice
pronunciation and Windows 11/NVDA use require their separate acceptance.

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

The current full-library backup uses common format 4. It includes this file
automatically, together with every library's bookmarks and resume positions,
the supported typed application state and pending-import recovery records.
The database and sidecars are captured under the same library writer lock;
their sizes and SHA256 values are bound to the manifest. Saved reading places
are checked against the copied source identities before publication.

Restore creates a new data folder. Bookmark names, identifiers, timestamps and
locations are preserved exactly. Archived libraries and older sealed revisions
keep their historical reading state; opening an old place reports an unavailable
or stale location instead of silently jumping to a newer revision. Damaged,
linked or mismatched state stops the operation and preserves the existing data.

Historical formats 1 through 5 remain readable under their exact declared
layouts. The two historical format-3 layouts are distinguished by their fields;
a mixed or incomplete manifest is rejected. Formats 1 and 2 contain no personal
reading state, so retain the separate per-library reading export when moving
those older backups. A different library cannot accept that export.

Keep the reported manifest SHA256 separately from the backup. Generated audio
has its own explicit inclusion option; retained recording originals, managed
project inputs and pending source bytes needed for recovery follow their typed
producer rules. External runtimes, models and operating-system credential
secrets require separate configuration. Restored jobs do not start automatically.
No cloud account or network retrieval is involved.

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
