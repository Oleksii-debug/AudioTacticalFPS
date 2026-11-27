# Exact search result pages

The current LibraryApp owns the continuation through search_page. Existing
search() callers keep their tuple contract. Exact phrase, whole word, all terms
and advanced Boolean/NEAR search can continue past the first hundred matches.
The desktop uses the current Search panel and worker, with named Previous
results and Next results controls and current-page status.

A cursor is an opaque token in a fixed per-application table. It binds workspace,
topic authority, literal query/mode, filters, presentation and page size. It is
not persisted authority and expires after the application closes or after the
bounded token table evicts it. Each request opens a new authorized transaction
and checks every scoped source digest before pruning candidates. The cursor
also binds the complete active source inventory and current catalog metadata;
changed inputs require a fresh search. No SQLite snapshot stays open between
user actions.

The result limit remains at most 100 per page. Continuation uses the stable
edition/segment/page/raw-offset order and has no legacy 10,000-offset ceiling.
Expanded source excerpts are deduplicated before the page boundary by replaying
only their existing source-page coordinates. Source quotations, retained
original checks and independent attestation all remain in LibraryApp. Failed or
cancelled materialization publishes neither partial selections nor a cursor.
The desktop retains the previous completed page if a later page fails.

Previous results retains up to 64 recent page positions; Search returns to the
first page. The application keeps at most 128 continuation records and the
existing 2,000 live selection allowance. This bounds navigation state without
setting a total forward-result ceiling.

This is the continuation part of V3-07, not completed large-library qualification.
The existing 500-edition/20,000-page/5-million-character scope bounds and 60-second
operation deadline remain. Native ranker/semantic pagination, durable index
construction and continuation across restarts are separate pending work. No
1k/10k/50k-book throughput, multi-GB source or memory-capacity result is claimed.

New controls cover real SQLite keyset paging, Unicode/raw spans, integrity of
nonmatching sources, query/catalog/source changes, scope isolation, presentation
deduplication, cursor eviction, cancelled attestation and exact source copying.
The native Windows script uses actual desktop controls and the current worker
for 205 results, next/previous, last-page state and changed-source rejection,
with exact checkout/installed-wheel module proof. Source review is complete;
execution remains NOT_RUN until the exact new candidate's gates finish.
