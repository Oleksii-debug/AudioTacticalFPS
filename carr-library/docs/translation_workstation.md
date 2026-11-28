# Selected-source translation

Translation uses the existing Google Cloud Translation Basic v2 and Advanced v3 gateway, provider profiles, Windows credential vault, bounded authorization and durable translation jobs. The native window is available under **Tools → Translate text, chapters and documents**.

## Choose and review the input

The input control offers selected text, a chapter or reading-position range, one whole library document, and an explicit batch. A batch contains at most 16 chosen editions; one job is limited to 1000 source reading positions and 2,000,000 characters. Choose a literal title filter to narrow the first 100 listed editions. Nothing is uploaded by browsing or capturing a source.

Chapter ranges use actual source reading-order positions. They do not infer printed page labels. Each captured position retains its workspace, edition, active revision, original asset hash, locator, exact text hash and approval state. The two-newline separators inserted between positions/documents are derivative structure, separately identified by the source offsets. Captured library text is read-only in this window. An unchanged native TextCtrl display keeps the exact underlying CRLF/Unicode snapshot.

Preview reports selected documents, positions, unverified positions, characters and request count, together with the exact source-binding fingerprint and source/target languages. The optional price is the user's current contract rate; it is not a current provider quote. A required total reservation budget bounds this application's requests, not the provider's external billing system. Translate asks for explicit consent for the exact displayed provider, selected text/source identities, language pair, selected glossary and budget.

Google's supported-language list requires a separate explicit request. Basic v2 uses the exact official HTTPS endpoint, model nmt and a credential reference; it does not use a shared embedded API key. Advanced v3 retains separate source and target language capabilities for its explicit project, location and general/nmt model. Missing direction flags never imply support. Provider-declared language support is distinct from independently tested quality. Local translation models remain unavailable. Reviewed DOCX and EPUB structure preservation is described below; original visual page layout and per-word emphasis are not reconstructed.

## Reviewed DOCX and EPUB structure preservation

For an eligible retained original, choose **DOCX with reviewed structure preservation**
or **EPUB with reviewed structure preservation** in **Translation input scope**, then
**Choose library sources** and select exactly one whole edition. The existing
chapter/range and batch modes continue to produce linked text derivatives.
Capturing a formatted original does not contact a translation provider.

The capture verifies the original bytes, current source revision, reading positions
and an explicit mapping from the original text to document blocks. Preview shows
the supported profile and its limitations. The exact mapping, source, account,
language pair, glossary and approved budget are bound to the same existing job.
A changed source or changed visible settings requires a new review. **Translate /
resume** retains accepted chunks and the existing usage ledger; it does not start
a second document-specific translation service.

For DOCX, eligible body paragraphs and simple table cells retain their paragraph,
table and style definitions. A single link covering a whole paragraph is supported.
The translated text uses the first textual run's formatting, so original per-word
bold/italic placement and line/page breaks are not reconstructed. Headers, footers
and other unselected package parts keep their original bytes and are not translated.
The canonical DOCX reader's refusals remain in force, including ambiguous fields,
notes, drawings, tracked changes and unsupported table or link structures.

For EPUB, eligible text blocks follow the retained spine and retain block/table
structure, links, identifiers, styles and eligible media. Text uses the first
textual style and the canonical reader's whitespace normalization. Mixed links,
inline images/breaks, executable content, explicit hidden attributes, repeated
spine resources and synchronized media overlays are refused. CSS-dependent
visibility is not computed. Head titles, navigation outside the selected spine,
image alternatives and original metadata are not translated; the target language
is added to the package metadata. Unchanged ZIP member payloads remain byte-identical.

After completion, choose **Export DOCX / EPUB** and a **new** file with the matching
extension outside private application storage. Export uses the accepted saved
receipts, verifies the current original and mapping again, and validates the rebuilt
package before publishing. It makes no new provider request, never overwrites the
original or an existing destination, and does not create quotation authority.
A stop received after successful publication still reports the committed file.
Closing the window or changing the library revokes queued work and stale results;
pending native cleanup retains its private stage and resource reservation until
the owned processes are quiet.

The existing input limits still apply: at most 64 MiB for the original archive,
1000 selected reading positions and 2,000,000 captured characters. Local capture
and rebuild use the shared resource admission and cancellation path. Restoring
a backup preserves the optional mapping and accepted receipt history passively;
it does not activate an account or grant permission to send text. Current source
authority is still required for a new export.

