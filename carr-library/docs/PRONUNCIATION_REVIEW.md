# Pronunciation rules for reading copies and Studio spoken lines

The Voice reader provides an explicit review and apply workflow for the existing
`ReadingDocument` pronunciation dictionary. It edits a reversible reading copy.
It does not rewrite an imported book, approve extracted text, change saved source
quotations, or establish how a particular voice will pronounce the result.

## Open and review

1. Open **Voice reader** from the Voice menu, or open a retained source in the
   reader's existing narration workflow. Wait for its source/voice operation to
   finish. A pronunciation edit does not require a synthesis engine.
2. Select text if the change should cover only a passage. Choose **Pronunciation
   rules**, or use its **Alt+N** access key. **Ctrl+Shift+P** remains available to
   the separate command palette.
3. The dialog loads the saved library dictionary. Add a rule with a pattern and
   replacement, or import a reviewed dictionary. **New rule** clears the entry
   fields. **Add rule** or **Update rule** retains an entry in the working list;
   merely typing in its fields does not apply or save it.
4. Choose **Whole reading copy** or **Selected text only**. An empty selection
   cannot be previewed as a selected-text scope. Choose the language used to match
   scoped rules; the selected reader voice ID is shown as an exact, read-only
   scope.
5. Choose **Preview rules** (Alt+V). Review the read-only current and proposed
   text. Status identifies the character range in the reading copy. These are
   reading-copy character positions, not original-book quotation locations.
6. Choose **Apply reviewed change** (Alt+A). This creates one undoable reading
   edit. The dialog rejects a preview if text, selection, voice, language, rule
   order, rule fields, source authority, or library scope changed meanwhile.
7. **Undo last apply** (Alt+U) reverses that dialog's last applied edit while its
   exact resulting reading state is still current. The Voice reader's existing
   **Undo reading edit** and **Redo reading edit** retain the general history
   after the pronunciation dialog closes.

Apply and undo stop current reader playback and invalidate its previous audio.
They preserve the source anchor and save through the existing reader recovery
store. If recovery saving fails, the dialog says the change remains in memory
and asks for **Save As**; the edit remains undoable. Close the pronunciation
dialog and use the reader's existing F6 control to listen with an explicitly
selected local voice. This workflow does not start speech automatically.

## Rule semantics and limits

Rules use the existing `PronunciationRule` engine. A literal rule replaces its
text exactly, optionally ignoring case. A regular-expression rule uses the
existing isolated Python regex worker with a one-second deadline per pattern
and cooperative cancellation. Invalid expressions or a cancelled/timed-out
preview leave the reading copy unchanged.

An empty rule language or voice ID applies to any such value. Nonempty values
must match exactly. General rules run first, then language-only rules, then
voice-only rules, then rules scoped to both. The list order is stable within
each of those groups. **Move up** and **Move down** change that order; later rules
see earlier replacements. The matching language field is independent of the
rule-entry language field.

The existing limits remain: 1,000 rules, 1,024 characters per pattern, 4,096 per
replacement, 128 per language, 2,048 per voice ID, and 2,000,000 characters in a
reading copy. Selected dictionary files are bounded to 8,000,000 bytes and read
in cancellable chunks. These substitutions do not claim stress-mark, phoneme,
SSML, or language-quality equivalence with another product.

## Save, import, export, and recovery

**Save library dictionary** saves the current rule list independently of applying
it to text. The dictionary lives in the existing library state at
`state/speech/pronunciations.json` and is included by ordinary library backup and
restore. Each save checks the digest loaded by this dialog. If another dialog or
process changed the saved dictionary, saving stops with a request to reload;
it does not overwrite those unseen changes.

**Reload library dictionary** discards the working rule list and reloads the
currently saved rules. **Import dictionary** replaces the working list for
review, without applying it or saving it into library state. **Export new
dictionary** publishes the current list only to a new destination and refuses
to overwrite an existing path. Saving/exporting does not accept an entry whose
form was changed without **Add** or **Update**. An atomic save/export that already
completed remains committed if cancellation arrives immediately afterwards.

The accepted interchange format is UTF-8 JSON with schema
`carr.pronunciations.v1`, using the existing `export_dictionary` and
`import_dictionary` functions. It is not a Balabolka dictionary-file importer.
Duplicate JSON fields, unknown schemas/fields, invalid rule data, linked paths,
and oversized files are rejected. A regex supplied by a dictionary is evaluated
only when preview is explicitly requested.

