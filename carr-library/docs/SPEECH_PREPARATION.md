# Prepare a reading copy for speech

The Voice reader and the Studio spoken-line editor both offer **Prepare speech text...**. The action creates a bounded text proposal on the host's existing worker. It does not start speech, choose a voice, change the retained original, authorize a quotation or save a Studio project automatically.

1. Open the reading copy or an existing Studio spoken line.
2. Choose **Prepare speech text...**. The initial **Verbatim — exact source** mode preserves every character.
3. Select **Explicit speech rules**, then Ukrainian, English or Slovak. Choose the whole current copy or its selected passage and only the conversions you want.
4. Use **Preview** and compare **Exact selected source** with **Proposed reading text**.
5. Use **Apply reviewed text** to accept one undoable derivative action. **Undo last preparation** restores that action only while the exact current text, selection, language and voice still match. The reader's ordinary Undo/Redo history remains available.
6. In Studio, close the preparation window and use **Save spoken line** to create the existing canonical project revision. The original script, other lines, role/device settings and source references remain unchanged.

Previously edited or pronunciation-adjusted mapped runs stay exact as current reading text. Automatic preparation does not overwrite those spans. Use the existing **Pronunciation rules** action after preparation for explicit general, language or exact voice overrides. That action retains its current dictionary formats and precedence; preparation introduces no second dictionary store.

## Explicit finite profiles

| Choice | Behavior and limit |
| --- | --- |
| Numbers | Integer cardinal words up to nine digits, or explicit individual-digit reading up to eighteen digits. Leading zeroes remain literal unless digit mode is selected. Signs and at most six fractional digits are supported. |
| Decimal mark | The locale default is comma for Ukrainian/Slovak and dot for English. Dot or comma may instead be selected explicitly. A different ambiguous mark stays literal. |
| Dates | Only actual calendar dates matching the selected exact ISO YYYY-MM-DD or locale UK/SK DD.MM.YYYY / EN MM/DD/YYYY format. English and Ukrainian use the retained PR99 finite month/day forms. Slovak uses explicit day, month and year labels with cardinal values; it does not invent ordinal or contextual case agreement. |
| Units | A case-sensitive listed SI/unit table. Number expansion and unit expansion are separate choices. Slovak fractional quantities use an explicit unit label instead of guessing grammatical case. |
| Abbreviations | Only the listed finite English, Ukrainian and Slovak forms. Unlisted names, acronyms and abbreviations stay literal and can be reviewed through Pronunciation rules. |
| Arithmetic and symbols | Explicit numeric expressions with at most sixteen operators, and a finite symbol table. URLs, email addresses, scientific notation, inline code and mixed identifiers remain protected. |
| Paragraph capitalization | Explicit capitalization of plain paragraph starts only. It does not infer names, sentence boundaries after abbreviations or punctuation. |
| Soft-hyphen joining | Removes only an explicit soft hyphen followed by a line break between alphabetic fragments. An ordinary hyphen is preserved. |
| Wrapped-line joining | An explicit proposal to join adjacent plain nonblank lines when the preceding line ends with a letter and the next starts with a lowercase letter. Blank paragraphs and recognized structures remain separate. No comma, question mark or sentence-ending punctuation is inferred. |

English/Ukrainian deterministic profiles and their original contract cases are reused from PR99. Slovak integer cardinals reuse the unmodified num2words 0.5.14 Slovak runtime closure, pinned to commit 6abe2c64714a38e41de23fe20fa319d0ad2fd845. Complete source, per-file Git identities, copyright headers and LGPL-2.1-or-later COPYING are included in the package. This is a text conversion dependency; it is not a speech engine or evidence of acoustic quality.

## Read, keep or skip structures

The choices apply only to explicitly recognized Markdown ATX headings, consistent simple pipe tables, Markdown footnotes and closed fenced code blocks. **Keep exact source** is the default for each. **Read with explicit labels** produces a reviewable derivative. **Skip recognized structure** proposes removing only that recognized span while preserving its line separation.

A physical PDF's visual layout, arbitrary indentation or an ambiguous OCR paragraph is not treated as a verified heading, table, code block or footnote. Unsupported/lossy table forms and unclosed selected code fences refuse the complete proposal. No unsupported linguistic structure is silently deleted.

## Source maps, recovery and limits

One preview admits at most 200,000 Unicode characters, 50,000 recognized tokens and 5,000 changes, producing at most 1,000,000 characters. The selected worker checks cancellation and a finite 0.001–10 second parser deadline; the UI uses the existing two-second default. For a larger reading copy, choose a smaller passage. A refused, cancelled, stale or timed-out preview makes no history change.

Accepting a proposal stores all checked original ranges as one compound action. Unchanged interior runs keep their exact original positions; transformed runs are explicitly nonexact. The immutable original, original digest and passive source anchor stay unchanged. A source map is not current quotation authority: use the ordinary library source/copy APIs for that.

Existing unmodified and ordinary-edit files retain their v1/v2 representation. The new action writes carr.reading-copy.v3 only when its history contains a compound action. Replay checks ordered nonoverlapping ranges, each old slice, complete before/after digests, the same source anchor and existing history/size limits. One Undo/Redo restores the whole action. Existing ReaderDraftStore, live document snapshots and common4 backup/fresh restore reuse that same parser; restoration does not start preparation or playback.

A change in the current document, selected range, language, voice, source authorization or selected library invalidates a pending proposal. Cancellation and native window disposal withdraw delivery through the existing owner; no additional executor or player is created.

## Qualification boundary

The component's source, isolated installed, recovery and native keyboard checks must be recorded against their exact commit and installed file inventory. New preparation runtime results are **NOT_RUN** until those gates complete. Existing component evidence does not qualify this new composition.

Deterministic text fixtures establish exact conversions, source positions, history and lifetime behavior. They do not establish Ukrainian/Slovak/English pronunciation naturalness, physical output, NVDA behavior, punctuation interpretation, foreign-name stress or a voice/model's quality. Those require the explicitly selected actual runtime and the owner's listening/accessibility review.
