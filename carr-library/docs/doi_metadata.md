# DOI metadata and native citation actions

## Working features

`carr_library.academic_integration.doi` retrieves a **metadata preview** from
Crossref and, when separately selected, Unpaywall. Networking is disabled by
default. The adapter has no document download, full-text upload, automatic
library import, paywall, or DRM operation. Opening a dialog makes no request.

The native Academic menu gains these actions through
`carr_library.ui.citation_formatting.install_citation_actions(frame)`:

- **Format bibliography and citations** (`Ctrl+Alt+C`): select active academic
  references, one of the installed CSL styles and locales, add optional ordered
  citations, preview, and save a new DOCX. `F6` formats locally. `Ctrl+Tab` changes
  the References, Citations, and Preview sections. Every editable or readable
  native control has a text label; cancellation and Close remain available.
- **Preview DOI metadata (opt-in)**: enter a DOI, enable requests for that dialog,
  select services, and inspect source, version, licenses and review notes.
  Unpaywall requires the user's contact email. Save preview JSON records the
  reviewed data for later decisions; it does not import it into the library.

Both actions bind to the original `LibraryApp` object and the exact active
workspace. There is no search or export fallback to other libraries. Formatting
uses 1–1000 active academic references, at most 8 MB of exchange JSON, and excludes
private notes and abstracts. Author names stored by the existing academic
library as literal text remain literal; the UI explicitly asks for their review.
It does not invent structured personal names from ambiguous text.

Publication page extents are separate from citation locators. A locator needs
the user's explicit source-check checkbox. Changing its source, value or type
clears that checkbox. Reloading changed bibliography/source bindings retracts
the verification flags of retained locator drafts. Those citations must be
reviewed and added again before formatting. Linked-source identity participates
in snapshot hashes even if the replacement has identical bibliographic fields.
These checks do not grant or modify the application's quotation approvals.

## Python API

```python
from carr_library.academic_integration import DoiMetadataClient

# Only create this enabled client after the user chooses the online action.
client = DoiMetadataClient(allow_network=True, providers=("crossref",))
preview = client.lookup("10.5555/example", cancelled=cancel_event.is_set)

item = preview.csl_item       # Independent copy of a bounded CSL JSON record.
review = preview.to_dict()    # Serializable, versioned provenance and rights.
```

For Unpaywall, explicitly select `providers=("crossref", "unpaywall")` or
`providers=("unpaywall",)` and provide `unpaywall_email=the_users_email`.
The Crossref public adapter sends no email or authorization credential.
Crossref remains the primary bibliographic source when both providers are
selected; Unpaywall fills missing fields. Conflicting values generate review
warnings. Unpaywall's deprecated `z_authors` field is deliberately omitted.

`DoiMetadataPreview` is frozen. Its `csl_item` property and `to_dict()` return
independent copies. It exposes `doi`, the stable `doi-<SHA256>` reference ID,
`sources`, `field_sources`, `licenses`, `locations`, `open_access`, `oa_status`,
`warnings`, `from_cache`, and an always-true `review_required` flag. Raw API
responses, abstracts, reference lists, credentials and request emails are not
included in the preview. Provenance contains the exact response SHA256, byte
count, UTC retrieval time and fixed endpoint with the email query removed.

The native controller is independently callable:

```python
from carr_library.ui.citation_formatting import CitationFormattingController

controller = CitationFormattingController(library, workspace_id)
snapshot = controller.load(cancelled=cancel_event.is_set)
formatted = controller.render(snapshot, style="apa", citations=(),
                              cancelled=cancel_event.is_set)
controller.export_docx(snapshot, formatted, new_docx_path,
                       cancelled=cancel_event.is_set)
```

The controller rejects another controller's snapshot, a changed bibliography,
another workspace, an archived workspace, existing output files, and export
paths inside the application's private library folder. It never edits the
sources or academic records. citeproc-js attribution is visible in the native
preview and DOCX. See `academic_integration.md` for engine and style licensing.

## Network and privacy boundary

