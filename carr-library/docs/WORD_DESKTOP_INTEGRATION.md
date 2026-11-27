# Optional desktop Word citation controls

This implements the Word portion of **V3-08, Professional academic integration
and synchronization**, on the existing CSL document workflow. V3-07 is the
large-library scale requirement.

## Explicit user workflow

1. In Citation styles, preview the reviewed references and export **Editable Word
   citations and bibliography**. Open and save that local DOCX in desktop Word.
2. Return to Citation styles and choose **Open Word citation controls**. Choose
   **Connect and create working copy** and a new DOCX filename. The original must
   be saved. An existing destination is refused.
3. Activate the new copy in Word. Place a collapsed caret in ordinary text in its
   managed body, outside an existing citation. Return to the controls and choose
   the reviewed reference, an optional explicit locator, and **Insert**.
4. Choose an existing stable occurrence to **Edit** its reference items, or
   **Refresh citations and bibliography**. The document retains its saved style
   and language. The selected library metadata is checked again before changes.
5. Review the copy and save explicitly in Word. The application never saves the
   original, saves Word automatically, starts Word in production, or quits the
   user's Word process. Each successful change is grouped in a named Undo record.

The document's original reference and occurrence IDs survive connection,
editing and refresh. Note styles use native Word endnotes and their existing
occurrence bookmarks; in-text citations and the bibliography use named rich-text
content controls. There is no second citation database. The current
carr-citation-document/1 snapshot, original CSL processor/style hashes and
unverified quotation/visible-edit flags are retained.

A snapshot checksum detects corruption, not authorship or source authenticity.
Entered locators are not verified quotations. A changed source with a retained
locator requires a fresh explicit item edit; publication page ranges are never
used as quotation locators.

## Supported boundaries

Initial connection accepts the application's editable citation export, with its
original headings, body text, reference bookmarks and formatted citation text,
or a previously connected copy with intact managed controls. Initial visible
edits that cannot be bound exactly are refused before opening a new copy.
Ordinary prose edits inside a connected managed body remain in Word; refresh
updates citation ranges and the bibliography, not that prose. The regenerated
snapshot captures plain body text for the existing separate regeneration API;
it does not promise to reproduce all Word layout or visible formatting.

Unsupported cases are refused: headers/footers and other insertion stories,
noncollapsed selections, tables, ordinary Word fields, tracked revisions,
protected/read-only files, footnotes, foreign or duplicated content controls,
missing native endnotes, changed citation text, ambiguous custom XML, active
embedded content, external relationships, network/device/stream paths, links,
and another active document. This adapter does not guess Zotero or Word built-in
citation-field semantics and does not silently discard their metadata.

Both the selected metadata and the exact managed Word document/selection are
checked after formatting and immediately before the first change. A current
workspace permission check is repeated at native mutation checkpoints.
Cancellation or failure inside the edit requests Word Undo and restores the old
citation XML when required. If Word cannot confirm recovery, the UI asks the user
to inspect or close the working copy without saving; the untouched original is
the recovery source. No automatic retry can duplicate a committed citation.

Word automation runs through the existing desktop run_job, shared progress,
and cancellation event. Each worker call creates and releases its own STA COM
apartment; no COM proxy crosses into the wx thread. Progress feedback is advisory.
Closing waits for the actual call to settle, including when the host suppresses
its ordinary callbacks during shutdown. Office can block an individual COM call
on its own modal dialog; cancellation is cooperative between calls, and this
application never kills a user's Word instance.

## Optional runtime that the current packager can install

The default offline core does not require Office or a Microsoft account. A
reviewed Word bundle uses these complete files with the **existing**
packaging/build_windows_bundle.py and installer:

- packaging/windows-word-cp311.lock: every current base pin plus pywin32 312.
- packaging/windows-word-roots.in: the corresponding explicit roots.
- packaging/windows-word-sbom.cdx.json: the same graph and the added wheel.
- Existing wheel-notices/SOURCES.json: exact upstream COM and WIN32 notices.

