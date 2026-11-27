# Optional selected-text translation: Google Basic v2 and Advanced v3

Tools → Translate selected text uses the existing provider profiles, Windows
Credential Manager, permission envelope, usage ledger, JobStore and reversible
ReadingDocument. No model or provider SDK is installed automatically.

## Use

1. In Tools → AI and API connections, create a separate account and choose
   `google-translation-v2`. The editor fills the official endpoint
   `https://translation.googleapis.com/language/translate/v2`, model `nmt` and
   capabilities `translation.text,translation.languages`. Add your API key and
   explicitly enable the account. Saving a profile makes no request.
   Advanced v3 uses the separate setup below.
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
   Resume also requires the exact account snapshot to remain enabled in AI/API
   connections. Removing, disabling or changing that account after opening the
   window or queuing work blocks further requests before credential access.
6. Export writes a fresh directory with original, translated and bilingual text,
   a reversible reading-copy project, and provenance/file hashes. The translation
   has no source-quotation authority. Existing outputs are never overwritten.

The shared Gateway defaults to LOCAL_ONLY. The dialog changes mode only for an
explicitly approved operation. Unknown cost, wrong scope/request hash, expired
approval and exhausted reservations fail before credential use or transmission.
The Basic API key travels in X-goog-api-key, never the URL. Advanced uses an
OAuth bearer token and the explicit project for quota attribution. TLS authority is fixed;
redirects are rejected. Provider responses are text, never executable commands.

The local reservation and optional estimate are **not provider-enforced billing
limits** or a live price quotation. Actual cost/quota remains the provider
account's contract; no current price is invented by this application.

## Advanced v3 setup and existing glossaries

Choose `google-translation-v3` in the existing account editor. Enter:

- Endpoint: `https://translation.googleapis.com/v3/projects/PROJECT_NUMBER/locations/LOCATION`.
  Replace both placeholders; use your **numeric project number**, not its name or
  project ID. This deliberately limited adapter can compare the returned model
  and glossary resources exactly without guessing Google's ID-to-number mapping.
- Model: `general/nmt`. Custom AutoML models and Translation LLM are not enabled by
  this adapter. Choose `global` for ordinary NMT, or the region of your existing
  glossary, such as `us-central1`.
- Capabilities: `translation.text,translation.languages`.
- Credential: a short-lived OAuth access token from your authorized IAM, gcloud
  or ADC identity, plus its actual expiry with UTC offset. For example, the expiry
  field accepts `2026-11-27T15:30:00Z`; enter the real future time, not this example.
  The token is stored only in Windows Credential Manager, bound to the exact
  endpoint. The application never reads ADC files, launches gcloud, discovers a
  service account, refreshes tokens or falls back to an API key. Do not paste a
  service-account JSON key or refresh token.

Google's official authentication guide distinguishes gcloud credentials from
ADC. Obtain the access token using your already authorized setup outside this
application. Appropriate project/IAM permissions and Google billing/quota remain
the account owner's responsibility. This adapter records the chosen project in
`X-goog-user-project`; a different quota project is not silently substituted.

The account's local configuration check displays the token's expiry. An expired
token fails before a network call and leaves the attempt recorded as `not_sent`.
Re-enter a current token and expiry while keeping the exact same account settings;
the editor retains its opaque vault reference, allowing the existing saved job
to resume within its original scope and budget. Accepted chunks are reused.
Changing account/model/endpoint settings creates a different reference. This
does not renew the job's separate 24-hour approval. An HTTP response already
received with an error is an uncertain attempt and is never retried just because
a new token was entered.

Fetch languages explicitly. Advanced keeps Google's `supportSource` and
`supportTarget` flags separately; a source-only language cannot be chosen as a
target. Missing flags do not imply support.

To apply terminology, enter an **existing** glossary resource in the translation
window, in the form
`projects/PROJECT_NUMBER/locations/LOCATION/glossaries/GLOSSARY_ID`. Choose an
explicit source language and the same project/non-global region as the model.
Matching is case sensitive. The exact glossary is shown in preview and transfer
confirmation and included in the approved request hash. The gateway requires a
`glossaryTranslations` response with the matching glossary configuration and
model. A missing/different glossary never falls back to the ordinary result.
Requested and reported resources are retained in chunk/export provenance.
Creating/uploading glossaries and proving their language coverage are separate
operations; this feature does not do them implicitly.

## Command-line workflow

The same service is available without wx through
`python -m carr_library.assistant.translation_cli --data-dir LIBRARY COMMAND`.
The library must already exist. No command accepts a secret on argv.

1. `profiles` lists non-secret configured accounts.
2. `languages --profile ACCOUNT` reads only the saved language inventory. Add
   `--approve-remote` to approve a new metadata request; no source text is sent.
3. `preview --profile ACCOUNT --input selected.txt --source en --target uk --budget 1`
   reads bounded UTF-8 plain text and prints its character count, exact account,
   endpoint, model, languages, cost reservation and `review_sha256`. Optionally
   supply `--glossary RESOURCE`, `--title TITLE` and `--rate-per-million RATE`.
4. Repeat the same arguments with `submit`, plus `--approve-remote` and
   `--review-sha256 HASH`. The command rejects changed text/settings before saving
   a job. It saves the approved source snapshot without sending it yet.
