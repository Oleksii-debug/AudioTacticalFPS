# Optional selected-text and library-source translation

Tools → Translate selected text and Tools → Translate selected book or source range
use the existing provider profiles, Windows Credential Manager, permission
envelope, usage ledger, JobStore and reversible ReadingDocument. No model or
provider SDK is installed automatically.

## Use

1. In Tools → AI and API connections, create a separate account and choose
   Basic `google-translation-v2` or Advanced `google-translation-v3`. Both use
   `translation.text,translation.languages`. Basic uses the official endpoint
   `https://translation.googleapis.com/language/translate/v2`, model `nmt` and
   your API key. Advanced uses an explicit numeric project and location at
   `https://translation.googleapis.com/v3/projects/PROJECT_NUMBER/locations/LOCATION`,
   model `general/nmt`, and a manually supplied IAM/ADC OAuth access token with
   its actual future expiry including UTC offset. Store the credential and
   explicitly enable the account. Saving a profile makes no request.
2. Choose Translate selected text to paste text, or select a book in the library
   and choose Translate selected book or source range. Enter one source reading
   position or a range such as `2-5`. The initial `1-last` range selects the full
   document. These are the reader's ordered source positions; they are not
   inferred chapter titles or printed page labels.
3. The book selection loads locally through the existing authorized reader.
   Its original text is read-only in this translation window. The source summary
   shows the title, position range, character count and limits. Unreviewed
   extracted text remains labelled as unreviewed. Loading sends no text.
4. Choose the Google account and edition, then explicitly approve Fetch languages
   if its capability list has not been fetched. Selected text is not sent by
   language discovery. The returned list is tied to the exact account, endpoint
   and model. Advanced source and target direction flags are shown and checked;
   a target-only language cannot be silently used as a source.
5. Choose a source code or `auto` and a target code from the fetched list.
   For Advanced, optionally enter an existing glossary resource in the same
   numeric project and non-global location. Glossaries require an explicit
   source language and use case-sensitive matching. Leave this field blank for
   Basic. Enter the maximum total reservation and, optionally, your own current
   contract price per million characters for an estimate.
6. Preview the character/call count. Translate asks explicit permission for
   this exact selected text and binds its request hashes, account edition,
   destination, model, glossary, operation, budget and 24-hour authorization expiry. Source authorization and the retained
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
Basic sends its API key in X-goog-api-key, never the URL. Advanced sends the
selected OAuth token in Authorization: Bearer and the explicit numeric billing
project in X-goog-user-project. TLS authority is fixed; redirects are rejected. Provider responses are text, never executable commands.
Completed saved work remains exportable offline when its account is unavailable;
the selected text, languages, glossary and numeric budget must still match the saved job,
and library-source exports still require current source authorization.

The local reservation and optional estimate are **not provider-enforced billing
limits** or a live price quotation. Actual cost/quota remains the provider
account's contract; no current price is invented by this application.

## Bilingual narration through the existing Audio Studio

For a **completed library-source translation**, choose **Bilingual audio project**
in the translation window. This rechecks the saved source, accepted decoded-text
receipts and current source authorization, then inspects the existing installed
voice registry. It makes no provider request and starts no speech.

Both voice lists open with **no voice selected**. Explicitly choose one voice for
the original and one for the translation. Each list shows the voice name, engine,
declared language and licence. Matching uses the complete declared language tag:
`en` does not silently match `en-US`, and regional or script variants are not
discarded. If a matching voice is unavailable, the dialog explains that the pair
is unsupported. Automatic source-language detection, mixed-language assignment,
pasted-text jobs and unfinished jobs are outside this narration workflow. They
retain their existing text-export behavior where supported.

The new export directory contains the original five translation files plus
`bilingual.studio.json`. Open that JSON with Audio Studio's existing **Open JSON**
action, review the roles, then explicitly **Render**. This uses the ordinary
Studio project, queue, renderer and output receipts. There is no separate narrator,
translator, voice catalog or job store. Exporting the project alone does not create
a render job or reserve a heavy-runtime slot.

Each accepted translation request chunk becomes one scene: literal original text
first, then its accepted translated text. Long utterances are split without dropping
characters. Existing Studio directives such as `[Music: ...]`, `[Pause: ...]` or
speaker-looking prefixes in the document remain literal words; they cannot create
music, pauses or extra roles. Chunk pairs are **not sentence, printed-page or
chapter alignment**. Original reading text, source positions and undo history stay
in the existing reading-copy export. The Studio script is explicitly a new derived
presentation; its `source_ref` strings are passive job/chunk/hash provenance,
without quotation or live source authority.

The provenance manifest includes the Studio file hash, project fingerprint and
the two selected voice declarations/runtime fingerprints. The service rereads
voice settings and selected runtime identities before creating the output and
before its completion manifest. A removed or changed voice, changed source,
revoked source access or cancellation cannot publish a successful export. A
partial new directory can remain, but it has no completion manifest. The accepted
translation job and provider receipts remain unchanged. At a later explicit Studio
render, the current Studio voice/runtime checks apply; a detached project does
not grant continuing access to the library or freeze an external voice installation.

The combined source and translated text must fit the ordinary 16 MiB Studio JSON
import limit as well as existing translation limits. The service checks the fully
serialized project before creating an export folder. A completed translation that
is too large for narration remains available for its normal text export; choose a
smaller source-position range for a new narration translation.

Programmatic callers use the same `TranslationService.narration_choices(job_id)`
and `TranslationService.export(..., narration_voices=(original_record,
translation_record))` methods as the native window. Supply the exact two explicitly
chosen records returned by inspection; the service revalidates them. There is no
new translation CLI or automatic choice. Adapter language declarations and local
synthetic tests do not establish linguistic or pronunciation quality.

