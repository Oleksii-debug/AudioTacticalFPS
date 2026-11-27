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
