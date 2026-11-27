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

## Existing library documents, chapters and source ranges

Select an imported book in **Books and Collections**, then open the translation
workspace and choose **Load library source**. Choose the entire bounded document,
one source reading position, an existing EPUB chapter, or a first/last position
range. Positions follow the existing Reading Desk order; PDF page ordinals and
unpaginated segments are not invented printed-page labels. EPUB chapter labels
come from its existing contents. The text remains an extracted-text derivative;
document layout, tables, images and styling are not translated.

Loading a source sends nothing. The workspace shows the exact source snapshot
read-only. Preview includes the workspace, edition, active revision, retained
original hash, source-position text hashes and original metadata alongside the
languages, provider, glossary and cost reservation. Explicit transfer approval
binds those source identities through the existing authorization task identity.
**Use pasted text** clears that source selection and starts a separate review.

A source-bound job reopens the existing authorized Reading Desk before submit,
resume, each HTTP transmission, acceptance of a provider response, and export.
An archived library/topic, changed revision, damaged retained original or
different source text stops the operation. Revocation before HTTP is recorded
as not sent; revocation during a response leaves the paid attempt uncertain,
without an accepted translation receipt or automatic retry. Reopening a newly
changed source requires a new explicit review.

Exports retain the canonical non-authorizing reading-copy source anchor,
original metadata and topic. They are staged privately and source-checked before
the existing exclusive directory publisher makes the files available. Original
retained documents, source revisions and quotation evidence are unchanged.
Canonical library backups already include the jobs, usage evidence and retained
sources: restoring to a different folder preserves the source IDs and allows
valid paused jobs to reuse accepted chunks. No absolute source pathname or new
backup format is added.

## Command-line workflow

The same service is available without wx through
`python -m carr_library.assistant.translation_cli --data-dir LIBRARY COMMAND`.
The library must already exist. No command accepts a secret on argv.

For a library source, replace `--input selected.txt` with
`--workspace WORKSPACE_ID --edition EDITION_ID`. That selects the entire document
within the current two-million-character limit. Add `--chapter 2` for one existing
EPUB chapter, or `--first 2 --last 4` for source reading positions. `--topic TOPIC_ID`
retains an explicit existing topic scope. Repeat all selectors unchanged for
`submit`; its review hash includes the exact source revision and metadata.

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
   the shared transport interrupts active reads within its whole-call budget.
   An uncertain charge is not automatically retried. Paused exit status is 2,
   errors are 1.
6. `jobs` lists saved statuses. `export JOB_ID --output NEW_DIRECTORY` publishes
   the same original/translated/bilingual/reading-copy files and provenance as
   the native window, with no overwrite.

This is local bounded chunk scheduling of selected text, not Google's Cloud
Storage batch service. It does not upload a document directory or buckets.

## Multiple documents with separate reviews and reservations

The translation workspace's **Add current source to batch** captures the current
exact text or loaded library source, account, languages, glossary and reservation.
Load the next source and repeat. **Document batches** opens an ordered list.
Move up/down or remove rows before **Review entire translation batch**. Each
document keeps its own settings; later edits to the single-document fields do not
change a collected row. Review displays each original identity, source revision,
character/call count, endpoint, model, glossary and budget, plus aggregate totals.

**Prepare approved batch** asks explicitly for the exact displayed order and
transfer reservation. It saves normal translation child jobs and sends no text.
**Run or resume batch** is a separate explicit action. A pause, stale source or
uncertain charge stops advancement to the next document. Accepted chunks and
complete children are verified and reused. Saved batches never mint fresh
permission or renew expired approvals. Interrupted local preparation can fill
missing child journals from the retained immutable templates without changing
their identities or envelopes.

A batch contains 1–32 documents and at most two million selected characters in
total. Source-bound entries remain in one existing workspace; the native window
also retains its selected topic. Entire documents, chapters and position ranges
use the same bounded Reading Desk source checks as individual jobs. Plain-text
files remain unbound selected text. Duplicate source/settings are refused;
export the accepted job again when another identical copy is needed.

**Export completed batch** requires every child to be verified complete and a
new explicitly selected directory. It writes numbered per-document subfolders
with the existing original/translated/bilingual/reading-copy files, plus
`batch-provenance.json` binding the child IDs and provenance hashes. Source
guards run again before publication. Backups use the same typed translation
producer and retain exact parent/child approvals, usage evidence and source
identities. Restore starts no request and restores no output-directory approval;
choose a new destination explicitly.

### Headless document batches

Use `python -m carr_library translate --data-dir LIBRARY COMMAND`, or the
translation module command above. Create a UTF-8 JSON description of at most
128 KiB with only explicitly selected input paths or library editions:

```json
{
  "schema": "carr-translation-batch-input/1",
  "title": "Two reviewed documents",
  "items": [
    {
      "profile": "ACCOUNT_ID",
      "workspace": "WORKSPACE_ID",
      "edition": "EDITION_ID",
      "source": "en",
      "target": "uk",
      "budget": "1"
    },
    {
      "profile": "ACCOUNT_ID",
      "input": "second-selected.txt",
      "source": "auto",
      "target": "uk",
      "budget": "1"
    }
  ]
}
```

Replace the placeholders with your existing IDs and your explicitly chosen
reservations. The numbers are examples, not prices. File paths may be absolute
or relative to the description's directory; no directory scanning or implicit
document upload is performed. Library items also accept `topic`, `chapter`, or
`first`/`last`; each item may include `title`, `glossary` and `rate_per_million`.

1. `batch-preview --batch-file selection.json` resolves the explicit inputs,
   rechecks sources and prints the exact aggregate `review_sha256`. No request.
2. `batch-submit --batch-file selection.json --approve-remote --review-sha256 HASH`
   rereads those inputs and rejects any changed text, source, settings or order.
   It saves the existing child approvals without transmitting text.
3. `batch-review BATCH_ID` displays the retained approval and current child
   checkpoints. `batch-jobs` lists batches. `batch-prepare BATCH_ID` finishes an
   interrupted local preparation; these commands do not send selected text.
4. `batch-resume BATCH_ID` uses only those original child approvals. Ctrl+C
   requests cancellation through the existing whole-request transport. A paused,
   partial or attention result exits 2; validation/permission errors exit 1.
5. `batch-export BATCH_ID --output NEW_DIRECTORY` publishes all completed
   derivatives into the explicitly selected new directory without overwrite.

Neither the JSON description nor a batch manifest is a second execution queue.
The ordinary child jobs and their usage ledgers remain the sole authority for
accepted translations and attempted provider calls. This bounded local batch
does not use Google's Cloud Storage batch API.

## Supported boundary and remaining qualification

Basic v2 and Advanced v3 NMT selected **plain text** are implemented, with Unicode-preserving
source storage and chunks of at most 4,500 characters. The provider's language
list establishes declared capability, not translation quality or canonical-135
coverage. Unknown/absent languages are rejected; similar names are never mapped.

Automatic ADC discovery/token refresh, glossary creation, formatted-document
preservation, Cloud Storage batch jobs,
local M2M100 inference, canonical-135 translation mapping,
and independent linguistic quality review remain open. No offline failure causes
a silent remote fallback or model download. Selected source text and receipts use
the existing private local state store; this feature does not supply encrypted
text-at-rest containers. API secrets use the OS vault.

Exports are staged privately and require final provenance and matching output
hashes before exclusive publication. Original library quotations remain attached
to their source, not the translated revision.

## Validation

The existing provider and translation suites exercise the canonical transport,
ledger, jobs and reading-copy exports. They include verified loopback TLS,
whole-call deadline/cancellation, destination pinning, API-key redaction,
exact consent, malformed inputs, receipt integrity, source preservation and
resume without repeat billing. Advanced v3 uses those same transport and
publication checks.

`tests/assistant/test_translation_source.py` uses actual library imports, EPUB
spine order, metadata revisions, source authorization and canonical backup/restore.
It checks source changes before HTTP and during replies, export revocation,
source-identity tampering, saved-chunk reuse after restore, and CLI/native-handler
review. Provider replies and native-handler widgets are synthetic.

The Advanced source suite `tests/assistant/test_translation_v3.py` exercises the
real shared gateway, ledger, jobs, derivative exports, CLI and actual dialog
handlers with synthetic gateway-transport responses and widget/scheduler stand-ins. It
checks exact project/model/glossary consent, directional language capability,
expiry and explicit re-entry without duplicate accepted calls, stale CLI/UI
reviews, disabled controls during queued work and close/cancellation.

The batch suites `tests/assistant/test_translation_batch.py`,
`test_translation_batch_cli.py` and `test_translation_batch_ui.py` cover actual
library/jobs/ledger/restore boundaries and real worker threads with synthetic
provider replies and widget stand-ins. Their new source requires execution on the
exact integrated build; source review alone is not a passing test result.

The required installed Windows probe is
`python -I -B scripts/windows_translation_v3_smoke.py`. It checks real wx native
Space/focus, asynchronous completion, temporary Windows Credential Manager
storage, ordered multi-document review/preparation/resume, and exact output bytes.
Its REST responses and confirmation/file-picker
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
