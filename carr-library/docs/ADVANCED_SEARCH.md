# Advanced academic source search

Choose **Advanced (AND, OR, NOT, NEAR)** in Search mode, or press
**Ctrl+Alt+Q** to open the query builder. The builder also has a button beside
the query field and an entry in the Research menu.

## Build a query from labelled fields

Enter one literal word or phrase on each line:

- **All of these**: every line must match.
- **Any of these**: at least one line must match.
- **Exclude**: none of the lines may match.
- **Nearby first phrase / Nearby second phrase**: both must occur close together.
  Choose the maximum number of intervening words, from 0 to 100.

All/Any/Nearby groups that are filled in are combined with AND. Exclusions apply
to that complete condition. A nearby pair may appear in either source order;
the two occurrences must be distinct and nonoverlapping.

Choose **Build query** to hear or read the generated expression. **Use query**
puts that expression in Search and selects Advanced mode. Press Enter in the
query field to run it. Editing a builder field invalidates the old preview.
Cancel keeps the original query and results. A pre-existing expression is shown
for reference; the builder does not claim to reverse-parse it into form fields.

Each field is literal input. A line containing `AND`, `OR`, quotation marks or a
backslash is safely quoted as source text. The Help button explains the syntax
in a named, read-only text control. Errors move focus to readable status.

## Type an expression directly

Operators must be uppercase. Lowercase `and`, `or` and `not` are literal words.

Direct expressions accept actual tabs and line breaks as whitespace. Inside a
quoted phrase they match equivalent source whitespace; outside quotes they
separate tokens. Other control characters and Unicode surrogates are rejected.
Builder fields use separate lines for separate phrases and reject tabs.
Backslash sequences are literal text, not escape sequences.

| Expression | Meaning |
| --- | --- |
| `memory AND identity` | Both words on the same page or text segment |
| `memory identity` | The same condition; adjacency implies AND |
| `"historical memory" OR "collective memory"` | Either exact phrase |
| `(memory OR identity) AND archive` | Group alternatives before requiring archive |
| `memory AND NOT fiction` | Memory is present and fiction is absent on that page |
| `"історична пам’ять" AND (Україна OR Словаччина)` | Ukrainian phrases and alternatives |
| `memory NEAR/5 identity` | At most five intervening words, in either order |
| `"oral history" NEAR/0 archive` | Distinct, nonoverlapping phrases with no word between |
| `"a ""quoted"" phrase"` | A phrase containing literal quotation marks |

Precedence is NEAR, NOT, AND, OR. Parentheses make grouping explicit. NEAR takes
two words or quoted phrases; combine separate NEAR clauses with AND rather
than chaining NEAR operators or applying it to a Boolean group.

Every possible successful OR branch must contain positive source evidence.
For example, `memory AND NOT fiction` is valid; `NOT fiction` and
`memory OR NOT fiction` are rejected. This prevents a source quotation from
being labelled a match solely because a term is absent. Double negation and
negated groups follow ordinary Boolean logic subject to that same requirement.

## Source precision and scope

Conditions apply to one physical PDF page or one logical text segment. A word
on one page cannot satisfy a condition for another page. Author, title,
language, edition and collection filters still apply within the active library.

Advanced search uses the existing literal matching rules: Unicode whole-word
boundaries, case folding, canonical character equivalence and equivalent
whitespace. Accents remain significant. Thus `STRASSE` can match `Straße`, and
composed/decomposed accents can match, while plain `cafe` is distinct from
`café`. Wildcards, regular expressions and stemming are not part of this mode.

There is one deterministic result per matching page or segment. A short result
may display only one of several distant positive matches. **More** opens the
original page context. Displayed and copied passages are contiguous, unchanged
source slices; non-adjacent passages are never joined into a quotation.
Independent source-review requirements remain in force.

Other search modes retain their original semantics. Typing `war OR peace` in
Exact phrase searches for those literal words; choose Advanced for the OR
operator. Thematic and Semantic remain separate modes for discovery.

## Limits and cancellation

Queries are limited to 500 characters, 32 term occurrences and 16 nesting
levels. NEAR distances are 0–100 words. The existing search scope limits and
shared operation timeout apply; highly repetitive proximity scans also have an
explicit occurrence budget. A budget failure or cancellation returns an error,
not an unmarked partial result set. The desktop displays up to 100 matches and
does not present that limit as a complete count of all possible matches.

## Research basis and implementation decisions

The official [Zotero 10 description](https://www.zotero.org/blog/zotero-10/)
documents nested search conditions. The official
[JabRef search manual](https://docs.jabref.org/finding-sorting-and-cleaning-entries/search)
documents Boolean groups and quoted phrases. These are useful scholarly
workflows to support alongside our existing source-integrity model.

This implementation reuses the application's own scoped `active_page`
transaction, Unicode/raw-offset mapping, source digests, live collection
authorization and cancellation budget. It adds no external service, dependency,
database schema, secondary persistent index or generated quotations. SQLite
FTS5 only prunes possible pages; the bounded Boolean matcher verifies actual
source occurrences before a result is published.

Future research views, bibliographic relations, reading plans and evidence
matrices are separate work. This document does not label those capabilities as
implemented by the advanced source-search module.


Research Search Center also supports these queries across explicitly selected
libraries. Use its Build advanced query button and the Previous/Next results
page controls. See [ACADEMIC_SEARCH.md](ACADEMIC_SEARCH.md) for live-scope paging,
CLI options and operation budgets. Source words remain literal in other modes.
