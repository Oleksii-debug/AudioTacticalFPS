# Optional selected-text translation

Tools → Translate selected text uses the existing provider profiles, Windows
Credential Manager, permission envelope, usage ledger, JobStore and reversible
ReadingDocument. No model or provider SDK is installed automatically.

## Use

1. In Tools → AI and API connections, create a separate account and choose
   `google-translation-v2`. The editor fills the official endpoint
   `https://translation.googleapis.com/language/translate/v2`, model `nmt` and
   capabilities `translation.text,translation.languages`. Add your API key and
   explicitly enable the account. Saving a profile makes no request.
2. Choose that account in the translation workspace. Explicitly approve Fetch
   languages. Selected text is not sent by language discovery. The returned list
   is tied to the exact account, endpoint and model.
3. Paste selected text. Choose a source code or `auto` and a target code from the
   fetched list. Enter the maximum total reservation and, optionally, your own
   current contract price per million characters for an estimate.
4. Preview the character/call count. Translate asks explicit permission for this
   exact selected text and binds its request hashes, destination, operation,
   budget and 24-hour authorization expiry.
5. Pause/resume the same saved job. Accepted chunks are durably recorded before
   cancellation and reused. Uncertain charges are never retried automatically.
   Expired approval stops further calls; renewal requires a new explicit review.
6. Export writes a fresh directory with original, translated and bilingual text,
   a reversible reading-copy project, and provenance/file hashes. The translation
   has no source-quotation authority. Existing outputs are never overwritten.

The shared Gateway defaults to LOCAL_ONLY. The dialog changes mode only for an
explicitly approved operation. Unknown cost, wrong scope/request hash, expired
approval and exhausted reservations fail before credential use or transmission.
The API key travels in X-goog-api-key, never the URL. TLS authority is fixed;
redirects are rejected. Provider responses are text, never executable commands.

The local reservation and optional estimate are **not provider-enforced billing
limits** or a live price quotation. Actual cost/quota remains the provider
account's contract; no current price is invented by this application.

## Supported boundary and remaining qualification

Basic v2 NMT selected **plain text** is implemented, with Unicode-preserving
source storage and chunks of at most 4,500 characters. The provider's language
list establishes declared capability, not translation quality or canonical-135
coverage. Unknown/absent languages are rejected; similar names are never mapped.

Advanced v3 IAM/ADC, glossaries, formatted-document preservation, direct chapter
or document selection, local M2M100 inference, canonical-135 translation mapping,
and independent linguistic quality review remain open. No offline failure causes
a silent remote fallback or model download. Selected source text and receipts use
the existing private local state store; this feature does not supply encrypted
text-at-rest containers. API secrets use the OS vault.

A failed export may leave a partial newly created directory. Export success
requires the final provenance manifest and matching output hashes. Original
library quotations remain attached to their source, not the translated revision.

## Validation

The focused existing-provider plus translation suite passed **63 tests** on Linux
with synthetic HTTPS responses. These execute the real Gateway, ledger, JobStore
and ReadingDocument: exact TLS authority and routes, API-key header, LOCAL_ONLY
rejection, malformed profiles, request-hash consent, Unicode counts, language
validation, pause/reopen without repeat billing, source immutability, no-overwrite
exports, provenance hashes, damaged receipts and uncertain HTTP failures. Additional review
regressions reject credentials encoded through JSON Unicode escapes or HTML entities
before receipts/cache publication, and permit exact not-sent cancellation retries
without allowing changed authority or any reserved/uncertain/completed replay.

No live Google request, user-document upload or charge was made. Translation
quality, real provider billing, native wx and NVDA remain separate acceptance.

## Official API references

- https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate
- https://docs.cloud.google.com/translate/docs/reference/rest/v2/languages
- https://docs.cloud.google.com/docs/authentication/api-keys-use
- https://docs.cloud.google.com/translate/docs/translate-text
