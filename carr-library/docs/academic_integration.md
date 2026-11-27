# Citation documents on the canonical CSL engine

The Academic library **Citation styles** command and **Create citation document**
use the same installed, offline `carr_library.citations.csl` processor. The
runtime is the pinned QuickJS binding; citeproc-js, styles and locales come from
the canonical citation package. No Node executable, second processor bundle,
network style lookup, or alternate DOI client is required.

## Bibliography and source-bound documents

Use Citation styles for reviewed bibliography previews and text, HTML or Word
export. Use Create citation document when the document needs an ordered sequence
of citations, explicitly reviewed pinpoint locators, editable Word runs and
embedded metadata for later regeneration. Both routes retain the existing
Academic library records and original source files.

A publication's page range is bibliographic metadata. It never becomes a
verified pinpoint automatically. The document API accepts a locator only through
a typed `CitationRequest` with an explicit caller review flag. The embedded
flag records that caller assertion; the formatter does not authenticate the
quotation, page, or source. Catalog changes withdraw the cached preview and
require the source locators to be reviewed again.

```python
from carr_library.academic_integration import (
    CitationCluster, CitationParagraph, CitationRequest,
    export_citation_docx, render_citations,
)

result = render_citations(
    references,
    style="apa",
    citations=[CitationCluster(
        (CitationRequest("source-a", "12", locator_verified=True),),
        id="reviewed-claim",
    )],
)
export_citation_docx(
    result, "new-citations.docx",
    paragraphs=[CitationParagraph("Researcher's own statement.", ("reviewed-claim",))],
)
```

The controller binds the processing copy to one library instance and workspace,
current academic reference metadata and linked catalog source identity. Private
notes and abstracts are excluded from the processing copy. A completed result
from another controller or a stale reference snapshot cannot be exported.

## One bounded processor

The compatibility API maps its stable cluster IDs and explicit note indices into
the canonical CSL bridge. Bibliographic sorting, numbering, disambiguation,
repeated notes and typography are produced by that bridge. Word layout receives
the actual bibliography parameters from the same processor result.

The installed processor has fixed memory and CPU budgets. Cancellation is checked
before and after the bounded QuickJS evaluation and before document publication.
The native evaluation itself has no Python cancellation callback; a request made
during evaluation is delivered when that bounded call returns. There is no
claim that the Python thread can interrupt a native evaluation instantly.

A supplied local style must pass the canonical custom-style validation and is
bound by its complete SHA-256. The legacy keyword `node_path` is retained only
to report an explicit incompatible runtime selection; it never launches Node.
The old command name `chicago-notes` selects today's canonical
`chicago-note` style for a new document, whose returned identity records the
current style and edition.

## Saved documents and historical identity

`read_citation_docx` reads the bounded embedded CARR snapshot without executing
document content or following external relationships. Its self-hash detects
corruption, not authenticity. Visible edits made outside the application do not
replace the embedded snapshot.

`regenerate_citation_docx` writes a new destination and retains paragraph order,
stable citation IDs, the supplied note numbering, metadata and reviewed locator
assertions. A changed reference with a retained pinpoint needs an explicit newly
reviewed citation request. Source-document bytes and identity are checked again
before publication.

Regeneration requires the exact saved processor and style identity. Historical
Node-produced snapshots remain readable, but their processor SHA-256 differs
from the canonical QuickJS bundle even when the reported citeproc version is the
same. They are refused for automatic regeneration. Review the recovered data
and explicitly create a new document with the current engine; the application
does not pretend that a different processor or Chicago edition reproduces the
old document. A custom-style snapshot additionally requires its explicitly
selected original file with matching bytes.

## DOI acquisition and export publication

DOI lookup and document acquisition use the existing canonical
`carr_library.acquisition` service and native acquisition dialog. Online lookup
requires explicit consent. Provider metadata and license claims do not grant
quotation verification or override original source data. The former independent
metadata worker is not installed.

Exports retain one destination-approval contract. Before confirmation the writer
captures the path, parent identity, file metadata, Windows ChangeTime when
available and a bounded full-byte digest. It checks the route again before
reading, remains cancellable during hashing and verifies the approved bytes
again after rendering. Initially absent files use atomic no-clobber publication
even if a caller omits an explicit create-only option. Existing files larger than
32 MiB or exceeding the quarter-second inspection budget require a new filename.

A completed publication returns an `ExportReceipt`. A remaining private staging
cleanup problem is reported as a cleanup warning on the saved result, rather
than turning successfully published bytes into a failed export. No native or
manual accessibility acceptance is inferred from source tests.
