# Voice reader: find, replace and pronunciation

The Voice reader and reading copy window uses the same ReadingDocument history and source mapping as the existing reader. These tools edit the separate reading copy. They do not change an imported book, its original text, or the authority of a saved quotation.

## Find and replace (RW-12)

Open **Find in reading copy** (default Ctrl+F) or **Find and replace** (Ctrl+H). Both open the same native review dialog. Find Next and Find Previous select exact current text. Options include case sensitivity, whole words, wrap, and the reading selection captured when the dialog opened.

Literal search treats replacement backslashes literally. Regular expressions use the existing isolated regex worker; Python-style replacement backreferences are supported. Enter an empty replacement to remove matching text. Preview Replace One requires the exact match selected by Find. Preview Replace All prepares one complete, reversible operation. Apply reviewed changes commits it; Reject discards it. The reader's Undo and Redo act on the entire accepted operation.

All regex work runs on the reader's existing worker and in a bounded child process. The maximum pattern is 1,024 characters, replacement 4,096 characters, result 10,000 matches, and reading copy 2,000,000 characters. Each search has a three-second child deadline. A cancelled or stale preview cannot edit the current copy. Reopen a preview after typing, undo, source revocation, or another accepted operation.

Native multiline positions are checked against actual control text. Windows CRLF and supplementary Unicode characters are translated to Python code-point offsets before a replacement is accepted. An unverifiable native position fails visibly.

## Pronunciation dictionary (RW-13)

Open **Pronunciation dictionary** (default Ctrl+Shift+D). Add, update, remove and reorder literal or regex rules. Language and voice ID are explicit scopes; an empty scope applies generally. The selected current voice supplies initial scope values. The existing pronunciation engine applies general scopes before more specific scopes; list order applies within equal scopes.

Preview pronunciation changes shows the proposed reading layer. Apply is explicit and one Undo restores the complete layer. Rules remain available in this reader session; Export dictionary saves the existing carr.pronunciations.v1 format to a new file, and Import dictionary validates that same format. Export never silently replaces an existing file. No voice model, dictionary package, network service or pronunciation quality is inferred from these editing controls.

A dictionary preview has a ten-second total deadline in addition to each regex child deadline. Large rule sets or chapters may need to be split. The preview display is bounded; a shortened display is labelled, while the full bounded operation remains associated with the captured reading snapshot.

## Keyboard and lifecycle

The Voice reader adds a voice_reader context to the existing shared keyboard preference file. Its defaults preserve F6/F7/F8 and Ctrl+S/Ctrl+Shift+S. Reader keyboard shortcuts uses the same command chooser, collision validation, import/migration and persisted overrides as the main window and Reading Desk. Ordinary text editing and Insert/Caps Lock/NVDA prefixes remain reserved. No global hotkeys are installed.

The tools borrow the reader's single executor. Cancel and closing the tool discard unaccepted results; they never shut down that shared executor. Destroy events use the captured live owner ID because a native wrapper can already be invalid when destruction is reported. A successful dictionary export remains reported as committed if cancellation arrives after its durable return.

RW-09 output selection, RW-15 installed Windows spelling and RW-25 reading appearance are separate controllers wired into the same reader. Their respective documentation and qualifications apply. These changes do not claim special foreign-code interpretation, live acoustic pronunciation quality, or manual NVDA qualification.

## Verification

Portable selectors:

- tests/speech/test_reading_tools.py
- tests/ui/test_reading_tools.py

Actual Windows source probe:

    python -I -B scripts/windows_reading_tools_smoke.py --source . --output ci-results/reading-tools-source.json

Actual installed probe, without injecting checkout source:

    python -I -B scripts/windows_reading_tools_smoke.py --expect-installed --output ci-results/reading-tools-installed.json

The native probe exercises actual wx controls and OS key dispatch, real isolated regex children, source-preserving history and dictionary/store persistence. External file chooser answers are controlled temporary fixtures. It requires no audio endpoint and performs no speech inference. Manual NVDA remains a separate acceptance activity. The final composed source/native gates must run before claiming these new controls qualified.
