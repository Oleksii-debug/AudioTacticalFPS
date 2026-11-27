# Research Search Center continuation

Research Search Center uses the existing catalog, source verifier and retrieval
engines. Select libraries explicitly, set the existing query and metadata
filters, then use Search, Next result page and Previous result page. All six
retrieval modes support the same navigation.

Each operation admits at most 20 libraries, 500 filtered editions per library and
100 result handles per library. Further selected libraries and source windows
remain reachable through the returned continuation. The default selected-library
request supports up to 10000 distinct IDs. These are operation and request
budgets, not claims of an unlimited workload.

The status names the current library range, source edition ranges and result
offsets. A window with no matches can still have a Next page. Ranking is local
to each displayed source window; results are grouped in selected-library order.
This does not claim a relevance ranking over the entire corpus. Existing
semantic model availability and candidate budgets retain their explicit
keyword fallback and reported limitations.

Continuations bind the exact selected libraries, query, filters, page sizes and
catalog/source generation. Every operation reads fresh authorized snapshots.
An updated catalog invalidates a pending continuation and asks for a new search.
Replacing a data directory, database or asset directory withdraws the session,
even if the replacement contains identical bytes. A cursor does not authorize a
quotation: copy, context and save still use the existing fresh source verifier.

A completed page replaces earlier action handles atomically. Failure or
cancellation before publication retains the exact preceding page and handles.
Previous rereads a prior source window and generates fresh handles. The UI keeps
at most 1000 page requests, without source passages or result handles; the
oldest Previous entry is retired when this history budget is reached. Changing
query/scope or closing the dialog clears the session.

The original bounded `LibraryApp.search` and `FederatedSearch.search` methods
remain compatible. In-process callers can use `LibraryApp.search_page` and
`FederatedSearch.search_page` with their returned immutable cursor objects.
The cursors are not portable credentials or persistent cross-process tokens.

Validation uses real synthetic libraries, source imports and quote verification,
including 501 editions, 23 selected libraries, all retrieval modes, exact scope
changes, cancellation, old-handle rejection and identical-byte root replacement.
The headless strict gates passed 482 cases across 14 groups before the additional
root-identity case; all four affected federation/UI groups then passed with that
case included. The installed native Windows probe exercises Next and Previous;
its result must be collected for the composed release. These local results are
not NVDA or acoustic/model quality acceptance.