CLI review uses the same optional mode:

    python -m carr_library translate --data-dir PRIVATE --workspace WORKSPACE preview --profile PROFILE --edition EDITION --source en --target uk --budget 1 --preserve-format docx

Use `epub` for an eligible EPUB. Review the returned `approval_sha256`, then
replace `preview` with `submit` and add `--allow-remote --approve-preview DIGEST`
with otherwise identical arguments. The mode accepts one whole edition, without
`--text-file`, `--first`, `--last` or an additional edition.

A completed format-bound job exports offline through the same CLI:

    python -m carr_library translate --data-dir PRIVATE --workspace WORKSPACE export-format --job JOB --output NEW_TRANSLATED.docx

The returned receipt identifies the original source/revision, mapping, accepted
chunk indices, changed ZIP members and exact new-file digest. Plain-text jobs
and their existing JSON, bilingual reading and byte-export routes remain readable.

The structural mapping and worker boundary adapt the existing implementation in
[PR #173](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/173), source
`26beb805aab1e9e202d134008441927523cdcb94`, through this application's canonical
readers, `translation/1` jobs, source permissions, provider approvals and retained
process ownership. New portable and native scenarios require receiving on their
final integrated head. This addition makes no live-provider, translation-quality,
visual-layout or NVDA qualification claim.

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


## Source identity, cancellation and saved jobs

Source capture reuses ReadingDesk and the existing narration source-anchor validator. Current permission, original bytes, active revision and source text are rechecked before each external translation chunk and before publication. The source-binding digest is also part of Authorization.task_id and therefore the independent gateway usage digest. A deterministic UUIDv5 additionally binds each source job to its exact source, approved account and original library directory identity before its first receipt exists. Removing the binding cannot downgrade that saved identity into a legacy pasted-text job. Identical text from another edition cannot replace the approved original by changing only local job metadata. An out-of-band copied or moved library retains readable derivatives but cannot inherit remote continuation authority.

Saved jobs restore their exact account, text, languages, glossary, budget and library binding. Resume/export reject a visibly changed selection. Missing or changed accounts block remote resume; completed derivatives remain available offline when their original library scope is still authorized. Existing accepted chunks are retained when paused. The last accepted chunk keeps the completed outcome after a late cancellation; later reading/export still rechecks current source permission. Source scope is checked at the gateway's existing cancellation points before credentials and transport. An uncertain charge is never retried automatically. Expired approvals must be reviewed explicitly; the app does not silently renew an external spending authorization.

The gateway records independent decoded translation/language digests. Every read, resume and export checks them against the accepted job receipts. Historical pasted-text pre-digest receipts remain explicitly labelled legacy-wire-only; the program never invents stronger evidence for them. New library-bound jobs require the independent decoded digest; removing it is not a legacy compatibility path.

A complete backup preserves source bindings, receipts and immutable original bytes. Restoration clears credential references and disables renewed remote execution. Historical profile digests are retained as read/export provenance only. An older restored job without that digest remains labelled restored-profile-unbound. Fresh paid execution requires a new reviewed submission.

## Bilingual reading and export

A completed job can open its original, translated or bilingual view in the existing voice reader. The source-loader protocol, editable reading copy, recovery, voice selection and playback controls are shared with ordinary reading. No second speech engine or player is created. Translation and narration never approve a quotation.

Single-source reading copies retain their original source anchor. A batch keeps its complete per-edition links in the durable job and export provenance; it does not pretend to be one original document. Translated text is a separate reading revision. Originals, saved quotations and research notes are never rewritten.

Export creates a new folder with:

- original.txt: the exact captured textual snapshot; original PDF/DOCX/EPUB binary assets stay in the library.
- translated.txt: the accepted translated text, retaining original chunk separators.
- bilingual.txt: paired original and translated chunks.
- translation.reading.json: the existing portable reading-project format.
- translation-provenance.json: source bindings, translation/usage evidence and output checksums.

Source-bound admission reserves the existing checkpoint/reading storage budgets before any paid call. Responses are bounded to 200,000 bytes; accepted translation plus reconstructed separators is limited to 2,000,000 characters. A response that crosses the total limit is retained with an attention outcome, and subsequent requests stop. Bilingual reading also remains within the shared reading-copy limit; choose a smaller range when combined original and translation exceed it.

Staging precedes fresh source validation. The existing library writer guard excludes cooperative archival during final publication, and both bundle and standalone reading exports recheck source permission at publication. Exclusive directory creation and create-only file publication preserve a concurrently created destination. The manifest is last; an interrupted publication keeps an INCOMPLETE marker and requires a new destination.

## CLI

The same contracts are exposed as translate. Global library/scope arguments come before the command.

Preview one chapter/range:

    python -m carr_library translate --data-dir PRIVATE --workspace WORKSPACE preview --profile PROFILE --edition EDITION --first 2 --last 4 --source auto --target uk --budget 1

For one whole document omit --first/--last. Repeat --edition for an explicit bounded batch. Use --text-file instead for an independent exact UTF-8 selected text snapshot. Neither preview nor capture sends text.

After reviewing the output, submit the same arguments with submit, --allow-remote and --approve-preview EXACT_APPROVAL_SHA256. The digest binds the current provider snapshot, text, source binding, language pair, selected glossary, entered rate and budget; changed input requires another preview.

inspect --job JOB prints a validated saved-job summary and resume_approval_sha256. resume requires --allow-remote and --approve-job with that exact digest and still enforces the existing approval and usage ledger. export --job JOB --output NEW_FOLDER works offline. reading --job JOB --view bilingual --output NEW_READING_FILE creates a linked reading project. languages --profile PROFILE --allow-remote explicitly discovers provider-declared support.

## Reuse and verification boundary

The implementation composes the reviewed [PR #161](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/161), source `7a9cc943acd48b6fd7bb43d03c30630ef3c67800`, decoded-receipt/usage binding and prior exact-request reconstruction with [PR #162](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/162), source `57cbd5d6c825f42b5769de9fa7ec6faf779622cc`, saved-visible selection checks. The implementation also adapts [PR #165](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/165), source `805d6cd8aeefa016e86b059cce4f691a63696a53`, deterministic source identity, original directory execution binding, storage/output budgets, live gateway source checks and completion-after-late-stop semantics. These mechanisms use the single batch-capable binding and existing translation/1 JobStore family here; its separate single-anchor schema and second menu are not installed. The existing shared HTTP transport, strict restore marker, single gateway, vault, job store and reader remain authoritative.

Portable tests cover actual SQLite originals, chapter/batch capture, preserved quotations, authorization/receipt tampering, stale scope, cancelled transfers, retained accepted chunks, source-bound CLI review, complete-backup restore and asynchronous native-handler composition. scripts/windows_translation_source_smoke.py exercises actual installed Windows controls and shared workers with synthetic provider replies. Local/native results must be tied to the final integrated source before release.

Synthetic protocol tests do not qualify live Google billing, translation or acoustic quality, glossary linguistic coverage, manual NVDA behavior or the full platform.


## Advanced integration and CLI compatibility

The Advanced adapter reuses the reviewed [PR #147](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/147) behavior at canonical source e69ba2403515f21509686d66f4bc8b3673756d81. It is composed into the existing retained-source implementation described above. It does not replace chapter/document/batch selection, bilingual views, source UUID/execution identity, live source permission checks, storage budgets, decoded-ledger verification or restore barriers.

The source-aware CLI accepts --glossary RESOURCE on Preview and Submit. That resource is part of the exact preview digest, approved wire request and saved-job resume review. profiles lists only nonsecret account identity. languages --profile PROFILE --cached reads the account's saved declarations without contacting a provider; --allow-remote remains necessary for a refresh. These two modes are exclusive. Existing --text-file, --edition, --approve-preview, --approve-job and --allow-remote contracts remain unchanged. The assistant.translation_cli import is a thin facade for this same implementation.

Advanced receipts retain the exact reported model and glossary configuration. Their decoded text is independently bound to the same existing usage ledger as Basic receipts. Offline reads and complete-backup restore preserve those fields; restored accounts remain disabled with credential references removed. An integer zero cannot masquerade as the glossary's false boolean flag.

New portable coverage is tests/assistant/test_translation_v3.py plus the existing native-handler/visible-selection suites. scripts/windows_translation_v3_smoke.py exercises actual Windows controls and a temporary OS-vault token with synthetic HTTP transport; it performs no live Google calls. Source invocation uses --source . --output RESULT.json, and installed invocation uses --expect-installed --output RESULT.json with Python -I -B. Test-controlled confirmation and file-picker answers are explicitly distinguished from real native key/focus, vault, ledger, derivative-byte and provenance checks. The final integrated source/native gate must qualify these additions before release.
