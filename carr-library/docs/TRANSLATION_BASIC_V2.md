# Google Translation excerpt integration

The native **Translation → Translate text** action uses the existing connection
profiles, Windows credential vault, reviewed request approval, durable budget
ledger and desktop background job. Opening the dialog or saving a profile does
not make a network request.

## Implemented scope

- Official Google Cloud Translation Basic v2, explicitly selected `nmt` model.
- One plain-text excerpt, at most 5,000 Unicode code points per reviewed call.
- Explicit source language or automatic detection; explicit target language.
- Exact text and language choices bind the approval. No automatic retries,
  alternate-language fallback, scraping endpoint or shared embedded API key.
- API key sent in `X-Goog-Api-Key`, restricted to the official HTTPS host; the
  existing transport rejects redirects, proxies and non-public addresses.
- A bounded, validated single result. Invalid/uncertain responses retain their
  cost reservation and cannot reuse the same recorded operation.
- A separate JSON translation document preserves both original and translated
  text, exact UTF-8 hashes, model/request/response identities and the operation.
  It is a derived text, never an original quotation. Publication cannot replace
  an existing destination or private library file. A complete paid result is
  retained for recovery if publication fails.
- A native read-only result dialog displays the translated text. Menu commands
  participate in the existing keyboard registry. Screen-reader acceptance still
  requires actual Windows/NVDA testing.

Configure a **Google Cloud Translation Basic v2** profile in **Connections → AI
and API connections**, with endpoint `https://translation.googleapis.com`, model
`nmt`, and the user's own API key. The original provider-task dialog also supports
the same capability. Saving/importing settings does not transfer usable keys.

## Explicit remaining scope

This change does not implement automatic whole-book batching/resume, dynamic
supported-language discovery, layout-preserving document translation, glossaries,
local translation models, or Advanced v3 IAM/ADC authentication. Language-code
syntax validation is not a claim of provider support; Google may reject a pair.
No real paid API request, acoustic/language-quality test, or manual NVDA test is
implied by synthetic transport and local filesystem/SQLite regressions.

The entered amount is an approved estimate, not a server-side billing cap or
actual invoice. Provider-side account quotas remain separate. Cancelling or
losing a response after transmission may still incur a charge; there is no
automatic rebilling. Existing non-translation request fingerprints are preserved.

## Primary protocol references

- [Basic v2 translate request and response](https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate)
- [Google API key header, including Basic v2 example](https://docs.cloud.google.com/docs/authentication/api-keys-use)
- [Translation language support](https://docs.cloud.google.com/translate/docs/languages)
- [Translation authentication and Basic/Advanced distinction](https://docs.cloud.google.com/translate/docs/authentication)

These references were reviewed on 2026-11-27. No new external dependency is
required for this adapter.
