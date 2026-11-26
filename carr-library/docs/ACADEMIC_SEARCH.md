# Academic search

This slice adds a Research Search Center for explicitly selected local libraries,
a manager for saved topic-search criteria, and JSON search commands for headless
use. It composes the existing `LibraryApp`, retrieval engines and source
verification services. It introduces no database migration, persistent search
index, network operation or Zotero dependency.

## Desktop workflow

Open **Research → Research Search Center...** with **Ctrl+Shift+F**. Only the
current library is checked initially; without an available current library, the
selection starts empty. Check the libraries to include, enter a query, and choose
**Search** or press Enter in the query field. Space toggles the focused library.
Libraries with identical names are distinguished by their stable IDs.

Choose an existing retrieval method: exact phrase, word, all terms on one page,
thematic keyword search, or the local semantic model. **Short quotations** and
**Thematic excerpts** control presentation. The Author contains and Title contains
filters retain the host's case-sensitive literal substring semantics; Language
code and Edition use exact matches. A missing semantic model produces an explicit
keyword-fallback notice.

Results retain their source library and verification status. **View source
context** opens freshly loaded context, or clearly labelled extraction preview
for an unverified result. **Copy quotation**, **Copy citation**, and **Save
quotation to a topic** require verified source text. Saving offers freshly loaded
topics from the result's own library and an optional separate research note.
Opening the Search Center preserves the desktop's current library, topic,
notebook and unsaved note.

**Cancel operation** requests cooperative cancellation. Changing search options
invalidates displayed result handles. Closing during an operation requests
cancellation and waits for the worker to finish before destroying the dialog.

## Saved topic searches

Select a research topic, then open **Research → Manage saved topic searches…**.
Create criteria for either **Saved quotations** or **Research notes**, with a
name, query and result limit. The manager provides Run, Edit, Duplicate, Archive
and Restore actions, and paged access to active and archived definitions.

Each Run evaluates the stored criteria against the topic's current, freshly
verified saved evidence. Definitions persist; result sets are generated afresh.
Selected-library source-search sessions remain temporary and separate from these
topic definitions.

Archive preserves the definition for restoration. Restore reactivates that same
identity; Duplicate creates a new active definition. Each operation reauthorizes
the library and visible topic. Update, Duplicate, Archive, Restore and Run compare
the complete definition the user reviewed with the current stored definition.
Writes perform that comparison inside the write transaction. A concurrent change
requires a refresh and review instead of overwriting another edit. The desktop
also rechecks the definition before displaying a completed Run.

## Headless CLI

With the package installed, list active library IDs and search an explicit
selection without opening the desktop:

```sh
python -m carr_library --data-dir "./research-data" --list-libraries
python -m carr_library --data-dir "./research-data" --search "railway history" --workspace LIBRARY_ID_A --workspace LIBRARY_ID_B --limit-per-library 20
```

Replace the example IDs with values from `--list-libraries`. Repeat `--workspace`
for each selected library; `--search` requires at least one. The installed
`research-library` command accepts the same options.

| Option | Values or behavior |
| --- | --- |
| `--search-mode` | `phrase` (default), `word`, `terms`, `thematic`, `semantic` |
| `--result-mode` | `short` (default), `thematic` |
| `--title`, `--author` | Literal catalog substring filters |
| `--language`, `--edition` | Exact catalog filters |
| `--limit-per-library` | 1–100; CLI default 20 |
| `--model-dir`, `--model-manifest` | Supply both for the existing verified local model runtime |

Search emits JSON with query, mode, ordering explanation, notices and grouped
library results. Each result includes its existing `verified` flag and source
identities. Extraction previews remain labelled unverified. Result references
belong to the in-memory search session; JSON output does not create reusable
copy/save handles for another process. The CLI currently uses the service's
default 60-second cooperative search budget.

## Service contract

[`FederatedSearch`](../src/carr_library/search/federated.py) accepts an already
initialized, trusted `LibraryApp`:

```python
from carr_library.search.federated import FederatedSearch

search = FederatedSearch(app)
session = search.search(
    (library_a, library_b), "railway history",
    mode="phrase", result_mode="short", author="Author",
    limit_per_library=50, timeout=60.0, cancelled=cancel_event.is_set,
)
```

