# Citations in an already-open Word document

This optional window connects the existing CSL workbench to an already-open
Microsoft Word document on Windows. It uses supported Word COM automation and
the application's existing citation processor. Word is not required for the
offline library, reading, bibliography export or DOCX regeneration.

**Implementation and static review are available; runtime qualification is
pending.** Required synthetic protocol and native-window checks are separate
from the optional actual Office probe. A passing synthetic test does not mean
that a Microsoft Word installation, every Word version, visual publication
layout, or NVDA has been qualified.

## Open and review

1. Open the intended document in your installed Word application and keep its
   window active. For a new citation or bibliography, place a collapsed caret
   in the main document outside an existing field or content control.
2. In the library's academic window, open **Citation styles and bibliography**.
   Select the reference scope, style, author-name convention and language,
   then choose **Preview formatting**.
3. Choose **Already-open Word…**. The window uses that same library, reference
   snapshot and existing worker. Opening it does not attach to Word, speak,
   change a document or create a second work queue.
4. **Inspect active Word document** reads the current document and lists
   Resonance-managed citation IDs and document references. Inspection does not
   grant later edit authority.
5. Choose the operation, select the reference or references, and choose
   **Preview Word change**. Read every proposed managed range.
6. Choose **Apply reviewed change** only for the intended document. Its
   confirmation defaults to **No**. Approval expires after 60 seconds; a slow
   or busy session may require a new inspection and preview.

The preview holds the exact document, window and native ranges in one bounded
helper process. The helper does not select an arbitrary file or open a Word
document. The application rechecks the source library and reviewed reference
snapshot after Word reports that it is ready, immediately before granting the
first document change.

## Supported operations

| Operation | Behavior |
| --- | --- |
| Insert citation | Adds one new, stable Resonance content control at the approved collapsed main-text caret. Current in-text CSL styles create an in-text citation; note styles create a native endnote with a managed citation range. |
| Edit citation | Uses an existing managed citation ID from Inspect, replaces its selected reference items, and rerenders the full managed citation sequence so numbering and disambiguation stay coherent. |
| Insert or regenerate bibliography | Adds one managed bibliography at the approved caret, or updates the existing managed bibliography together with all current citations. |
| Document references | Displays the managed document's inert CSL JSON and supports explicit copy to the clipboard. This data carries no library, source or future Word execution authority. |
| Keyboard workflow | Native labeled controls support Tab, button mnemonics and Space. Escape/Close requests Stop during active work and waits for the actual result before closing. No automatic Apply is bound to the default button. |

A pinpoint requires one selected reference and an explicit statement that it was
checked against its source. Publication page ranges never become citation
locators automatically. Prefix and suffix apply to one selected reference at a
time. Unknown or unavailable references are refused rather than replaced by a
similar title. A bibliography-only action uses the current selected active
references; a document with existing citations requires their references to be
within the current library selection.

The canonical processor supplies citation text and the supported bold, italic,
underline, superscript, subscript and small-caps runs. The bridge does not claim
to certify Word paragraph layout, page design or every custom CSL style. Visual
layout remains a separate publication review.

## Boundaries that remain visible

The bridge refuses read-only or protected documents, Track Changes, a different
active document/window, another active custom Undo record, changed source
metadata, edited managed text, locked managed controls, overlapping managed
ranges, and a target containing a foreign field or another content control.
Inserting inside a field or content control is also refused.

Changing between existing in-text and endnote citation families is not an
implicit migration. Choose a separate, reviewed document workflow for that
conversion.

Changes are grouped under an application-owned, uniquely named Word Undo
record. The bridge never closes somebody else's Undo group and never performs
an automatic Undo that could remove another person's edits.

The bridge never calls `Documents.Open`, `Save`, `Close` or `Quit`, and never
changes Word macro or security settings. **Word's own AutoSave can still act
according to the user's existing settings.** The bridge does not disable it
and cannot promise that the document remains unsaved.

## Results and Stop

- **Completed:** the helper observed the exact managed text, metadata and
  preserved outside content, and returned a validated immutable completion
  receipt. A later cancellation or cleanup notice does not erase this result.
- **Stopped before a final grant:** no operation was authorized by this
  session. A new action requires a fresh preview.
