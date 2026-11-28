# Google Translation Advanced v3 in the current translation workspace

This is a narrow integration of the Advanced v3 contract from PR147
`abb5de8d407df84caaf63cf8d8fbf97e55e7315a`. It extends the existing
`carr_library.providers` gateway, OS credential vault, durable budget ledger,
`assistant.translation` jobs and native Translation dialog. It does not add
another provider framework, job store or source reader.

## Account and request scope

AI and API connections now offers both Google Basic v2 and Advanced v3.
Basic v2 retains its exact profile fields, request hash shape, endpoint and API
key flow. Advanced v3 requires a numeric Google Cloud project number, an
explicit location, the installed `general/nmt` model, and a short-lived OAuth
access token obtained by the operator through their authorized IAM/ADC setup.
The fixed authority is `https://translation.googleapis.com`.

The application does not discover ADC files, read service-account JSON keys,
run gcloud, exchange refresh tokens or refresh OAuth credentials automatically.
The operator supplies the actual future expiry with an explicit UTC offset.
The bounded token/expiry/endpoint record is stored only in Windows Credential
Manager. Public profile JSON, job checkpoints, backups, exports and diagnostics
contain neither the token nor the record. API keys are rejected for v3.

Ordinary credential replacement receives a new opaque reference and invalidates
previous remote approval bindings. Explicit same-account renewal is available
only for an unchanged connected profile. That confirmation keeps its original
reference so an approved paused job can continue. The application cannot infer
the principal of an opaque token: the same-account statement is the operator's
explicit decision, not a verified identity claim.

No network request occurs while opening settings, making a profile, loading
saved jobs, inspecting a preview or restoring a backup. Supported-language
discovery requires its own confirmation and sends no selected text. Translation
requires a separate preview and exact-data approval, with a finite integer
reservation budget. The UI rechecks the selected account, text, glossary,
language pair, title, price input, budget and library after the native approval
dialog returns.

## Advanced v3 behavior

Language discovery calls the fixed `supportedLanguages` route for the selected
model resource. Its cached inventory preserves source/target direction flags;
an omitted protobuf boolean is false. The UI and service both reject pairs
that the fetched account/model inventory does not declare supported. An
inventory is provider metadata, not independent language-quality acceptance.

Text requests use `mimeType: text/plain`, one bounded source chunk, an explicit
target, optional explicit source, and the exact project/location/model resource.
The response must report that same normalized numeric-project model. Literal
text such as `&amp;` remains literal in v3 derivatives. HTML escape decoding is
used for credential-reflection detection, not to rewrite the accepted text.

An optional existing glossary must belong to the same project and non-global
location and requires an explicit source language. The adapter uses
`ignoreCase: false`. A glossary request is accepted only from
`glossaryTranslations` with the same glossary resource and case mode; an ordinary
translation is never silently adopted as a glossary result. Glossary creation,
upload and editing, custom/LLM translation models, document-layout translation
and fallback providers are outside this installed adapter.

The gateway reserves the exact approved call before reading credentials or
sending data. Expiry is checked again before transmission, including after DNS
and TLS setup. A token expiring while an already submitted response arrives
does not discard otherwise valid paid output. The transport retains the current
DNS scope check, fixed TLS hostname, certificate validation, byte/deadline
budgets, no redirects/proxies and no automatic retry. Unexpected model/glossary,
malformed JSON, credential reflection or an ambiguous transport failure records
an uncertain charge; it does not issue the request again.

## Durable formats and recovery

| Data | Basic v2 | Advanced v3 |
| --- | --- | --- |
| Job | `carr-translation-job/1` unchanged | `carr-translation-job/2` |
| Language cache | `carr-translation-languages/1` unchanged | `carr-translation-languages/2` |
| Export provenance | `carr-translation-export/1` | `carr-translation-export/2` |
| Namespace | `state/translation` | Same existing namespace |
| API authority | Exact original profile + root + ledger | Same, plus versioned glossary request binding |

Advanced jobs preserve the immutable source and glossary, exact per-chunk
request hashes, accepted model/glossary receipts and the existing write-ahead
request intent. Explicit schema dispatch rejects v3 fields hidden inside v1,
and rejects a v2 provider hidden inside a v3 job. The same typed validator is
used by job loading and complete backup/verification/restore. Both supported
provider versions use the existing typed cache path; schema-2 cache publication
is accepted only with its exact Advanced endpoint/model binding.

The current ledger also binds each newly accepted decoded text and detected
language independently of the wire-response hash. Complete live resume and
export compare that digest with the separate ledger, including when a
checkpoint is otherwise rehashed or one side loses its digest. Historical
wire-only rows remain explicitly `legacy-wire-only`; their migration does not
invent proof. Moved or restored derivatives retain their recorded hashes and
export as `archived-unverified`, without reopening credentials or provider
connections. Export restores only the source chunk separators; stored accepted
receipts and immutable originals are preserved.

Accepted chunks survive cancellation and token renewal. An operation recorded
as unsent can resume only with its original exact approval, request and
reservation. Unknown, paid-but-missing or uncertain operations never retry
automatically. Complete restored derivatives remain exportable offline.
Restored queued jobs retain their old root and ledger identities and cannot
silently acquire fresh remote authority. Credentials and the live provider
ledger are not restored from these checkpoints.

Export creates a fresh output directory with original, translated, bilingual,
reading-draft and provenance files. Existing originals and prior exports are
not overwritten. The derivative remains outside source quotation authority;
the existing reading draft retains its reversible source relationship.

## Evidence and required gates

At candidate preparation, the execution environment was unavailable. New source,
portable tests and the native probe were prepared but **NOT_RUN**. Existing Basic
v2 results remain attributed to their prior exact source.

Required portable/source and installed-wheel gates:

- `tests/e2e/test_google_translation_gateway.py` (existing Basic v2 boundary).
- `tests/e2e/test_google_translation_v3.py`.
- `tests/assistant/test_translation.py` (existing durable Basic v2 jobs).
- `tests/assistant/test_translation_advanced.py`.
- `tests/ui/test_api_connections.py` and `tests/ui/test_translation_dialog.py`.
- `tests/ui/test_translation_advanced.py`.
- Existing complete backup and provider-ledger gates.

Native Windows gate: `python -I -B scripts/windows_translation_v3_smoke.py
--output <receipt.json>`, alongside the existing Basic v2 native probe.
The new probe uses actual native controls and a temporary actual Windows
Credential Manager entry, with synthetic REST replies and public fixture text.
It verifies the shared gateway/ledger, directional language selection, exact
model/glossary receipts, immutable original and hashed derivative files, and
removes its synthetic vault entry on exit.

Real Google account access, billing, glossary/model availability, linguistic
quality, manual keyboard usability and NVDA qualification remain separate
acceptance evidence. No real account was contacted during source preparation.

## Primary protocol references

- [Cloud Translation authentication](https://docs.cloud.google.com/translate/docs/authentication):
  Advanced v3 uses OAuth/IAM and does not support API keys.
- [Translate text](https://docs.cloud.google.com/translate/docs/translate-text):
  official REST authority and request examples.
- [v3 translateText reference](https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/translateText):
  project/location, text/plain, model and same-region glossary fields.
- [v3 supportedLanguages reference](https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/getSupportedLanguages):
  explicit model language discovery.
- [v3 RPC reference](https://docs.cloud.google.com/translate/docs/reference/rpc/google.cloud.translation.v3):
  reported numeric-project model and directional language metadata.

No new third-party runtime dependency or package inventory entry is needed:
all product changes extend already packaged modules; the HTTP/vault code uses
the existing standard-library implementation.
