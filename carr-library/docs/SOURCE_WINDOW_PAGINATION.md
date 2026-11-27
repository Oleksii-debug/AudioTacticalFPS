# Bounded source windows and selected-library continuation

The current search host can continue through larger source selections and through explicitly selected research libraries. Existing exact quotation, source-context and evidence actions still use the original retained source and the current library authorization.

## Using the controls

In the main Search section, choose the retrieval method and source filters, then choose **Search**. **Next results** and **Previous results** move between bounded pages. Thematic and semantic pages state that relevance applies only to the inspected source window. A page with no matches may still have **Next results**: more source segments remain to be inspected.

In **Research Search Center**, explicitly check the libraries to search. The current library remains the initial selection; an empty selection never means all libraries. Each page visits at most 20 selected libraries, with at most 100 results from each visited library. **Next results** first finishes the current library's source windows, then continues through the selected library order. The current notebook and unsaved notes remain in place.

Only the latest Search Center page owns its actionable result handles. Choose a current result to copy its verified quotation or citation, read source context, or save evidence to a topic in that result's own library. Unverified extraction remains visibly unverified and cannot become an approved quotation through search.

Changing a query, filter or selected scope discards its continuation. Reopening the application or Search Center requires a new search. Continuations are opaque in-memory tokens, not saved access credentials. A changed source or catalog snapshot is rejected; start a fresh search to review the new data.

## Scope and limits

The original exact-search route is retained for its compatible scopes. The added route streams source metadata and validates all selected raw-source digests before pruning candidates. It then admits one bounded candidate window: at most 20,000 source segments and 5,000,000 characters, with a default of 500 segments. Ranked windows inspect at most the requested result limit, so qualifying parents are not silently discarded when the next window is chosen. Individual text values are checked for storage type, NUL and UTF-8 byte length before materialization.

A selected-library request may name up to 10,000 distinct libraries, but it never materializes all their results in one call. Each page keeps the existing total cooperative deadline of at most 60 seconds and the existing 2,000 actionable-result ceiling. Source verification is repeated on every page. This prioritizes exact snapshot integrity; it is not a claim that every large corpus will complete within that allowance.

Semantic mode uses only the existing admitted local model interface. When no local model is available, or its existing window budget is reached, the page explicitly reports keyword fallback. No global relevance ranking, new index, additional model download or provider call is introduced.

## Reuse and qualification

The catalog keyset and bounded candidate logic reuse PR99 head `18a61e7ec6601119f9a4ffa7228274e836856ca4`, including pagination donor `f11ca1e01ef95130474d8c1d9a4a125d6a0ae9f1`. The current host's opaque CursorStore, source attestation, exact-copy/evidence routes and authorized SQLite snapshot remain authoritative. The donor's serialized cursor format and expanded operation deadline were not adopted.

Prepared tests cover actual sealed catalogs with 601 editions; exact repeated Unicode matches; empty windows; catalog/source changes; current authorization; malformed raw values before full reads; opaque and foreign-session token rejection; 25-library continuation; failed-page atomicity; and real background UI callbacks. All prior literal-search tests remain. The former two ranked-not-supported cases now check actual bounded ranked continuation and retained-original copying.

`scripts/windows_source_pagination_smoke.py` adds actual wx keyboard checks for empty ranked windows, Next/Previous, an explicit 25-library checklist, current-page verified clipboard copying and unchanged original bytes. It checks exact source or installed wheel origins. Existing exact-pagination and Search Center native gates remain separate and unchanged.

At this source checkpoint, the new implementation, tests and native probe are **NOT_RUN**. They make no NVDA, real semantic-model quality, corpus-scale benchmark or external-provider qualification claim.

Source command from the application project directory:

```text
python -I -B scripts/windows_source_pagination_smoke.py --source . --report ci-results/source-windows-source.json
```

Installed command:

```text
python -I -B scripts/windows_source_pagination_smoke.py --expect-installed --report ci-results/source-windows-installed.json
```