**Cancel operation** (Alt+N within this dialog) requests cancellation on the
Voice reader's existing worker. Closing a busy dialog cancels its operation and
suppresses late UI callbacks. Closing with unsaved rules offers Save, Discard,
or Cancel. Library relocation or loss of source authority invalidates queued
work; it cannot apply a previously approved preview to a different source.

## Studio: one spoken line at a time

Open **Audio Studio → Review spoken-line pronunciation**. Select a saved project,
scene and spoken line, then choose **Open selected line**. These are the existing
`Scene.lines` and `Line.text` objects. Audio clips have no implicit transcript and
are not treated as narration text.

The line's role supplies its exact voice ID and initial rule-matching language.
Select a range in the read-only line text if needed, then choose **Review
pronunciation rules**. This opens the same review dialog and the same saved
library dictionary used by the Voice reader. Its **Apply** and **Undo last
apply** operate on a local reading draft. Close that review and choose **Save
spoken line** to publish the edit as one new project revision. Save and Discard
are explicit; project/scene/line choices remain disabled while a draft is dirty.

**Discard unsaved line changes** restores the opened line without changing the
saved project. **Undo last saved change** creates a further project revision
restoring that editor session's previous line text. It is available only while
the exact saved project remains current; it never overwrites later edits by
another editor. Earlier revisions remain in the existing project history after
the dialog closes, although the session's one-click undo target is not restored
on reopening.

Saving compares the complete reviewed project fingerprint and expected revision
inside the existing `ProjectStore` lock. A same-revision external replacement is
therefore rejected as well as an ordinary later revision. Original script,
source references, other lines, role settings, clips, music, pauses, schema and
sample rate remain unchanged. An ordinary Studio Save with an unchanged script
retains the edited performance. Explicitly rewriting the script continues to use
the existing script parser and creates a new performance revision.

The line draft is limited by the existing 100,000-character `Line` contract.
Native selections are mapped from wx's normalized display text back to retained
text, so untouched CR/LF sequences and Unicode characters remain exact. This
follows the [wx.TextCtrl text-format and selection contract](https://docs.wxpython.org/wx.TextCtrl.html).
Project enumeration is cancellable between bounded project reads and permits at
most 1,000 reviewed projects and 32 MiB of aggregate saved/canonical project JSON.
The whole list is checked before loading; content growth is checked again after
parsing. A collection beyond that review budget receives an explicit error,
without claiming support for a larger collection. All work uses the existing Desktop shared worker
and cancellation event. Closing waits for its owned operation to finish; an
already committed save remains saved if a later cancellation is requested.

Project edits and historical revisions use ordinary library backup/restore.
They do not create a new dictionary format or rendering engine. An explicitly
requested subsequent Studio render consumes the revised spoken line and emits
its existing measured timeline; earlier completed job outputs remain intact.

## Verification and qualification boundary

The source suites exercise actual core/dictionary functions, a real shared
worker with queued UI completions, and the production Voice-reader hook. They
cover selected Unicode/CRLF ranges, exact stale-preview rejection, cancellation,
closed-window lifetime, one-edit undo, preserved source bytes and quotation
lineage, concurrent dictionary saves, no-clobber export, and dictionary
backup/delete/restore. Native-widget stand-ins assert that worker callbacks do
not read or modify UI controls.

`scripts/windows_pronunciation_smoke.py` is the separate installed Windows probe.
It activates the bundled runtime before importing wx, checks exact installed
module bytes, and uses a normal native MainLoop with real keyboard events to
open the reader's dialog, preview/apply/undo selected text, and save/import/export
a synthetic dictionary. Its source is synthetic, and its provider only
enumerates a fixture voice. File selection and confirmation decisions are
controlled fixtures and are reported separately. A source-suite pass does not
mean this Windows probe ran. Neither test proves NVDA behavior, human voice
quality, or physical keyboard/file-picker interaction.

The Studio source suites additionally exercise real project histories,
same-revision replacement, source and clip preservation, shared-worker handlers,
portable backup/delete/restore, and two actual FFmpeg render jobs using synthetic
tone fixtures. Those tones prove text-to-job binding and artifact preservation,
not pronunciation quality. `scripts/windows_studio_pronunciation_smoke.py` is the
separate installed native Studio gate: it dispatches the production menu command,
uses real keyboard controls through a normal MainLoop, checks selected
Unicode/CRLF edits, saves and undoes revisions, and rejects a later project
replacement. It performs no synthesis and reports human/NVDA qualification as
not run.

The reviewed PR127 narration editor informed the UI comparison, but its
alternative narration/project model is not used here. This workflow reuses the
canonical reading-copy engine; the Studio adapter only publishes an approved
spoken-line draft through the existing versioned project store.
