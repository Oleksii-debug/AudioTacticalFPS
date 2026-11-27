# Explicit speech preparation

Speech preparation proposes edits to a separate reading copy. It never changes
the imported original, citation text, quotation authority, or source file. It
does not start speech, select a voice, fetch data, translate, calculate a result,
or guess a foreign name. Accepted text uses the existing narration revisions,
Undo and recovery draft; the ordinary full backup already preserves that state.

## Review contract

Verbatim is the default and means exact text, including line endings, whitespace,
digits and punctuation. Speech mode requires an explicit Ukrainian (uk-UA) or
English (en-US) profile. Other languages are refused rather than substituted.
Each expansion is opt-in. The native workflow is **Prepare speech text… → Preview → inspect both
texts and notices → Apply**, with a separate Undo. No edits are applied merely by
opening the view, selecting a profile, or completing a worker.

The pure proposal retains source/output SHA-256 and nonoverlapping original
spans. Apply must also bind the exact current controller, revision, full reading
text, selection, range and option snapshot. A late or edited proposal is refused.
A new preview does not modify an earlier accepted revision.

## Finite supported rules

- ASCII cardinal integers: signed 0 through 999,999,999. Leading zeroes remain
  literal. An explicit digit-by-digit mode preserves up to 18 digits, including
  leading zeroes. Decimal fractions have at most six places. The decimal marker
  is the selected locale's marker or an explicit dot/comma override; no thousands
  separator is inferred.
- English decimals read their fractional digits. Ukrainian cardinals read an
  explicit whole/fraction form, retaining the selected decimal precision.
- Dates: exact YYYY-MM-DD, or the explicitly selected locale format
  DD.MM.YYYY / MM/DD/YYYY, validated against the actual calendar. The proposal
  names the day/month and labels the cardinal year. Other date-like sequences
  stay literal, even when arithmetic reading is selected.
- Case-sensitive quantities: m, cm, mm, km, kg, g, mg, s, ms, min, h, Hz, kHz,
  MB, GB, l, ml, %, °C, °F, km/h, m/s, m² and m³. Ukrainian mode also accepts
  the listed Ukrainian equivalents in the source module. Quantity labels use
  nominative forms with explicit integer/fraction agreement; the system does
  not infer the grammatical case of a surrounding sentence or convert values.
- Unambiguous finite abbreviations: e.g., i.e., etc., approx. in English;
  напр., прибл., і т. д., і т. ін. in Ukrainian. Titles, ambiguous short forms
  and names stay literal. Use the existing explicitly reviewed pronunciation
  dictionary for foreign names or additional project-specific replacements.
- Numeric arithmetic with up to 16 operators: +, -, −, ×, *, ÷, /, =, <, >,
  ≤, ≥ and ^. Operators are spoken, never evaluated. Unselected arithmetic
  stays literal. Expressions resembling dates/versions are conservatively
  retained; use explicit spaces or manual reading-copy edits to disambiguate.
- A finite list of symbol names, including %, &, @, #, $, €, £, ±, ∞, √, ∑
  and ∫. A currency symbol names the symbol; it does not infer a country.
  URLs, email addresses, inline code, identifiers and scientific notation
  remain literal.

## Explicit read/skip controls

Keep preserves each recognized structure exactly, including its numeric body.
Read adds visible spoken labels. Skip removes only recognized source spans and
preserves line boundaries. Supported markup is:

- Markdown ATX headings, levels one through six;
- simple pipe tables with a separator row and consistent columns, at most 32
  columns and 2,000 rows; escaped pipes or inline code in a requested table
  transformation require manual review;
- footnote references/definitions using a bounded ASCII identifier and indented
  continuations;
- fenced code blocks; Read labels the block but keeps the code body verbatim.

Unclosed code cannot be read/skipped automatically. Other rich-document,
mathematical, HTML or markup structures are outside this profile. The editor
does not pretend to recover table semantics from arbitrary aligned prose.

## Resource and evidence boundary

One selected passage is at most 200,000 characters, 50,000 recognized tokens and
5,000 edits. Prepared output is at most 1,000,000 characters. Cancellation,
inclusive monotonic deadlines and malformed/lossy supported structures fail the
whole proposal; there is no partial-success Apply. A large reading copy may be
handled through explicit selected passages.

The core, current native editor/session integration, source tests and native
acceptance script are **NOT_RUN** while the local executor is unavailable. Language rules are deterministic text transformations;
they do not qualify speech intelligibility, any installed voice, or a native UI.

Reference design checks: Unicode CLDR's
[Ukrainian number/plural rules](https://www.unicode.org/cldr/charts/44/supplemental/language_plural_rules.html)
and [unit grammar](https://www.unicode.org/cldr/charts/50/grammar/uk.html).
CLDR itself explains that rule-based number formatting needs grammatical
context beyond numeric values; this finite profile deliberately exposes its
nominative/label choices for review instead of making that contextual guess:
[UTS #35 number formatting](https://unicode.org/reports/tr35/tr35-numbers.html).


## Current editor integration and verification commands

The new action follows the existing Spelling, Appearance and Paced reading
actions. The earlier Normalize spacing action retains its existing behavior.
The native form exposes whole-copy or selected-passage scope, mode, locale,
number style, decimal/date formats, independent expansion toggles and four
Keep/Read/Skip choices. Inactive options cannot change a verbatim proposal.

Preparation runs through the existing frame executor and TranscriptionSession.
The worker sees only immutable text/options; it never reads a native control.
Ordinary Close cancels and waits for actual worker settlement. Forced native
destruction invalidates callbacks and cancels the bounded pure operation.
Apply/Undo use the current NarrationController and recovery store. If recovery
saving fails after Apply, the accepted in-memory revision remains available and
the warning says so. Existing backups retain the saved revision and Undo history.

Strict source suites (no optional spelling wheel, voice, model, network or
audio device is needed):

- tests/reader/test_speech_normalization.py
- tests/ui/test_speech_preparation.py
- tests/ui/test_speech_preparation_hooks.py

The first suite covers exact finite text examples, source-span rebinding,
ambiguity, structural loss refusal, deadlines/cancellation and forged oversized
previews. The UI suites use a real ThreadPoolExecutor, current controller/store,
forced-destruction and late-selection guards, actual recovery-save failure,
exact CRLF/UTF-16 narration hooks, and a complete backup/restore/Undo roundtrip.

Installed Windows: run
python -I -B scripts/windows_speech_preparation_smoke.py --native
--output ci-results/native-speech-preparation.json
on one command line. The probe drives actual wx buttons/focus and the existing
operation session with a real executor: exact verbatim selection, explicit
Ukrainian preview, Apply, recovery verification, Undo, whole-copy verbatim,
Save and Close. It checks unchanged original bytes and installed module origins.
It does not claim physical keyboard input, NVDA behavior or synthesized speech.