5. `resume JOB_ID` runs that job's existing approval. Ctrl+C requests a pause;
   an in-flight bounded HTTP call may need to return or time out, and an uncertain
   charge is not automatically retried. Paused exit status is 2, errors are 1.
6. `jobs` lists saved statuses. `export JOB_ID --output NEW_DIRECTORY` publishes
   the same original/translated/bilingual/reading-copy files and provenance as
   the native window, with no overwrite.

This is local bounded chunk scheduling of selected text, not Google's Cloud
Storage batch service. It does not upload a document directory or buckets.

## Supported boundary and remaining qualification

Basic v2 and Advanced v3 NMT selected **plain text** are implemented, with Unicode-preserving
source storage and chunks of at most 4,500 characters. The provider's language
list establishes declared capability, not translation quality or canonical-135
coverage. Unknown/absent languages are rejected; similar names are never mapped.

Automatic ADC discovery/token refresh, glossary creation, formatted-document
preservation, inferred chapter selection, Cloud Storage batch jobs,
local M2M100 inference, canonical-135 translation mapping,
and independent linguistic quality review remain open. No offline failure causes
a silent remote fallback or model download. Selected source text and receipts use
the existing private local state store; this feature does not supply encrypted
text-at-rest containers. API secrets use the OS vault.

## Retained document and explicit range translation

Tools → Translate selected book or source range reuses the authorized reading
desk. Select an explicit source position or `first-last` range; the full range
selects the retained document. Printed page labels and chapter boundaries are
not guessed. Loading runs on the existing executor, keeps the selected source
read only, and sends no text. Source and translated reading copies each retain
the existing two-million-character bound, with checkpoint/serialization space
reserved before billable calls.

The same Basic v2 and Advanced v3 services handle this range, including the
explicit Advanced glossary and model. Preview/consent binds the source snapshot,
account, languages, glossary and budget. Source jobs keep a separate typed job
identity in the existing JobStore, anchored to the retained asset, edition,
revision, workspace, topic and source positions. A changed/revoked source cannot
silently be retargeted, downgraded to a pasted-text job or sent under the old
approval. Moving/restoring the library preserves completed output; further
remote requests require fresh review against the new library identity.

Accepted output is saved before later cancellation or scope checks. New source
jobs require the canonical independent decoded-text/language receipt in the
usage ledger; the stronger Basic/Advanced receipt is reused, with no parallel
receipt store. Older unbound pasted-text derivatives remain exportable offline
with an explicit legacy integrity label rather than invented proof.

Saved jobs restore the exact visible account, source, languages, glossary and
budget. Reading that list runs in the existing worker. Changing the visible
scope is rejected against the selected snapshot before queuing work or opening
an export picker. That fast comparison grants no authority: the saved record and
current account are checked again in the existing worker. Preview or New
translation starts a new review. Missing/changed accounts block
remote resume while preserving completed offline exports. The worker rereads the
current profile before constructing a gateway and at existing remote-operation
checks. Language discovery also checks the current account after queuing. Status
errors focus the named status control. Bound exports retain
the original source anchor in the reversible reading project and provenance;
translated edits never acquire source-quotation authority. Publication rechecks
source authorization and uses the existing writer guard, new output directory
and private-destination rules.

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

The Advanced source suite `tests/assistant/test_translation_v3.py` exercises the
real shared gateway, ledger, jobs, derivative exports, CLI and actual dialog
handlers with synthetic HTTPS responses and widget/scheduler stand-ins. It
checks exact project/model/glossary consent, directional language capability,
expiry and explicit re-entry without duplicate accepted calls, stale CLI/UI
reviews, disabled controls during queued work and close/cancellation.

The required installed Windows probe is
`python -I -B scripts/windows_translation_v3_smoke.py`. It checks real wx native
Space/focus, asynchronous completion, temporary Windows Credential Manager
storage and output bytes. Its REST responses and confirmation/file-picker
results are deliberately synthetic. Linux execution reports NOT_RUN and exits
77; it is not a portable replacement for the Windows gate.

No live Google request, user-document upload or charge was made. Real API account
access, IAM configuration, billing, glossary coverage and translation quality
remain **WAITING_INPUT**. Native Windows/NVDA acceptance must be reported for the
exact installed build; source tests do not establish it.

## Official API references

- https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate
- https://docs.cloud.google.com/translate/docs/reference/rest/v2/languages
- https://docs.cloud.google.com/docs/authentication/api-keys-use
- https://docs.cloud.google.com/translate/docs/translate-text
- https://docs.cloud.google.com/translate/docs/authentication
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/translateText
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/getSupportedLanguages
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/SupportedLanguages
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/TranslateTextResponse
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/TranslateTextGlossaryConfig

The Advanced REST/authentication references above were reviewed on 2026-11-27.
No live provider pricing or account capability was inferred from them.

The current-account catalog checks and saved-job regressions reuse public PR165
at `5baee73ae94d860909152fb8c8434d5864c0e638`, adapted to retain Advanced v3
project/glossary scope and the existing durable pasted-text capture. Reconstructed
source and handler tests require execution on the composed candidate; prior donor
results do not qualify this build.
