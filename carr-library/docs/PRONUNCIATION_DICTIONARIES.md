# Pronunciation dictionary import

Pronunciation rules change a reading copy. They do not change a retained book,
source text, source hash, quotations, or voice installation.

## Review, then choose what to keep

Open **Pronunciation rules** in the voice reader or the Studio pronunciation
review. Choose **Import**, then select a supported JSON, DIC, REX, or BXD file.

The default encoding is UTF-8, with or without its BOM, or UTF-16 with a BOM.
For an older file, explicitly choose **Windows-1251** or **Windows-1252**.
The importer does not infer an ANSI codepage from the computer's current locale.
A conflicting BOM, undecodable bytes, NUL, or UTF-32 produces an actionable
error. The source file remains unchanged.

Import replaces only the dialog's unsaved review list. It does not change the
library dictionary or the reading copy. Check the displayed order and scope,
choose **Preview**, inspect the exact before/after text, and choose **Apply**
only if that result is wanted. Applying remains one ordinary reading edit with
the existing Undo operation.

**Save** explicitly retains the displayed rules in the current library's
existing `carr.pronunciations.v1` dictionary. **Export** produces that canonical
JSON format and keeps the captured-destination and no-clobber checks. The
application does not rewrite a foreign dictionary in place or preserve its
comments, row IDs, disabled rows, or display metadata in the JSON export.
Canonical JSON retains its existing caller-selected filename; only the
`.dic`, `.rex`, and `.bxd` suffixes select foreign parsing.

The ordinary complete library backup already preserves this canonical
dictionary. Restore does not apply it to a reading copy or run a regular
expression. Foreign input files outside the library are not silently added to a
backup.

## Supported text dialects

This is a finite conversion into the existing rule model. Unsupported
constructs are rejected as a whole with the format and line number. The
previous saved dictionary and unsaved review list survive a failed import.

### DIC: literal text templates

This is the pronunciation DIC syntax described in the publisher's help, not
a Hunspell `.dic` word list. Each active line has a pattern, an unescaped
`=` separator, and literal replacement text.

- Blank lines and lines beginning with a single `#` are comments.
- A leading single `$` makes a pattern case-sensitive. Other rules ignore case.
- A leading `*` permits matching a suffix inside a word; a trailing `*`
  permits matching a prefix. Both together permit a substring.
- Doubled `**`, `$$`, `==`, and `##` in the pattern represent the
  corresponding literal character. A `##` pattern is not a comment.
- Replacement text is literal: `**|$|==|##` remains exactly that text.
- Case-sensitive rules run first. Within each case group, longer decoded
  literal patterns run first; equal lengths retain file order. For example,
  `scat=Y` precedes `*cat=X` even if the latter line occurs first.

A whole-word `cat=X` changes standalone `cat`, `Cat`, and `CAT`, but not
`scat`, `cats`, or `cat1`. An underscore acts as a separator, so
`cat_cat` becomes `X_X`. The authored corpus also checks accented Latin,
Greek, CJK, decimal digits and emoji. Word boundaries use Unicode
alphanumeric characters excluding underscore. The exact observed replacements
are retained in the test fixture linked below.

### REX: a bounded text regular-expression subset

Rules run in physical file order; the result of one rule is the input to the
next. A leading `@` makes a pattern case-sensitive. Otherwise matching ignores
case. A backslash escapes a pattern's `=` separator.

The supported subset includes ordinary captured groups, alternatives,
character classes, anchors and quantifiers accepted by the existing reviewed
substitution engine. Replacement `$0` denotes the complete match,
`$1` through `$99` refer to existing captured groups, and `$$` is a literal
dollar. Hexadecimal pattern escapes `\xNN` and exactly four-digit
`\x{NNNN}` are accepted for complete characters.

Numeric pattern escapes, inline flags, special groups, lookarounds, unknown
escapes, nonexistent replacement groups and replacement case operators such
as `\U$0` require explicit manual review and are refused by import.
Preview still executes only through the existing bounded isolated substitution
worker. It does not load a foreign regex engine.

### BXD: Version 1 text rules

The admitted layout begins with `Version=1`, then `Delimiter=` followed by
one ETX character (U+0003), then exactly three description fields separated by
ETX. Each subsequent nonempty rule row has ten ETX-separated fields:

| Position | Field | Accepted interpretation |
| --- | --- | --- |
| 1 | Rule identity | Distinct 32-character ASCII alphanumeric value. |
| 2 | Ordinal | Distinct positive decimal integer, at most nine digits. It does not reorder rows. |
| 3 | Enabled | `0` omits the row from imported active rules; `1` includes it. |
| 4 | Case | `0` ignores case; `1` is case-sensitive. |
| 5 | Pattern type | `0` is literal text; `1` uses the REX subset above. |
| 6 | Literal boundary mode | `0` whole word; `1` prefix; `2` suffix; `3` substring. Regex mode ignores this selector. |
| 7 | Output mode | Only `0`, plain replacement text. |
| 8 | Pattern | Nonempty, within the existing pattern limit. |
| 9 | Replacement | Literal for text rules; captured-group syntax for regex rules. |
| 10 | Note | Display metadata; not imported into a rule. |

