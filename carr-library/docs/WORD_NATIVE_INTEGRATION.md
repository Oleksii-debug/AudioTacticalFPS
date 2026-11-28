# V3-08: optional Word citation review

## Scope and entry point

**Academic → Review citations in open Word…** uses the existing academic-library
controller, pinned CSL processor, selected workspace and the desktop's existing
single worker. The offline formatter, DOCX exporter and ordinary library
operations do not require Microsoft Office or import COM.

Open Word and the intended document yourself. Inspect attaches to an already
running instance through GetActiveObject; it does not launch Word or open a
document. Where several Word instances are running, the registered instance
may differ from the foreground window. The review displays the captured
document's exact FullName. Inspect and approve the intended name.

The finite implementation supports inserting an inline citation, editing a
stable CARR citation ID, inserting one CARR bibliography, and selectively
regenerating the captured CARR fields. Installed CSL note styles use native
**endnotes** with continuous Arabic numbering starting at 1. Other numbering
policies are rejected without changing Word's settings. Moving existing
citations between inline and note stories is not a style-only operation.
Protected, read-only and active Track Changes documents are not modified.

Fields are type wdFieldAddin (81). Their explicit CARR_CITATION_ or
CARR_BIBLIOGRAPHY_ code and strict carr-word-field/1 Data carry stable IDs,
workspace, source/controller snapshot digest, pinned processor identity,
normalized citation clusters and result hashes. This metadata describes
provenance; it does not grant source approval. Historical locators require a
new explicit review. A changed source binding requires an explicit citation
edit, even if the bulk historical-review checkbox is selected. Publication
extent is never inferred to be a verified pinpoint.

Only CARR fields' Result.Text and Data are updated. New fields and native notes
are created only at the captured collapsed main-story cursor. Foreign fields
are not evaluated, updated or used as writable ranges. Ordinary main-story
prose and foreign endnotes are checked again after the writes. A manually
modified CARR result, malformed marker, duplicate ID, changed active document,
changed cursor/preimage or changed library invalidates the review.

## Review and lifetime

Preview resolves current references from the selected library and runs the
same local CSL formatter used by the offline integration. Apply confirms the
exact named document on the GUI thread. The captured scope, cancellation and
plan are checked again after that modal returns, before the existing dispatcher
clears its cancellation event. The worker verifies the source and native
preimage, consumes the plan, and rechecks current scope and document before
each write. A consumed plan cannot be automatically replayed.

A Word update is **not an atomic transaction**. If a write fails or source
authority changes after the first possible write, the result is explicitly
incomplete and the document can contain a partial update. Inspect it in Word
before preparing another review. The bridge does not run automatic Undo.
It never invokes Document.Save, Document.Open, Application.Quit, a complete
Fields.Update, a foreign Field.Update, a macro, an account flow or a network
request. Word's own independently configured AutoSave policy remains in effect.

Every native proxy is retained by one STA worker. Closing the dialog revokes
GUI callbacks and queues disposal after the actual work on that same existing
worker. Desktop shutdown also retains an explicit finalizer. A failed release
remains registered for a retry; a worker is not declared stopped merely because
its dialog closed.

Read-only calls can request COM cancellation. A deadline or Cancel stops
subsequent calls. **An in-flight mutating RPC is never cancelled, detached or
treated as completed before it returns**: Microsoft's contract permits a
server to continue work after COM cancellation. If Word is unresponsive during
a write, the worker and proxies remain owned. This is not a hard wall-clock
termination guarantee. After the last acknowledged write, a bounded read-only
verification can preserve a completed receipt despite a late Cancel.

Limits are 500 CARR fields, 2,000 inspected native fields, 500 native endnotes,
two million aggregate main-story/endnote characters, 256,000 UTF-8 metadata
bytes per field and bounded aggregate metadata/update bytes. The existing CSL
reference and cluster limits remain in force. No persistent Word authority,
Office credentials or document copy is added to the library or backups.

## Source and test inventory

New production files:

- academic_integration/word_fields.py: strict passive field records and scoped preview/apply service.
- academic_integration/word_com.py: optional existing-instance-only native adapter and retained STA lifecycle.
- ui/word_citations.py: accessible review controls and the existing-worker orchestration.

