# Optional plain-text and library-source translation: Google Basic v2 and Advanced v3


Tools → Translate selected text and Tools → Translate selected book or source range
use the existing provider profiles, Windows Credential Manager, permission
envelope, usage ledger, JobStore and reversible ReadingDocument. No model or
provider SDK is installed automatically.

## Use

1. In Tools → AI and API connections, create a separate account and choose
   `google-translation-v2`. The editor fills the official endpoint
   `https://translation.googleapis.com/language/translate/v2`, model `nmt` and
   capabilities `translation.text,translation.languages`. Add your API key and
   explicitly enable the account. Saving a profile makes no request.
   Advanced v3 uses the separate account setup below; both use the same source workflow.
2. Choose Translate selected text to paste text, or select a book in the library
   and choose Translate selected book or source range. Enter one source reading
   position or a range such as `2-5`. The initial `1-last` range selects the full
   document. These are the reader's ordered source positions; they are not
   inferred chapter titles or printed page labels.
3. The book selection loads locally through the existing authorized reader.
   Its original text is read-only in this translation window. The source summary
   shows the title, position range, character count and limits. Unreviewed
   extracted text remains labelled as unreviewed. Loading sends no text.
4. Choose the Google account and explicitly approve Fetch languages if its
   capability list has not been fetched. Selected text is not sent by language
   discovery. The returned list is tied to the exact account, endpoint and model.
5. Choose a source code or `auto` and a target code from the fetched list.
   Enter the maximum total reservation and, optionally, your own current
   contract price per million characters for an estimate.
6. Preview the character/call count. Translate asks explicit permission for
   this exact selected text and binds its request hashes, destination, operation,
   budget and 24-hour authorization expiry. Source authorization and the retained
   original are checked again in the worker before transmission.
7. Pause/resume the same saved job. Accepted chunks are recorded durably and
   reused. Uncertain charges are never retried automatically. Expired approval
   stops further calls; a new approval requires a new explicit preview and review.
   Saved jobs display their original text and settings without permitting edits
   to the approved request. New translation starts a separate reviewed job.
   Resume also requires the exact unchanged account snapshot to be visible and
   currently enabled in AI/API connections. Account removal, disabling or
   replacement after opening the window blocks further requests. Explicit
   errors and saved-job selection move focus to the named status field.
8. Export writes a fresh directory outside the private library with original,
   translated and bilingual text, a reversible reading-copy project, and
   provenance/file hashes. Existing outputs are never overwritten.

The shared Gateway defaults to LOCAL_ONLY. The dialog changes mode only for an
explicitly approved operation. Unknown cost, wrong scope/request hash, expired
approval and exhausted reservations fail before credential use or transmission.
The Basic API key travels in X-goog-api-key, never the URL. Advanced uses an
OAuth bearer token and its exact numeric project for quota attribution. TLS authority is fixed;
redirects are rejected. Provider responses are text, never executable commands.
Completed saved work remains exportable offline when its account is unavailable;
the selected text, languages, glossary and numeric budget must still match the saved job,
and library-source exports still require current source authorization.

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


## Source lineage, interruption and recovery

A library-source job retains the original reading text and the canonical
workspace, edition, revision, asset digest and source-position anchor. The
translated reading project starts from that original document and records a
separate machine-translation edit. It does not attach original literal offsets
to changed translated text. Undo restores the original reading snapshot;
inserted position separators remain derived. Translation never grants quotation
authority or changes original files, citations or research notes.

A changed active revision, changed retained original, archived library or
archived selected research topic blocks further requests or a new export.
The existing reader validates the full selection at submission/resumption and
the relevant positions again before each outgoing chunk. Fresh authorization
and revision checks also run at the provider's existing cancellation points.
An accepted chunk is saved before later scope/cancellation checks so a source
change cannot erase a paid result and trigger an automatic repeat. A cancellation
after the final accepted chunk leaves the job complete.

Backups retain these jobs through the existing state snapshot. New library-bound
jobs also bind remote continuation to the original data-directory path and
filesystem directory identity. A moved/restored directory, including recreation
at the same path, cannot silently inherit the prior remote permission. Its
accepted chunks remain stored; an unfinished translation needs a fresh preview
and approval as a new job. A completed translation can be exported locally from a
restored library after current source authorization, without a new provider call,
credential read or renewal of an expired approval.

New source-bound jobs use `translation/2` inside the existing assistant JobStore.
Their deterministic UUIDv5 ID binds source lineage, source text digest, provider
snapshot and approval. Legacy pasted-text jobs retain their existing UUIDv4 IDs.
The loader checks this producer distinction even before the first usage record,
so changing only the saved kind, source binding and consent fields cannot turn a
source-bound job into a legacy one.
New Google usage receipts also bind the exact decoded translation text by SHA256;
changing a saved translation and its self-hash cannot borrow the original response
receipt. Source jobs require this decoded-output proof. Older pasted-text receipts
that never recorded it remain exportable with `decoded_text_binding: unrecorded-legacy`
in their provenance, rather than claiming missing provider-content proof.
New source approvals use the already allowlisted selected-script data class;
legacy pasted-text approvals use selected-excerpt. Changing a saved source job to
the legacy kind cannot discard its source authorization checks. Existing
`translation/1` pasted-text jobs retain their supported behavior.
Unknown or mixed job formats fail closed. Tasks understands both saved job versions
and shows only their stored status, accepted chunk counts and bound workspace.
It does not verify translation quality, show source text, renew approval or start
a provider call. Resume stays in the translation window or explicit CLI command.