`session.groups` contains immutable `LibraryResults(workspace, library_name,
hits)` values. Each `FederatedHit` holds its session ID, library name and the
existing immutable `SearchResult` as `hit.result`. `session.hits` provides a
flattened view. Groups preserve deduplicated selection order; ordering within
each group comes from the existing engine. There is **no global relevance
score or ranking across libraries**.

The service also accepts `title`, `language`, `edition`, an optional `collections`
mapping from selected library IDs to their own active collection IDs, and an
`on_status` callback receiving a library-labelled string. The current desktop
and CLI expose the metadata filters; collection mappings are a service API.

`context(hit)`, `preview(hit)`, `copy(hit, field="both")`, `topics(hit)` and
`save_hit(hit, topic, note="")` accept exact handles from the latest completed
session and support `cancelled=`. `topics()` returns existing `Topic` objects.
`clear()` invalidates the session and pending publication.

[`QueryManager`](../src/carr_library/research/query_manager.py) binds to
`QueryManager(app, workspace, topic)`. It exposes `list`, `get`, `create`,
`update`, `duplicate`, `archive`, `restore` and `run`. Update, Duplicate, Archive,
Restore and Run receive the complete previously read `SavedQuery` as `expected`;
callers should refresh after a concurrent-change error.

## Limits and source safeguards

| Boundary | Limit or rule |
| --- | --- |
| Selected libraries | Explicit nonempty list/tuple; at most 20 distinct IDs |
| Source-search query | Nonblank text, at most 500 characters; prohibited control characters and surrogates rejected |
| Results | 1–100 per library; service/UI default 50; UI choices 10, 25, 50, 100 |
| Catalog scope | Existing host limit of 500 editions per library after filters |
| Search deadline | One total cooperative budget, greater than 0 and at most 60 seconds |
| Saved topic queries | Query up to 500 characters; 1–100 results; manager pages up to 100 definitions |

Each library's name lookup and source search uses fresh authorized SQLite
snapshots. The combined result is **not a simultaneous snapshot of all
libraries**. Native blocking calls remain cooperative rather than forcibly
preempted.

A candidate session uses an isolated shallow host copy with its own selection
cache. Only complete success replaces `search.current`. Failure or cancellation
preserves the previous completed service session and never publishes partial
handles or evicts the desktop's selections. The desktop hides rows after an
unsuccessful attempt. Searches fail promptly when another action owns session
publication rather than waiting outside their budget.

Actions derive their workspace from the exact session-owned result. They freshly
check scope, collection membership and original source integrity through the
existing host. Saving also authorizes the destination topic in that same
library. Stale or reconstructed handles, changed data-directory bindings,
archived scope and removed membership are rejected.

Verified quotations are exact source slices with their existing revision and
citation anchors. Presentation and semantic retrieval do not synthesize quoted
text or approve extraction. Unverified PDF/DOCX/EPUB text remains preview-only for
these actions; the existing source attestors determine verification, and this
slice adds no approval workflow. Missing or changed retained originals are
integrity errors, never a reason to silently downgrade a result to preview.

## Design references and native acceptance

[Zotero 10, announced 17 August 2026](https://www.zotero.org/blog/zotero-10/),
describes selection across libraries and results grouped by library. That
selection/grouping pattern informed this feature. Zotero's
[Searching guide, Saved Searches section](https://www.zotero.org/support/searching#saved_searches)
describes storing criteria that continue to evaluate current content. These are
product-design references, not dependencies or claims of feature parity. The
Zotero 10 announcement is the reference for current multi-library behavior.

The additive [native smoke helper](../tests/ui/federated_native_smoke.py) runs
inside an existing wx desktop event loop and exercises synthetic search,
context, clipboard, topic saving and cancellation. Windows execution evidence
belongs to the corresponding source commit's CI run; the helper's existence
does not establish acceptance. No NVDA acceptance is claimed. Validation evidence
and execution counts are recorded separately from this product/developer contract.

The [focused Windows/NVDA protocol](ACADEMIC_SEARCH_NVDA.md) records actual spoken
names, state, focus restoration and complete keyboard workflows separately.