The one added binary is pywin32-312-cp311-cp311-win_amd64.whl, SHA-256
d11417d84412f859b722fad0841b3614459ed0047f7542d8362e77884f6b6e8a.
Its reviewed upstream b312 source is commit
2a277cb5552756c2b4d42b524dc36d25e0bb6354.

Prepare a complete admitted local wheelhouse through the existing dependency
preparation workflow. Select the Word lock and matching SBOM when building:

    python packaging/build_windows_bundle.py --wheel APP.whl --wheelhouse APPROVED_WHEELHOUSE --lock packaging/windows-word-cp311.lock --sbom packaging/windows-word-sbom.cdx.json --python-archive APPROVED_PYTHON.tar.gz --output NEW_BUNDLE

Production release builds also use the existing exact-source verification,
launcher, OCR and delivery gates. The builder already puts **all** explicitly
admitted wheels in install.lock; an optional project extra is not the sole
delivery mechanism. This profile preserves the base graph, does not implicitly
add a semantic model, and does not change default installation behavior. A
combined reviewed runtime can retain the same pywin32 component in its own full
lock/SBOM, without adding a second installer.

Pywin32 contains mixed upstream licences. Mark Hammond and contributors' COM and
WIN32 notices are included, and the complete wheel—including all its
License-File metadata and packaged licence files—is preserved. Those two
notices are not a claim that the entire distribution has one licence. No
post-install registration script, COM server registration, runtime download or
new automation engine is used.

## Qualification evidence

The controlled tests exercise the real current database, CSL processor, DOCX
metadata, export publication and actual worker/Event lifetimes. Their Word/wx
boundary is controlled and is **not** Office or assistive-technology
qualification. This source candidate has not been locally executed during the
executor outage.

The optional native probe creates only synthetic documents in a new empty Word
instance:

    python scripts/windows_word_citations_smoke.py
    python scripts/windows_word_citations_smoke.py --require-word

Without Windows, the optional bridge or registered Word, the receipt is
NOT_RUN with zero checks. --require-word then fails. Installed Word failures
are FAIL, not successful unavailability. With actual Office, the probe must
complete all 20 named assertions across APA and a note style: exact new copy,
native controls, insert/edit with stable IDs, one actual Word Undo including
XML, bibliography refresh, native story structure, actual save/readback,
unrelated active-document refusal, and unchanged original/prose.

The native probe is separate from normal application startup and may explicitly
create/close only its own empty Word instance. It never opens a real user
document. NVDA, Word-version compatibility and physical user workflow remain
NOT_RUN unless separately observed. A fake COM boundary cannot upgrade them.

## Primary API and upstream references

- Microsoft: [ContentControls.Add](https://learn.microsoft.com/en-us/office/vba/api/word.contentcontrols.add),
  [ContentControl.Tag](https://learn.microsoft.com/en-us/office/vba/api/word.contentcontrol.tag),
  [Endnotes.Add](https://learn.microsoft.com/en-us/office/vba/api/word.endnotes.add).
- Microsoft: [UndoRecord.StartCustomRecord](https://learn.microsoft.com/en-us/office/vba/api/word.undorecord.startcustomrecord),
  [Document.Undo](https://learn.microsoft.com/en-us/office/vba/api/word.document.undo),
  [CustomXMLPart.LoadXML](https://learn.microsoft.com/en-us/office/vba/api/office.customxmlpart.loadxml).
- Microsoft: [Documents.Open](https://learn.microsoft.com/en-us/office/vba/api/word.documents.open),
  [custom XML datastore properties](https://learn.microsoft.com/en-us/dotnet/api/documentformat.openxml.customxmldataproperties.datastoreitem?view=openxml-3.0.1).
- Upstream: [pywin32 312](https://pypi.org/project/pywin32/312/) and
  [exact b312 source](https://github.com/mhammond/pywin32/tree/2a277cb5552756c2b4d42b524dc36d25e0bb6354).