Only `https://api.crossref.org:443/works/{encoded_doi}` and
`https://api.unpaywall.org:443/v2/{encoded_doi}?email=...` are accepted. The complete
DOI is percent-encoded as a route argument; arbitrary URL hosts, credentials,
resolver queries/fragments and ambiguous traversal-like DOI paths are rejected.
The returned DOI must match the requested normalized identifier.

The default transport uses `http.client.HTTPSConnection` with standard verified
TLS, fixed hosts, no environment proxies, no cookies and no redirects. It asks
for uncompressed JSON. Success responses have bounded headers, content length,
streamed bytes, JSON depth, node count and field lengths. Error bodies are not
read or displayed. A supervised subprocess enforces the overall deadline and
cancellation even while DNS or a slow socket blocks. Default limits are 15
seconds and 2 MB per service; configurable limits are at most 60 seconds and
8 MB. There is no automatic retry. HTTP 429 records a bounded Retry-After cooldown.
One client serializes concurrent lookups and spaces each service's calls by one
second. Its bounded memory cache holds at most 128 previews for one hour; explicit
`refresh=True` bypasses the cache without changing provider authorization.

Contact email travels only in the explicit Unpaywall request. It is excluded
from request reprs, error messages, process arguments, provenance, exported JSON
and environment variables. The native dialog does not persist it. No paid
service or API key is used. The worker environment omits proxy, Python injection
and TLS key-log environment settings.

Provider links are inert metadata. URL validation excludes credentials, unsafe
schemes, local/private IP literals, obvious private host names and nonstandard
ports; it does not resolve publisher DNS or claim that a host is trustworthy.
Any future downloader needs its own authorization and destination/redirect/DNS
checks. A declared license, `is_oa=true`, or a PDF link never sets
`permission_verified=True`. License versions and applicable manuscript copies
remain distinct. Missing/implied licenses and embargoed copies carry explicit
review notes. `oa_date` is not used to infer availability or embargo expiry.

## Frozen application entry point

Source Python invokes the fixed worker with `-I doi.py --metadata-worker`.
Frozen builds invoke the executable with `--carr-doi-metadata-worker`. The main
entry point must handle that exact sole argument **before starting wx**:

```python
if arguments == ["--carr-doi-metadata-worker"]:
    from carr_library.academic_integration.doi import _worker_main
    return _worker_main()
```

The root application already provides this dispatch. Request data enters over
stdin. Worker output contains bounded JSON or a fixed error category; no remote
exception or request URL is printed.

## Verification and limits

`tests/academic_integration/test_doi.py` exercises real loopback HTTP requests,
opt-in enforcement, normalization, provenance, immutable previews, missing or
conflicting metadata, license/version distinctions, cache, redirects, wrong
hosts, response limits, schema rejection, TLS configuration, email redaction,
frozen command selection, and actual process deadline/cancellation/reaping.
No public service request or user credential is needed or used by the tests.

`tests/academic_integration/test_citation_ui.py` uses real application SQLite
transactions, the installed local Node CSL engine, and generated DOCX packages
to check workspace isolation, source immutability, privacy, source-binding
changes, stale exports, cancellation, and metadata-only DOI preview export.
wx is not installed in this Linux test runtime. Native Windows focus, NVDA
announcements and the packaged executable need the Windows acceptance run;
headless controller results are not reported as screen-reader verification.

## Primary API documentation consulted

- Crossref: [REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/),
  [access and authentication](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/),
  [official REST API documentation](https://github.com/CrossRef/rest-api-doc),
  [license/version metadata guidance](https://github.com/CrossRef/rest-api-doc/blob/master/funder_kpi_metadata_best_practice.md).
- Unpaywall: [API v2 and contact-email requirement](https://unpaywall.org/api),
  [DOI and OA location schema](https://unpaywall.org/data-format),
  [current OpenAlex/Unpaywall notes on deprecated authors and OA dates](https://help.openalex.org/access/unpaywall/).

API documentation was reviewed on 2026-11-26. A provider declaration is
retained as evidence to review; this adapter does not interpret it as legal
authorization to obtain or redistribute the associated publication.
