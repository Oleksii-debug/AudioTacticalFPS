# DOI/ISBN metadata and open-access acquisition

This is the V3-08 metadata workflow in the existing Universal Research Library desktop.
Open **Academic > DOI/ISBN lookup and open-access sources**. Opening this dialog makes
no network request. Bibliographic metadata, downloaded documents and approved
quotations remain separate.

## Use the desktop

1. Select the destination library. A selected local book is optional.
2. Choose DOI or ISBN. Enter a DOI, `doi:` identifier, a `https://doi.org/…`
   resolver URL, or a checksum-valid ISBN-10/ISBN-13 in the selected mode.
3. For DOI, select Crossref, Europe PMC, Unpaywall and/or OpenAlex. Crossref is
   the default. Unpaywall requires your own contact email, held only for this
   dialog/request. ISBN mode uses Open Library and disables DOI provider options.
4. Explicitly allow this online lookup and choose the lookup button. Only the identifier
   and the optional provider contact email are sent. Source text, notes, saved
   quotations, unrelated catalog entries and credentials are never uploaded.
5. Review all supplied metadata and provider response hashes. Missing data stays
   missing. Different provider titles and version information are shown for review.
6. **Save bibliographic reference** saves an independent academic reference.
   Exact DOI and equivalent ISBN-10/ISBN-13 deduplication preserve an existing
   reference and its private notes. Multiple existing matches require review.
   **Apply metadata to selected book** requires a separate confirmation and an
   unchanged current catalog record. Missing authors/language/volume preserve
   current values; the current edition label is preserved. Saved evidence keeps
   its historical metadata and original text.
7. On **Download source**, review provider, URL, license and manuscript version.
   Select a provider-reported open-access PDF, confirm your right to obtain it,
   and choose a **new local folder**. The download saves `document.pdf` and
   `provenance.json`, then enters the same isolated PDF import used by Import
   Center. A downloadable file never automatically approves quotation text.

All controls are native named text fields, checkboxes, choices, buttons and tabs.
No mouse, waveform, web login or embedded browser is required. Status and error
text remain focusable. Cancel stops work; closing an active dialog requests
cancellation and waits for the worker to return. Screen-reader acceptance must
be recorded on a real Windows/NVDA installation separately from automated tests.

## What is available

| Provider | Implemented query | Interpretation |
| --- | --- | --- |
| Crossref | Exact DOI `/works/{doi}` | Deposited bibliographic metadata; its links alone are not proof of open access. |
| Unpaywall | Exact DOI `/v2/{doi}` with user contact email | Reported OA locations, version and license; only a reported OA PDF can be downloaded. |
| Europe PMC | Exact DOI index search, core JSON response | Exact matching record and reported full-text locations; partial/ambiguous responses are rejected. |
| OpenAlex | Exact DOI singleton `/works/doi:{doi}` | Bibliographic metadata and each location's own OA, version and license claims; a work-wide OA label does not approve unrelated locations. |
| Open Library | Exact ISBN `/api/books?bibkeys=ISBN:…&format=json&jscmd=data` | Book metadata with a matching ISBN response key and explicit matching identifiers; preview/borrow links do not authorize document acquisition. |

The selected provider must return the same normalized DOI. DOI comparison folds
ASCII A–Z only and preserves other Unicode distinctions. No best-guess title
match or silent DOI substitution is accepted. Provider data is bounded, parsed
as data, and rendered as text. An OA flag or URL is not a new license grant.
An accepted manuscript can differ from the publisher's version and pagination.
Publication page extent is never treated as a verified quotation pinpoint.
OpenAlex's language field describes its title/abstract metadata, so it is shown
as a provider warning and never replaces the imported book's full-text language.

An ISBN record must explicitly contain the queried ISBN. Its equivalent ISBN-10
or ISBN-13 can also be present; an unrelated ISBN or a guessed title match is
rejected. A book's page count is not converted into a publication page range.
Saving an ISBN record creates no DOI and does not replace an existing record.
Open Library currently documents this Books API as a legacy endpoint; service
availability is checked at request time and cannot be guaranteed by local tests.

OpenAlex's current authentication and singleton documentation permits basic
anonymous queries and free exact DOI singleton requests. This integration uses
that bounded query only; it does not create an account, submit an email to obtain
a key, purchase credits or invoke paid search/download endpoints. Changed access
requirements produce an unavailable-provider result, not an automatic purchase.
Publisher-specific login, paywall/DRM bypass, browser cookies and automatic
library-wide enrichment are not implemented. No model weights are needed.

## Privacy, cancellation and recovery

The offline core and all existing telemetry/model-offline settings remain in
force. Metadata requests and downloads each require explicit permission. Permissions
are not remembered across dialogs or application restarts. An in-memory cache
avoids repeated identical requests for ten minutes; a cached result still needs
an explicitly approved lookup. The cache is local, bounded and not backed up.

