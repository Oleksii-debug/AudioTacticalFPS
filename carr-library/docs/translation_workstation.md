# Optional selected-text, retained-document and batch translation

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
   Advanced v3 uses the separate setup below.
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
   Advanced v3 keeps source and target support flags separate. Optionally select
   an existing Advanced glossary; it requires an explicit source language.
   Enter the maximum total reservation and, optionally, your own current
   contract price per million characters for an estimate.
6. Preview the character/call count. Translate asks explicit permission for
   this exact selected text and binds its request hashes, destination, model,
   glossary, operation, budget and 24-hour authorization expiry. Changing the
   glossary, price estimate or other reviewed settings requires a fresh preview. Source authorization and the retained
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
OAuth bearer token and the explicit project for quota attribution. TLS authority is fixed;
redirects are rejected. Provider responses are text, never executable commands.
Completed saved work remains exportable offline when its account is unavailable;
the selected text, languages, glossary and numeric budget must still match the saved job,
and library-source exports still require current source authorization.
Language discovery also rechecks the account before Gateway creation and at
the existing authorization boundaries. Removal or replacement during consent,
queued work or credential lookup blocks that request without changing the cache.

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
`python -B -m carr_library translation --data-dir LIBRARY COMMAND`.
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
   the native window, with no overwrite. A library-source job created in the
   native window keeps its real library context for CLI resume and export.
   Completed exports do not need the account or its credentials, but retained
   source authorization still applies. Before remote discovery/resume and at
   each cancellation boundary, the CLI rechecks the current account against the
   exact saved snapshot; removed, disabled or replaced accounts cannot send text.

This is local bounded chunk scheduling of selected text, not Google's Cloud
Storage batch service. It does not upload a document directory or buckets.

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
Unknown or mixed job formats fail closed. An older application or an operation
dashboard that only understands `translation/1` needs explicit version-aware
integration before opening these new jobs; the native translation workspace is
the supported entry point.

Export rechecks the source after the folder picker and holds the existing
application writer guard while publishing the derived files. It rehashes the
retained original after acquiring that guard and before the completion manifest.
Archival uses the same guard. No database write transaction spans source hashing or network work.
Each translation export is staged and published as a new directory through the shared atomic no-replace publisher.
A pre-publication failure leaves no final directory. If the complete directory was already
published but final directory durability or staging cleanup fails, the typed receipt identifies
the published destination; inspect it before exporting again. Export success requires the
final provenance manifest and matching output hashes. A document batch may publish earlier
individual jobs before a later job stops; its partial-publication receipt lists those actual
directories and does not delete them or report that nothing was written.

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
interpretation, original rich-document layout preservation, local M2M100 inference,
canonical-135 translation mapping and independent linguistic quality review
remain separate work. No offline failure causes a silent remote fallback or
model download. Source text and receipts use the existing private local state
store; this translator does not itself encrypt the active library. The separately selected
sync/archive encryption workflow retains its own scope and acceptance requirements. API
secrets use the OS vault.

## Reuse and verification

This extension retains the canonical provider/reader architecture. It reuses the
approved-request/usage-ledger receipt checks, decoded-output validation and
plain-text join assembly from PR152 at
`112935aa15952682a885b10c322b3b4016c6cc80`.
It does not import that candidate's separate transport rewrite or the alternative
PR136 provider architecture.
The saved-selection matcher and validated job accessor reuse PR162 at
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
source/reading regression suites remain required. The Advanced tests exercise
explicit project/model/glossary requests, OAuth expiry/re-entry and directional
language support. Composition tests cover retained-source Advanced jobs,
read-only source/glossary UI scope, current-account checks at CLI/UI boundaries
and completed CLI/UI exports after account removal. Dialog doubles are not native
Windows or NVDA evidence. The installed native probes are
`scripts/windows_translation_v3_smoke.py` and the retained-source range checks
in `scripts/windows_v3_smoke.py`; their responses and permissions are synthetic.
They must run against the exact installed build before native acceptance is claimed.

Exact source, installed Windows and manual accessibility results belong to the
candidate's validation record. This document does not transfer earlier test
counts to a later source tree. No live Google request, user-document upload or
charge was made by this implementation work. Real provider billing and
translation quality require separate explicit execution.


## Explicit local translator

The optional `libretranslate-local` profile uses the existing Gateway,
authorization envelope, usage ledger and translation jobs. In **AI and API
connections**, select that adapter, enter an already installed service's literal
loopback URL (for example `http://127.0.0.1:5000`), an operator-declared model label
and the two translation capabilities, then explicitly enable it. The application
does not launch a server, install or download a model, probe a private pack,
forward a credential or fall back to Google.

From the selected-text window choose **Local translation...**. Approve fetching
the selected server's language declarations before previewing text. The adapter
requires the server's directed source/target pairs; a language appearing in the
list does not imply every direction is available. Automatic detection must
return a valid detected source that belongs to a declared direction.

Local jobs use an explicit **zero external cost** envelope and the same
at-most-one-attempt receipt rules. A local failed/uncertain attempt is not silently
replayed. HTTP redirects and proxy environment variables are not followed. The
client accepts only the reviewed JSON text protocol, not server commands or
formatted-file conversion. Existing completed work remains readable/exportable
without a server. An unfinished job rechecks its current selected profile and,
for retained documents, the original source authority.

The API has no attested model-file or runtime fingerprint. The profile's model
field is an operator label, not proof that a particular model produced a result.
The fetched pairs are provider declarations. Actual engine identity, model
licenses, language-pair quality, native offline egress and Windows performance
require separate evidence. No 135-language coverage is inferred.