- **Uncertain:** the final grant may have reached Word, or mutation began, but
  no complete verified result was observed. Some or all of the changes may be
  present. Inspect Word and its Undo history. The application never retries
  this outcome automatically.

Stop remains available while the finite helper runs. The shared resource
reservation and private transport remain owned until the native helper is
quiescent and temporary cleanup is confirmed. Closing the parent or replacing
the selected library revokes queued and late callbacks.

The helper's default total deadline is 180 seconds, with an explicit maximum
of 300 seconds. It limits preview approval to 60 seconds and the final
source-attestation grant to five seconds before the first content write.
These are finite admission limits, not a claim about Word's performance.

## Existing and foreign citation data

New native controls use `resonance:cite:<stable-id>` and
`resonance:biblio:<document-id>` tags. Inert metadata uses the separate namespace
`urn:resonance:word-citations:1`.

Legacy application DOCX metadata in `urn:carr:citations:1` is preserved. Existing
Zotero or other foreign fields and content controls remain foreign; this bridge
does not silently convert or edit them. Use the existing DOCX regeneration
workflow for that legacy interchange, or the tool that owns a foreign field.
There is no claim that native Word controls and Zotero fields are interchangeable.

Malformed or edited managed metadata requires review; it is not reset or adopted
as a fresh document automatically. A saved metadata record cannot authorize a
later mutation. Each run requires a current library selection, new held-session
capture and new explicit approval.

No new application backup namespace is introduced. The document metadata is
part of the user's Word document if Word saves it. Copying document references
or restoring library state does not restore a live Word session.

## Availability and qualification

The application uses its already-pinned `comtypes` dependency and the already
installed Word registration. It does not acquire Office, download add-ins,
alter account state, or add another citation engine. Only an existing running
Word instance can be attached.

The required CI probe uses real native wx controls, the actual CSL processor,
owned Windows processes and the real library/broker, with an explicitly
synthetic Word backend. Its report states `actual_word: NOT_RUN`. Actual Office
compatibility is checked only by the separately documented opt-in disposable
document probe. Neither route establishes manual NVDA or publication-quality
layout acceptance.

There is no affiliation or endorsement implied by naming Microsoft Word or
Zotero.

## Primary API references

Retrieved 2026-11-27. These references describe the supported APIs; they are not
runtime evidence for this application.

- [comtypes client: GetActiveObject](https://comtypes.readthedocs.io/en/stable/client.html#getactiveobject)
- [Pinned comtypes 1.5.0 active-object implementation](https://github.com/enthought/comtypes/blob/1.5.0/comtypes/client/_activeobj.py)
- [Pinned comtypes 1.5.0 dynamic IDispatch implementation](https://github.com/enthought/comtypes/blob/1.5.0/comtypes/client/dynamic.py)
- [Word ContentControls.Add](https://learn.microsoft.com/en-us/office/vba/api/word.contentcontrols.add)
- [Word ContentControl.Tag](https://learn.microsoft.com/en-us/office/vba/api/word.contentcontrol.tag)
- [Word Endnotes.Add](https://learn.microsoft.com/en-us/office/vba/api/word.endnotes.add)
- [Word UndoRecord](https://learn.microsoft.com/en-us/office/vba/api/word.application.undorecord)
- [Word UndoRecord.CustomRecordName](https://learn.microsoft.com/en-us/office/vba/api/word.undorecord.customrecordname)
- [Word UndoRecord.CustomRecordLevel](https://learn.microsoft.com/en-us/office/vba/api/word.undorecord.customrecordlevel)
- [Word TextRetrievalMode](https://learn.microsoft.com/en-us/office/vba/api/word.textretrievalmode)
- [Word Range.Text](https://learn.microsoft.com/en-us/office/vba/api/word.range.text)
- [Word Application.Version](https://learn.microsoft.com/en-us/office/vba/api/word.application.version)
- [Office CustomXMLParts.SelectByNamespace](https://learn.microsoft.com/en-us/office/vba/api/office.customxmlparts.selectbynamespace)
- [IUnknown identity rules](https://learn.microsoft.com/en-us/windows/win32/api/unknwn/nf-unknwn-iunknown-queryinterface%28refiid_void%29)
