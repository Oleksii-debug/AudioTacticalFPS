# DOI metadata and open-access acquisition

This is the V3-08 DOI workflow in the existing Universal Research Library desktop.
Open **Academic > DOI lookup and open-access sources**. Opening this dialog makes
no network request. Bibliographic metadata, downloaded documents and approved
quotations remain separate.

## Use the desktop

1. Select the destination library. A selected local book is optional.
2. Enter a DOI, `doi:` identifier, or a `https://doi.org/…` resolver URL.
3. Select Crossref, Europe PMC, and/or Unpaywall. Crossref is the default.
   Unpaywall requires your own contact email, held only for this dialog/request.
4. Explicitly allow this online lookup and choose **Look up DOI**. Only the DOI
   and the optional provider contact email are sent. Source text, notes, saved
   quotations, unrelated catalog entries and credentials are never uploaded.
5. Review all supplied metadata and provider response hashes. Missing data stays
   missing. Different provider titles and version information are shown for review.
6. **Save bibliographic reference** saves an independent academic reference.
   Exact DOI deduplication preserves an existing reference and its private notes.
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

The selected provider must return the same normalized DOI. DOI comparison folds
ASCII A–Z only and preserves other Unicode distinctions. No best-guess title
match or silent DOI substitution is accepted. Provider data is bounded, parsed
as data, and rendered as text. An OA flag or URL is not a new license grant.
An accepted manuscript can differ from the publisher's version and pagination.
Publication page extent is never treated as a verified quotation pinpoint.

This increment does not add ISBN searches, OpenAlex, publisher-specific login,
paywall/DRM bypass, browser cookies, or automatic library-wide enrichment. These
are not implied by a successful DOI lookup. No model weights or commercial API
calls are needed.

## Privacy, cancellation and recovery

The offline core and all existing telemetry/model-offline settings remain in
force. DOI requests and downloads each require explicit permission. Permissions
are not remembered across dialogs or application restarts. An in-memory cache
avoids repeated identical requests for ten minutes; a cached result still needs
an explicitly approved lookup. The cache is local, bounded and not backed up.

HTTPS hosts are exact allowlisted destinations. Credentials in URLs, non-HTTPS,
private/loopback/link-local IPs and unapproved redirects are rejected. The actual
connection is pinned to a validated public address while TLS verifies the host.
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
document. The download SHA-256, PDF MIME and byte count are bound to the existing
durable import request before isolated parsing. A file changed after download is
not imported as that source; restore the exact bytes matching the receipt and
resume the same request in Import Center, rather than importing changed bytes.
A failed or quarantined import retains the local file and receipt for review.
If import already committed but reference linking stopped, the result
`imported_reference_pending` retains the edition ID; review or link that existing
edition in the academic library. Do not import or download it again.
On a crash, a remaining `.acquisition-incomplete` marks a folder that must not be
treated as a completed acquisition.

The receipt records DOI, public source URLs, provider license/version statements,
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

Repeat `--doi-provider crossref`, `--doi-provider europe_pmc`, or
`--doi-provider unpaywall` to select providers. For Unpaywall, set your own contact
email in an environment variable and name it with `--unpaywall-email-env VARIABLE`.
No email is written to the preview, provenance receipt or application settings.
`--save-doi-reference` saves the reviewed bibliographic proposal. JSON output
includes provider metadata and sources without raw response bytes.

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

External API availability and deposited metadata quality may change. Automated
fixtures establish our request/response contracts and failure handling, not live
provider availability, blanket download rights, or manual screen-reader acceptance.