Export rechecks the source after the folder picker and holds the existing
application writer guard while publishing the derived files. It rehashes the
retained original after acquiring that guard and before the completion manifest.
Archival uses the same guard. No database write transaction spans source hashing or network work.
A failed or cancelled export may leave a partial newly created directory.
Export success requires the final provenance manifest and matching output hashes;
the saved completed translation remains available for another explicit export.


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


Source jobs created by the native book/range action are listed by the same `jobs`
command. `resume` and `export` open the explicitly selected existing LibraryApp
so the service can reauthorize that exact retained source. Incomplete remote
resume rereads the saved account from the current catalog at every source guard;
removed, disabled or changed accounts cannot use their old snapshot. Completed
receipts remain locally replayable and exportable without a credential read or
new provider request, subject to source authorization.


## Supported limits and remaining qualification

Basic v2 and Advanced v3 NMT selected **plain text**, including the full bounded reading text of
an imported document or an explicit source-position range, is implemented.
The source and translated reading copy each support at most 2,000,000 characters.
Requests contain at most 4,500 characters. Library-source jobs additionally bound
each provider response to 200,000 bytes and stop further requests if accepted
translated text or its reconstructed source separators exceed the reading-copy budget.
Before a new source job is saved, a conservative JSON-size preflight reserves the
existing 64 MiB checkpoint and 32 MiB reading-project limits, including escaped
source text, a single translation edit and accepted receipts. Already accepted chunks remain
stored; an over-limit job is not reported complete.

The provider's language list establishes declared capability, not translation
quality or canonical-135 coverage. Unknown/absent languages are rejected;
similar names are never mapped. Export restores source whitespace at request
joins without altering the saved provider receipts. This preserves tested
plain-text joins; it is not a claim of page layout, table, footnote or formatted
document fidelity.

Automatic ADC discovery/token refresh, glossary creation, automatic chapter-title
interpretation, batch document selection, formatted-document preservation, local M2M100 inference,
canonical-135 translation mapping and independent linguistic quality review
remain separate work. No offline failure causes a silent remote fallback or
model download. Source text and receipts use the existing private local state
store; this feature does not supply encrypted text-at-rest containers. API
secrets use the OS vault.


## Reuse and verification

This extension retains the canonical provider/reader architecture. It reuses the
approved-request/usage-ledger receipt checks, decoded-output validation and
plain-text join assembly from PR152 at
`112935aa15952682a885b10c322b3b4016c6cc80`.
It does not import that candidate's separate transport rewrite or the alternative
PR136 provider architecture.
The composed source/range feature reuses PR165 at
`5baee73ae94d860909152fb8c8434d5864c0e638`, preserving the existing Advanced v3
request, OAuth, model/glossary and CLI contracts. The saved-selection matcher and validated job accessor reuse PR162 at
`57cbd5d6c825f42b5769de9fa7ec6faf779622cc`. The native callback additionally
rereads the existing profile catalog in its worker before creating the gateway
and at the existing remote-operation authorization checks.

The existing `tests/assistant/test_translation.py` suite now exercises actual
LibraryApp, ReadingDesk, retained originals, provider ledger, JobStore,
ReadingDocument and library backup/restore with synthetic HTTPS responses.
Regressions cover whole documents and ranges, exact consent, source changes
before submission/between chunks/before export, revocation during response,
durable accepted prefixes, completed-after-cancel semantics, changed source
bindings, same-path and new-path restore, archival exclusion, output/cancellation
bounds, and native-dialog/menu callback contracts. Existing provider and
source/reading regression suites remain required. Dialog doubles are not native
Windows or NVDA evidence.

Exact source, installed Windows and manual accessibility results belong to the
candidate's validation record. This document does not transfer earlier test
counts to a later source tree. No live Google request, user-document upload or
charge was made by this implementation work. Real provider billing and
translation quality require separate explicit execution.

The composed source was checked with 348 local cases: 141 Basic/source cases,
59 existing Advanced cases, 16 source/Advanced/Tasks/CLI composition cases,
19 existing Tasks cases, 37 source-reader cases, 48 reading-project cases and
28 provider cases. All passed with no skips. The provider suite includes real
owned loopback servers, so it ran in an isolated network namespace with loopback
enabled and an external connection refused with `ENETUNREACH`; a blanket ban on
all sockets cannot exercise those existing tests. The other suites ran through
the required offline `ci_check.py` gate.

The 16 composition cases in `tests/assistant/test_source_translation_composition.py`
include both Basic and Advanced completed jobs through actual backup, deletion
of the original library, restoration elsewhere and offline CLI replay/export.
They also exercise Advanced source-bound glossary receipts, rejection of a
changed decoded output even when its self-hash is recomputed, read-only Tasks
visibility, current-account checks at CLI admission and after gateway creation,
and the actual saved-source native handlers with widget stand-ins. These tests
make no Windows, live Google or linguistic-quality claim.


## Existing Advanced and native verification

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