HTTPS hosts are exact allowlisted destinations. Credentials in URLs, non-HTTPS,
private/loopback/link-local IPs and unapproved redirects are rejected. The actual
connection is pinned to a validated public address while TLS verifies the host.
Each metadata request and its redirects remain on that provider's host, including
inside the isolated worker. Selecting several providers never authorizes one to
forward a provider-specific contact email to another selected provider.
No proxy environment, cookies, automatic alternate provider or arbitrary POST is
used. Rate-limit responses receive at most two short bounded retries. The
supervising parent can terminate the isolated network process on cancellation
or deadline, including stalled DNS/TLS/body reads.

JSON responses are limited to 2 MiB and this PDF acquisition path to 64 MiB,
matching the current verified source import boundary. Lookup has a 60-second
operation budget and download 120 seconds. These are explicit current limits,
not a claim that V3's multi-GB document target is complete. PDF content and the
declared response size are checked, reads are chunked, and originals are never
overwritten. Only approved source-host redirects are followed; a publisher moving
to a different CDN needs a fresh supported source, not a silently expanded grant.

Download publication uses exclusive new-folder reservation, an incomplete marker,
file hashes and durable writes. A failed or cancelled download leaves no completed
document. Once a verified download is saved, an import/quarantine/reference-link
failure is reported as a **partial result** with the PDF and receipt retained.
Retry that local file through Import Center; it need not be downloaded again.
On a crash, a remaining `.acquisition-incomplete` marks a folder that must not be
treated as a completed acquisition.

The provenance records the identifier type, DOI/ISBN, public source URLs, provider license/version statements,
retrieval time, response hashes, PDF hash/size and the explicit acquisition
acknowledgement. It records no contact email. Completed imports retain exact source
bytes; the academic reference records the acquisition provenance in its notes,
which the existing full-library backup includes. The independently chosen download
folder is not silently added to backups. Existing private notes and source links
are preserved; a different already-linked edition is reported for manual review.

## Headless use

List library IDs with `research-library --list-libraries`. Then explicitly request
one DOI in exactly one library:

```text
research-library --data-dir LIBRARY --workspace ID --lookup-doi 10.5555/example --allow-online-doi
```

Repeat `--doi-provider crossref`, `--doi-provider europe_pmc`,
`--doi-provider openalex`, or `--doi-provider unpaywall` to select providers. For Unpaywall, set your own contact
email in an environment variable and name it with `--unpaywall-email-env VARIABLE`.
No email is written to the preview, provenance receipt or application settings.
`--save-doi-reference` saves the reviewed bibliographic proposal. JSON output
includes provider metadata and sources without raw response bytes.

For one book ISBN, use its separate explicit consent:

```text
research-library --data-dir LIBRARY --workspace ID --lookup-isbn 9780306406157 --allow-online-isbn
```

Add `--save-isbn-reference` to save the preview. ISBN mode rejects DOI-provider,
Unpaywall-email and source-download options; it does not imply open access to the
book. Both modes require exactly one explicitly selected workspace.

To download after reviewing the preview, supply its exact PDF URL:

```text
research-library --data-dir LIBRARY --workspace ID --lookup-doi DOI --allow-online-doi --doi-provider unpaywall --unpaywall-email-env VARIABLE --download-source-url HTTPS_URL --acquisition-folder NEW_FOLDER --confirm-source-rights
```

The freshly returned source must still match that exact URL and report an OA PDF.
An ambiguous or disappeared URL is rejected. A completed import or exact-byte
duplicate returns exit code 0; a retained download with pending/failed import
returns 2 and a structured partial result. Validation/transfer failures return 1
(invalid command-line combinations return argparse's 2).

## Upstream API evidence

The implementation uses Python's standard HTTPS/JSON/process libraries and the
existing academic/import services; no provider client code is vendored.
API behavior was checked against these primary sources on 2026-11-26:

- [Crossref REST API reference](https://github.com/CrossRef/rest-api-doc)
- [Crossref access, rate limits and caching](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/)
- [Unpaywall API](https://unpaywall.org/api)
- [Unpaywall data format and OA locations](https://unpaywall.org/data-format)
- [Europe PMC REST documentation](https://europepmc.org/RestfulWebService)
- [OpenAlex current authentication](https://help.openalex.org/api/authentication/)
- [OpenAlex exact singleton queries](https://help.openalex.org/api/get-single-entities/)
- [OpenAlex work attributes](https://help.openalex.org/data/works/attributes/)
- [OpenAlex source locations](https://help.openalex.org/data/locations/)
- [Open Library Books API](https://openlibrary.org/dev/docs/api/books)

External API availability and deposited metadata quality may change. Automated
fixtures establish our request/response contracts and failure handling, not live
provider availability, blanket download rights, or manual screen-reader acceptance.