Active rows run in physical file order, regardless of ordinal. Literal
boundaries use the same Unicode alphanumeric rule as DIC. All four boundary
values were exercised for both literal and regex modes by the publisher's
actual text utility.

BXD output modes `1` and `2` produce SAPI/IPA phoneme markup in that utility.
They are explicitly refused here: this reading-copy rule model does not carry
phoneme authority. Other output modes or unknown flags are also refused,
including values the external utility happens to coerce to defaults. The
message asks for reviewed plain-text replacements. Disabled rows are counted
and reported; saving does not silently re-enable or preserve them as active
rules.

General foreign rules have empty language and voice scopes. A user may
explicitly edit those scopes in the existing review form. Import does not
select a voice, infer pronunciation quality, or grant quotation authority.

## Limits and file ownership

All dictionary input is captured from one regular file with a single hard
link before parsing. The importer checks the selected file and parent before
reading, checks the opened descriptor before consuming bytes, enforces the
captured length plus one byte, checks cancellation between chunks, and checks
the same identities again afterward. A substituted FIFO, symbolic link,
hard link, reparse point, file growth or replacement is rejected under the
existing platform-specific safe opener.

Foreign text conversion accepts at most:

- 8,000,000 bytes in the selected file.
- 4,096 lines, including comments and blank lines.
- 16,384 characters in one line.
- 1,000 rule rows; disabled BXD rows count toward this limit.
- The current rule model's 1,024-character pattern and 4,096-character
  replacement limits, including any converted representation.
- 32 nested regular-expression groups and 99 captured groups in the imported
  subset.

These limits are independent of the existing preview worker's execution,
match-count and output-size limits. A parse failure or cancelled preview does
not publish partial rules or edit the reading document.

## Evidence and qualification

The grammar uses the publisher's documentation and actual authored text-only
observations. No third-party dictionary contents, help archive or executable
is distributed with this feature.

Primary documentation:

- [Balabolka, publisher download and documentation](https://www.cross-plus-a.com/balabolka.htm).
- [Publisher text-extraction utility and its dictionary option](https://www.cross-plus-a.com/btext.htm).
- [Govorilka author's description and published DIC dictionaries](https://www.vector-ski.ru/vecs/govorilka/dic.htm).

The [bounded help research](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46012342496)
captured the official portable ZIP (27,759,645 bytes,
SHA-256 `dcbf31a2c4201789aa0ff090afa29efa791fb007207f3353087c22ccaba507bb`)
and its English help (116,103 bytes,
SHA-256 `3fec29670aa098f2b4368c6bca5e57fc5b0b8758630196ea2d96d15b056f1de5`).
Only bounded grammar excerpts were printed.

The [actual author-tool run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46027229611)
completed all 49 authored cases, including the unchanged baseline, using:

| Captured component | Exact identity |
| --- | --- |
| Official archive | `https://www.cross-plus-a.com/blb2txt.zip`; 6,145,731 bytes |
| Archive SHA-256 | `1e4954fb8a934a53d8228219fd457336e18fed1fb463b4f04c12e71346359e04` |
| Selected member | `blb2txt.exe`, observed PE machine `0x14c`; 6,798,336 bytes |
| Executable SHA-256 | `e9985d73f18d6d6a3c3a4a47dc833aa5149e274e6cab3437db0c2490dceac0e1` |
| Research commit | `822438759db212c1d32773a333f5a30b0734de43` |

The [authored observation fixture](../tests/speech/fixtures/pronunciation_author_observations.json)
preserves every input, dictionary, output, exit status, and diagnostic field.
The first 42 cases exactly match the earlier
[author run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46020971597).
Tests compare the actual existing preview worker against all admitted
observations and require explicit errors for the unsupported cases. They
remove only the single final CRLF that the utility adds to every output file,
as established by the unchanged baseline.

The source suites also exercise saved dictionary bytes, reading Undo/history,
complete backup/restore, malformed input, and descriptor substitution races.
The native pronunciation probe preserves its keyboard flow and adds explicit
import/preview/apply/undo for all three foreign dialects, with installed source
identity checked before and after. Its picker/confirmation answers and
synthetic voice inventory are reported separately from real keyboard events.

The earlier draft completed 109 source and 109 isolated installed tests in
[run 46019038992](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46019038992).
That run predates the author-observation corrections and is not a pass for
these final bytes. The final source/installed review branch is
`carr/dictionary-import-review-20261127`; its exact result must be recorded
after completion. The extended Windows native probe remains NOT_RUN until its
corresponding gate completes.

A successful format test does not establish natural pronunciation, stress,
phoneme quality, voice quality, NVDA behavior, or equivalence with every
regular-expression and Unicode extension of another application. Preview is
the explicit place to review the converted text. No voice synthesis is
performed by the pronunciation probe.
