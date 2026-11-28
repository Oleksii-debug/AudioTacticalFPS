# Catalog identity projection for paged search

## Scope and evidence

This change reduces repeated catalog work within the existing paged-search path. The retained-corpus measurement at source commit `79ef756d08667919d9a7c4367305aca5dfdaf6c9` completed all five phases for 50,000 books and 1,000,000 segments. Its 50,000-book, 2,000,000-segment profile reached the unchanged 3,600-second measure limit during full-result traversal: 1,276,800 results, 38,304 completed requests, and 12,768 windows were verified. Exact input/archive identities and all phase records are retained in evidence blob `57c82d60133c51fbd62deb86c8c96153057c4da3`.

Those measurements precede this change. No speedup or completed two-million-segment result is claimed by this source checkpoint. The new SQLite VM-work regressions measure the specific query cost using real SQLite; they do not establish ordinary-import throughput, retained-corpus latency, or end-to-end capacity.

## Implementation

A paged source window needs the selected edition ID and work ID. It previously obtained those two fields by constructing complete public EditionView values, including metadata columns and the complete author list. The private identity query now reuses the same validated selection CTE and returns only the two needed columns. Public browse_editions and get_edition continue to construct the same complete views.

The selection keeps current read-transaction and foreign-key checks, workspace and active-collection validation, literal title/author filtering, exact language and edition filters, membership EXISTS, catalog-work validation, fixed keyset/exact-ID predicates, BINARY ordering, and existing limits. Author filtering still executes the author predicate when requested; the removed work is only the unused author-list aggregation for a private identity result.

The one-row next-catalog query is deferred until the current source window has finished its selected page range. An unfinished range continues within its already verified edition boundary, so it does not need that query. A required continuation query runs in the same current transaction and is followed by the same cancellation/deadline check before a result can escape.

## Preserved authority and budgets

The source-window wire, digest, raw rows, character accounting and continuation tokens are unchanged. Both independent application search reads and the full hidden-result lookahead still execute. Literal candidate row equality, retained-source authentication, text attestation, selection publication and fresh-window identity refusal remain in their existing positions. There is no cross-call cache or new persisted authority.

The benchmark retains result page size 37, source windows of at most 100 pages, the same four profiles, the same 3,600-second phase limits, and the same explicit complete-backup allowance. Neither corpus work nor verification is moved outside its admitted budgets.

## Regression inventory

The new whole module `tests/search/test_window_catalog_projection.py` contains 27 statically enumerated cases across seven functions. It uses actual Catalog, Storage and SQLite connections. Coverage includes an independent Unicode/BINARY/literal-filter oracle; a 64-author VM-work comparison; exact former source-window wire across unfinished, exact-end and 500-edition boundaries; cancellation during a query and after a deferred lookup; fresh-snapshot membership, archive and active-revision mutations; and unchanged authorization/input refusals.

All 31 existing search test modules were inspected for a dependency on the removed paging-module import. None refers to or patches `paging.browse_editions`. Existing public catalog-view tests and the old query-work suite continue to call the unchanged public read_views API.

The dedicated capacity contracts retain all previous 189 cases and add the whole 27-case module, requiring 216 source and 216 isolated-installed passes. The original focused source preflight also retains every old selector and adds the same whole module. Exact source/test pins, current wheel proof, full installed payload verification, current module origins, zero-skip/xfail checks, child cleanup and existing finite envelopes remain required.

At this checkpoint source and fixture peer reviews are accepted as STATIC_NOT_RUN. Python parsing, actual regression execution, native backup execution and the next retained-corpus measurements must be recorded independently against the eventual tested commit.
