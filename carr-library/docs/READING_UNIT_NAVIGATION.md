# Exact word, sentence and paragraph navigation

The Voice reader's **Text unit** row selects or reads a bounded part of the
current editable reading copy. It uses the existing reading document, local
voice registry, speech worker and draft recovery. It does not introduce another
reader, speech engine, playback queue or saved project format.

## Using the controls

1. Open the standalone Voice reader or an authorized book/chapter narration
   copy. Put the cursor in the text you want to examine.
2. Choose **Word (letter-based)**, **Sentence (punctuation)** or **Paragraph
   (stored line)**.
3. **Select current unit** highlights the exact unit containing the cursor.
   **Previous unit** and **Next unit** move the selection without speaking.
4. Choose an actually installed local voice and press **Read current unit** to
   select and synthesize only that unit. The existing F7 and F8 controls pause
   playback or stop synthesis/playback. Existing all/selection and from-cursor
   commands remain available independently.

An exact existing unit selection anchors repeated Current/Previous/Next actions.
For another nonempty selection, its start is the anchor. An empty selection uses
the cursor. At the exact end of the final unit, the document-end cursor can still
select that unit. A cursor in surrounding whitespace or a symbol-only gap has
no current word; choose Previous/Next or another unit explicitly. At either
document boundary, navigation reports the boundary and leaves the selection
unchanged. It never wraps to the opposite end.

Every new control has a native accessible name. Selection returns focus to the
editor, and the status identifies the unit number and the boundary rule. Unit
planning runs on the reader's existing background worker. No speech starts on
open, on a mode change, or after a selection-only action.

## Boundary rules and limits

| Unit | Existing rule reused | Explicit limit |
| --- | --- | --- |
| Word | Spelling review's Unicode letter/mark ranges, with internal apostrophes and hyphens | Up to 20,000 letter-based words. Numbers and symbol-only strings are not words in this profile. A continuous unspaced letter sequence is one unit. |
| Sentence | Document conversion's punctuation-plus-whitespace splitter | Up to 10,000 nonempty parts. Abbreviations such as `Dr.` may form a separate unit; this is a heuristic, not a language model. |
| Paragraph | Document conversion's stored-line splitter, including CR/LF line boundaries | Up to 10,000 nonempty parts. Visual wrapping and inferred document structure do not create paragraphs. |

The current reading copy has the existing two-million-character limit. Leading
and trailing whitespace is excluded from each highlighted unit without editing
the text. Blank units are omitted. A limit or cancellation rejects the operation;
there is no accepted partial plan or automatic whole-document fallback.

## Exact scope, source and recovery

The operation captures the current reading-document identity, revision, text
hash, cursor, selection and chosen unit. Reading also captures the selected
voice, language, rate and volume. The reader checks the current library object
and directory identity and repeats its existing source authorization guard.
After background planning, all captured values must still match before a native
selection is applied. A changed cursor, document, source, library, voice or
setting requires another explicit action.

Native positions are translated through the existing verified Unicode/line-end
mapping. The selected native range must equal the exact original text slice;
failed selection restores the previous native range. Before synthesis, the
reader again checks the selected text and document/voice binding. An empty,
changed or unselectable range cannot become an all-text request.

Selection and navigation do not edit the reading copy or its undo history. The
existing draft store saves the character cursor and voice settings. A recovery
write error is reported while the current selection remains usable. Reopening
recovers the saved cursor through the existing reader mechanism; unit mode and
selection are session controls, not a new persisted schema. Original retained
source bytes, source locators and quotation approval are unchanged. A narration
copy remains non-authorizing quotation material.

## Evidence and qualification

`tests/speech/test_reading_units.py` has 34 portable cases for exact Unicode
ranges, CRLF, gap/boundary behavior, repetition, stale plans, finite budgets and
cancellation. `tests/speech/test_reading_unit_handlers.py` has 28 cases executing
the actual reader closures and existing worker contract. They cover queued scope
changes, native selection refusal, no widened speech request, real draft/source
lineage and unchanged undo history. Three cases synthesize the exact requested
unit through the actual installed SAPI provider on Windows or actual Flite on
POSIX, and verify nonempty PCM output. This is synthesis evidence, not listening
or physical playback qualification.

The separate installed `scripts/windows_reading_units_smoke.py` probe exercises
real wx controls, foreground/focus ownership and paired native key events. Its
original retained fixture includes a long line, Unicode and line endings; it
checks exact unit selection, adjacent navigation, boundary/gap refusal, recovery
and one explicit actual-SAPI synthesis. It verifies installed module identities
and synthesized text/PCM hashes. Its only audio substitution retains the
existing quiet-dispatch playback callback without executing it, so this probe
does not claim MCI or a physical output device. Existing actual WavePlayer/EPUB
gates remain separate. Native qualification is **NOT_RUN** until that exact
Windows artifact executes successfully.

`tests/ui/test_native_reading_units_driver.py` executes eight portable cleanup
cases against the actual probe helpers. Failed key release retains the exact
key/modifier pair for retry; cleanup errors cannot bypass the outer-loop exit,
and foreign or inactive dialogs are not ended. These cases do not simulate a
successful native key event or qualify Windows modal behavior.

Continuous reading with automatic unit advance, voice-aligned word highlighting,
word timestamps, multilingual linguistic segmentation and human NVDA/listening
acceptance are not established by this feature or these portable results.
