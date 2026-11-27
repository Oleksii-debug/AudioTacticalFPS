# Optional selected-text and library-source translation

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
The API key travels in X-goog-api-key, never the URL. TLS authority is fixed;
redirects are rejected. Provider responses are text, never executable commands.
Completed saved work remains exportable offline when its account is unavailable;
the selected text, languages and numeric budget must still match the saved job,
and library-source exports still require current source authorization.

The local reservation and optional estimate are **not provider-enforced billing
limits** or a live price quotation. Actual cost/quota remains the provider
account's contract; no current price is invented by this application.

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
A failed or cancelled export may leave a partial newly created directory.
Export success requires the final provenance manifest and matching output hashes;
the saved completed translation remains available for another explicit export.

## Supported limits and remaining qualification

Basic v2 NMT selected **plain text**, including the full bounded reading text of
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

Advanced v3 IAM/ADC, glossaries, automatic chapter-title interpretation, batch
document selection, formatted-document preservation, local M2M100 inference,
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

## Official API references

- https://docs.cloud.google.com/translate/docs/reference/rest/v2/translate
- https://docs.cloud.google.com/translate/docs/reference/rest/v2/languages
- https://docs.cloud.google.com/docs/authentication/api-keys-use
- https://docs.cloud.google.com/translate/docs/translate-text