Tests use actual SQLite, original source assets, current backup/restore,
embedded CSL and the actual DesktopExecutor. The Word object model is an
explicit stand-in. It rejects writes to ordinary ranges and calls to the
complete field evaluator. Negative controls cover source and document changes,
stable IDs, native-note indices, stale locator approval, malformed metadata,
partial writes, late cancellation and same-worker disposal.

Required portable selectors:

- tests/academic_integration/test_word_fields.py
- tests/academic_integration/test_word_com.py
- tests/ui/test_word_citations.py

Cross-fixture loading is file-bound; product imports still resolve from the
installed package during importlib/-I installed gates.

At this source checkpoint, new cases and native probes are **NOT_RUN**.
A modeled pass must never be reported as Office compatibility or human
screen-reader acceptance.

## Native qualification

The same script has two deliberately explicit modes.

Native wx/worker/CSL with a modeled Word instance (no Office required):

    python -I -B scripts/windows_word_citations_smoke.py --expect-installed --mode modeled --report word-ui-installed.json

For source qualification, replace --expect-installed with
--source <exact carr-library checkout>. The probe verifies actual module bytes
against that checkout, and installed mode also verifies the owning wheel
RECORD. Native widgets, keyboard Space dispatch, explicit fixture approval,
close, and direct destruction with queued inspection are exercised.
Its report says MODELED_NOT_QUALIFIED for Office.

Actual Office qualification is opt-in and requires a disposable document that
the operator has already opened in desktop Word. Give it exactly one paragraph:

    CARR Word integration aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa

The final paragraph mark is native Word CR. It must contain no existing fields
or endnotes. Use its exact Word FullName below. Do not use a real manuscript.

    python -I -B scripts/windows_word_citations_smoke.py --expect-installed --mode office --allow-word-write --expected-document "C:\exact\CARR-probe.docx" --fixture-token aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa --style apa --report word-office-inline.json

For native notes, manually open a fresh disposable marker document and set
its endnote policy to continuous Arabic numbering starting at 1; run the
same command with --style chicago-notes and that document's exact FullName.
The probe performs reviewed insertion, edit, bibliography and refresh through
the actual native dialog and desktop worker. It moves only the explicitly
approved fixture cursor for the later bibliography. Its fixture confirmation
callback is disclosed in the report; it is not human acceptance.

No script creates, opens, saves, closes or quits Word. A missing/unavailable
Office prerequisite cannot yield an Office PASS. Diagnostics are written
before GUI/native imports. The watchdog prints a stack without killing an
outstanding Office write. Actual Office execution remains **NOT_RUN** until
these reports are produced on the qualified Windows/Office combination.

## Primary API references

- [Word Field.Data](https://learn.microsoft.com/en-us/office/vba/api/word.field.data)
- [Word Field.Result](https://learn.microsoft.com/en-us/office/vba/api/word.field.result)
- [Word Fields.Add](https://learn.microsoft.com/en-us/office/vba/api/word.fields.add)
- [Word WdFieldType](https://learn.microsoft.com/en-us/office/vba/api/word.wdfieldtype)
- [Word Endnotes.Add](https://learn.microsoft.com/en-us/office/vba/api/word.endnotes.add)
- [Word Endnote.Index](https://learn.microsoft.com/en-us/office/vba/api/word.endnote.index)
- [GetActiveObject](https://learn.microsoft.com/en-us/windows/win32/api/oleauto/nf-oleauto-getactiveobject)
- [COM cancellation semantics](https://learn.microsoft.com/en-us/windows/win32/com/canceling-method-calls)
- [CoEnableCallCancellation](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-coenablecallcancellation)
- [CoDisableCallCancellation](https://learn.microsoft.com/en-us/windows/win32/api/combaseapi/nf-combaseapi-codisablecallcancellation)
- [comtypes client API](https://comtypes.readthedocs.io/en/stable/client.html)

Reuse: strict cluster recovery follows reviewed PR152 regeneration43758cf,
adapted to current formatter and source-controller contracts. No donor COM
integration existed, no second citation engine was introduced, and the
existing optional Windows comtypes 1.5.0 dependency is reused.