`tests/assistant/test_translation_narration.py` exercises actual completed jobs,
source authorization, receipt tampering, changed voice assets/settings, literal
text, output bounds and the existing Studio load/save/revision/backup paths.
Its voice provider is a declared test fixture that cannot synthesize. The existing
native Windows translation probe additionally opens real unselected voice controls,
exports an actual source-bound project through the window, and destroys a chooser's
parent directly. Its voice declarations and HTTPS responses are synthetic; acoustic
quality, real Google billing and manual NVDA are separate qualification.

## Advanced OAuth and glossary boundaries

The account editor accepts a short-lived token value and its explicit expiry,
then stores a typed endpoint-bound record in Windows Credential Manager. The
token must be unexpired and no more than twelve hours from the time of
entry. The application never searches credential files, reads ADC automatically,
accepts a service-account JSON key, or refreshes a token in the background.

Renew a token explicitly in the same unchanged account. The existing vault
reference and complete non-secret profile remain stable, so a paused job can
resume under its original still-valid approval without repeating accepted
chunks. Changing the project, region, model or other profile fields requires
review; an old saved job cannot inherit the changed account. Expiry or an
invalid record stops transmission before an Authorization header is sent.
A provider refusal after submission remains an uncertain charge and is never
automatically retried.

Advanced requests select the full NMT model resource. Successful receipts must
return that exact normalized numeric-project model; with a glossary, the
glossary result and its exact resource/case configuration must also match.
Ordinary translations cannot substitute for a missing glossary result.
Advanced text/plain responses retain literal entities such as `&amp;`; Basic
HTML entity decoding remains separate. Both adapters bind accepted decoded
text to the same usage ledger.

Experimental Advanced checkpoints from other branches that lack this
decoded-output evidence do not acquire provider authority on import. They
remain stored, but validated resume/export rejects missing proof; a new remote
job needs explicit preview and consent. Existing supported Basic checkpoints
keep the legacy readability behavior described below.

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
receipt. Source jobs and all Advanced jobs require this decoded-output proof. Older Basic
pasted-text receipts that never recorded it remain exportable with `decoded_text_binding: unrecorded-legacy`
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
A failed or cancelled export may leave a partial newly created directory.
Export success requires the final provenance manifest and matching output hashes;
the saved completed translation remains available for another explicit export.

## Supported limits and remaining qualification

Basic v2 and Advanced v3 NMT selected **plain text**, including the full bounded
reading text of an imported document or an explicit source-position range, are
implemented through the same service, consent, journal and derivative path.
The source and translated reading copy each support at most 2,000,000 characters.
Requests contain at most 4,500 characters. Library-source and Advanced jobs bound
each provider response to 200,000 bytes and stop further requests if accepted
translated text or its reconstructed source separators exceed the reading-copy budget.
Before a new library-source or Advanced job is saved, a conservative JSON-size preflight reserves the
existing 64 MiB checkpoint and 32 MiB reading-project limits, including escaped
source text, a single translation edit and accepted receipts. Already accepted chunks remain
stored; an over-limit job is not reported complete.

The provider's language list establishes declared capability, not translation
quality or canonical-135 coverage. Unknown/absent languages are rejected;
similar names are never mapped. Export restores source whitespace at request
joins without altering the saved provider receipts. This preserves tested
plain-text joins; it is not a claim of page layout, table, footnote or formatted
document fidelity.

Automatic IAM/ADC discovery or refresh, custom/LLM translation models, glossary
creation, document-batch APIs, automatic chapter-title interpretation, batch
source selection, formatted-document preservation, local M2M100 inference,
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
The saved-selection matcher and validated job accessor reuse PR162 at
`57cbd5d6c825f42b5769de9fa7ec6faf779622cc`. The native callback additionally
rereads the existing profile catalog in its worker before creating the gateway
and at the existing remote-operation authorization checks.

The Advanced adapter reuses the reviewed account and REST contracts from PR147
at `3671e9b1bbbfc566b2fb5ea18fcda834ad6bb36e`. It does not replace the canonical
source-bound service with that branch's legacy translation/1 implementation or
add a parallel credential, usage or job store. The native workflow is the supported
Advanced entry point; this increment does not add a translation CLI.

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

`tests/assistant/test_translation_v3.py` adds synthetic OAuth/REST/model/glossary
validation, directional-language handling, same-profile token renewal, accepted
chunk reuse, decoded-receipt tamper rejection, full/range source lineage,
mid-operation source revocation and same-path restore. It also exercises the
actual native callback functions with widget stand-ins, including stale account
and changed saved glossary checks. `windows_translation_v3_smoke.py` separately
uses real Windows wx controls, native focus/Space actions, a temporary synthetic
Credential Manager record and synthetic HTTPS responses. It binds its installed
module bytes to the exact checkout and runs inside the existing egress-blocked
acceptance steps. Native approval/file-picker usability and manual NVDA remain
separate from that automated probe. New exact-head runtime results must be read
from CI; source preparation and static review are not execution results.

Exact source, installed Windows and manual accessibility results belong to the
candidate's validation record. This document does not transfer earlier test
counts to a later source tree. No live Google request, user-document upload or
charge was made by this implementation work. Real provider billing and
translation quality require separate explicit execution.

## Official API references

- https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate
- https://docs.cloud.google.com/translate/docs/reference/rest/v2/languages
- https://docs.cloud.google.com/docs/authentication/api-keys-use
- https://docs.cloud.google.com/translate/docs/translate-text

- https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/translateText
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/TranslateTextResponse
- https://docs.cloud.google.com/translate/docs/reference/rest/v3/projects.locations/getSupportedLanguages
- https://docs.cloud.google.com/translate/docs/advanced/translate-text-advance
- https://docs.cloud.google.com/translate/docs/authentication
