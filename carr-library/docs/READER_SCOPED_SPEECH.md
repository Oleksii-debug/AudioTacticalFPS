# Reading units through the existing Voice reader

The Voice reader provides explicit **Read current word**, **Read current logical
line**, **Read current sentence**, and **Read current paragraph** controls.
The current cursor or the start of the current selection determines the unit.
These actions select one nonempty span and invoke the same local narration
worker and player as **Read selection / all**. A missing span never falls back
to reading the entire document.

The **Text navigation unit** choice controls **Previous unit**, **Select current
unit**, and **Next unit**. These three controls move the selection without
starting speech and do not wrap past either end. A logical line follows stored
line breaks, independently of visual text wrapping. Whitespace at a unit's
boundary is excluded; spacing inside the selected span is unchanged.

The reused PR152 navigation rules preserve combining marks, Ukrainian
apostrophes and joined/hyphenated words. Sentence boundaries use punctuation and
line breaks. These are deterministic navigation aids, not linguistic parsing,
word timing or quotation evidence. Abbreviations and languages without word
separators may require an explicit selection.

## Existing source and lifecycle boundaries

Navigation runs on the current reader worker and captures the exact document,
tab identity, source digest and cursor selection. Before the result reaches the
native editor, the current source/library scope and reading revision must still
match. The selection is checked again after focus callbacks and immediately
before narration. Stop, Close, moved cursors, edited text and replacement tabs
revoke pending results. Existing speech completion, playback quiet admission,
saved ranges, source lineage, undo, tabs and Save All remain in control of their
existing services.

The existing native offset helpers map Windows UTF-16/CRLF positions through
actual control prefixes. A separate offset convention or hidden text
normalization is not introduced.

All seven new commands are in the existing `voice_reader` keyboard context
with empty default shortcuts:

- `voice_reader.read_word`
- `voice_reader.read_line`
- `voice_reader.read_sentence`
- `voice_reader.read_paragraph`
- `voice_reader.previous_unit`
- `voice_reader.current_unit`
- `voice_reader.next_unit`

Existing assignments, including the optional playback command, are preserved.
Keyboard settings can assign keys explicitly; conflicts with Stop, editing and
screen-reader keys continue to be rejected.

## Qualification

New suites:

- `tests/speech/test_navigation.py`
- `tests/ui/test_reader_navigation.py`

The source and installed Windows probe is
`scripts/windows_reader_navigation_smoke.py`. Run with `python -I -B`,
`--output <receipt.json>`, exactly one of `--source <carr-library>` or
`--expect-installed`, and `--expected-device-identity <verified identity>`.
The probe uses actual wx controls and native key dispatch, the real registry,
reader worker and WavePlayer, with an explicitly synthetic provider producing
silent PCM. The selected endpoint must match exactly; there is no default-route
fallback. It checks the imported module bytes against the checkout and, for the
installed lane, the actual owning wheel RECORD.

Source and modeled tests, native playback and acoustic quality are distinct
evidence. This source checkpoint is **NOT_RUN** until the corresponding exact
candidate gates execute. The native probe does not qualify model pronunciation,
speech quality or manual NVDA behavior.