Protocol source review used LibreTranslate **v1.10.0**, commit
`29aea44cc80100401497fb62db813a3f3a504e84`, specifically `GET /languages` and
`POST /translate` with `q/source/target/format=text`.
The implementation here contains client mapping only; it does not copy, bundle
or redistribute LibreTranslate/Argos engine code or model weights. The separately
installed server and its models retain their own licenses and obligations.
No actual translator/model inference was executed during this source-only draft.

## Explicit retained-document and paragraph batches

Use **Tools → Translate selected documents...**, or **Document batch...** in the
existing translation window. Add each selected book or range explicitly from the
currently visible Books and Collections page. `*` chooses that one entire
document; `2-5` chooses explicit source reading positions. All visible rows are
never interpreted as consent. The batch supports at most 64 selections and
2,000,000 total source characters. Duplicate selections and edited reading
copies are rejected; the existing ReadingDesk rechecks retained originals and
source authorization.

Choose the existing Google Basic/Advanced or explicit local profile, source/auto,
target, total budget and optional owner-entered current price. Advanced batches
may select an existing compatible glossary with an explicit source language.
Preview shows each exact source identity, reading-position count, character/call
count and its share of the total reservation. Provider billing and quota are
not enforced or guessed by this local reservation.

**Approve and save jobs** binds the current library path/directory identity,
source snapshots, profile, languages, glossary and total review hash. It saves
ordinary `translation/2` jobs; it starts no provider call. **Run / resume saved
jobs** executes only those saved IDs on the existing host background worker.
There is no second queue or batch database. Pause or a failed later job retains
earlier accepted jobs. **Choose saved document jobs** explicitly selects durable
jobs after reopening the window. Closing or changing the library/topic cancels
pending work and suppresses callbacks into an obsolete window.

Paragraph-preserving jobs split only explicit blank-line boundaries; they do not
guess language-specific sentences or document layout. Export includes the
existing original/translated/bilingual text and reversible reading-copy project,
plus `translation-structure.json` and escaped `bilingual.html`. The structure
file maps each ordered paragraph back to exact source character ranges, retained
reading positions and accepted receipt indexes. Original layout, tables, images,
footnotes and printed page fidelity are not reconstructed. All output is a
non-authoritative derivative; original citation/quotation offsets are not granted
to translated text. The reading project can be opened in the existing reader for
separately selected narration, retaining its original/translation lineage.

A partial preparation receipt lists already saved IDs without implying a provider
call occurred. A partial run or export receipt retains completed work and real
published directories. Inspect these IDs/destinations before preparing another
batch; the application never automatically reapproves or deletes them.
A moved/restored unfinished source job needs fresh source approval; completed
derivatives still export after current source verification.

### CLI additions

The same `translation` command accepts `--approve-local` for local language
discovery/submission. It is distinct from `--approve-remote`; the wrong approval
type fails before connection. Text `preview` and `submit` accept
`--preserve-paragraphs`.

For retained documents, supply an explicit UTF-8 JSON list such as:

```json
[
  {"workspace": "WORKSPACE_ID", "edition": "EDITION_ID", "first": 1, "last": 3},
  {"workspace": "WORKSPACE_ID", "edition": "ANOTHER_EDITION_ID"}
]
```

Omitting `last` selects the whole retained document from `first`; no path,
wildcard, script, hidden library scan or arbitrary operation is accepted.
Use `documents-preview --profile PROFILE --selections chosen.json --source en
--target uk --budget 0`, then repeat with `documents-submit --review-sha256 HASH`
and the matching explicit local/remote approval. Google selections use the
owner's actual chosen reservation, not the illustrative local zero.
`documents-run JOB_ID [JOB_ID ...]` runs those saved jobs;
`documents-export JOB_ID [JOB_ID ...] --parent EXISTING_DIRECTORY` creates new
child exports. Ctrl+C pauses the bounded worker. Structured/partial receipts are
printed as JSON with exit code 2; ordinary pre-publication errors remain code 1.

### Required evidence for this extension

All new composition is **NOT_RUN** until the exact source/installed candidate
gates execute. Required offline modules are
`tests/assistant/test_translation_local.py`,
`tests/assistant/test_translation_documents.py` and
`tests/ui/test_translation_documents.py`, alongside all prior Google/provider,
source/reader and backup suites. The document test reuses the adjacent local
transport test fixture; both files must be copied into an installed test run.

`tests/assistant/test_translation_local_loopback.py` uses real loopback HTTP and
real CLI children. It belongs only in the explicit loopback network gate, never
the common all-socket-denied runner. Its server is synthetic; it does not qualify
LibreTranslate model inference. The installed native probe
`scripts/windows_translation_documents_smoke.py` checks actual wx keyboard,
host background jobs, retained-source capture and structured output hashes.
It supplies synthetic transport and controlled consent/picker answers. Native
picker interaction, actual model inference/quality, native server egress and
NVDA remain separate NOT_RUN dimensions. Existing Google native probes remain
required without removal.

The final-observer correction reuses PR189 at
`7651df63e54f895e6fa3c279d423f1c112655fff`: save the complete job before notifying
the final UI observer, and never let an observer failure erase accepted output
or authorize a duplicate request.

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

Local protocol references:
- https://github.com/LibreTranslate/LibreTranslate/tree/29aea44cc80100401497fb62db813a3f3a504e84
- https://docs.libretranslate.com/api/
- https://github.com/LibreTranslate/LibreTranslate/blob/29aea44cc80100401497fb62db813a3f3a504e84/LICENSE
