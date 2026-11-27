# Selected-source translation

Translation uses the existing Google Cloud Translation Basic v2 gateway, provider profiles, Windows credential vault, bounded authorization and durable translation jobs. The native window is available under **Tools → Translate text, chapters and documents**.

## Choose and review the input

The input control offers selected text, a chapter or reading-position range, one whole library document, and an explicit batch. A batch contains at most 16 chosen editions; one job is limited to 1000 source reading positions and 2,000,000 characters. Choose a literal title filter to narrow the first 100 listed editions. Nothing is uploaded by browsing or capturing a source.

Chapter ranges use actual source reading-order positions. They do not infer printed page labels. Each captured position retains its workspace, edition, active revision, original asset hash, locator, exact text hash and approval state. The two-newline separators inserted between positions/documents are derivative structure, separately identified by the source offsets. Captured library text is read-only in this window. An unchanged native TextCtrl display keeps the exact underlying CRLF/Unicode snapshot.

Preview reports selected documents, positions, unverified positions, characters and request count, together with the exact source-binding fingerprint and source/target languages. The optional price is the user's current contract rate; it is not a current provider quote. A required total reservation budget bounds this application's requests, not the provider's external billing system. Translate asks for explicit consent for the exact displayed provider, selected text/source identities, language pair and budget.

Google's supported-language list requires a separate explicit request. Basic v2 uses the exact official HTTPS endpoint, model nmt and a credential reference; it does not use a shared embedded API key. Provider-declared language support is distinct from independently tested quality. Advanced v3 IAM/ADC, glossaries, local translators and preservation of original document layout are not implemented by this adapter.

## Source identity, cancellation and saved jobs

Source capture reuses ReadingDesk and the existing narration source-anchor validator. Current permission, original bytes, active revision and source text are rechecked before each external translation chunk and before publication. The source-binding digest is also part of Authorization.task_id and therefore the independent gateway usage digest. A deterministic UUIDv5 additionally binds each source job to its exact source, approved account and original library directory identity before its first receipt exists. Removing the binding cannot downgrade that saved identity into a legacy pasted-text job. Identical text from another edition cannot replace the approved original by changing only local job metadata. An out-of-band copied or moved library retains readable derivatives but cannot inherit remote continuation authority.

Saved jobs restore their exact account, text, languages, budget and library binding. Resume/export reject a visibly changed selection. Missing or changed accounts block remote resume; completed derivatives remain available offline when their original library scope is still authorized. Existing accepted chunks are retained when paused. The last accepted chunk keeps the completed outcome after a late cancellation; later reading/export still rechecks current source permission. Source scope is checked at the gateway's existing cancellation points before credentials and transport. An uncertain charge is never retried automatically. Expired approvals must be reviewed explicitly; the app does not silently renew an external spending authorization.

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

After reviewing the output, submit the same arguments with submit, --allow-remote and --approve-preview EXACT_APPROVAL_SHA256. The digest binds the current provider snapshot, text, source binding, language pair, entered rate and budget; changed input requires another preview.

inspect --job JOB prints a validated saved-job summary and resume_approval_sha256. resume requires --allow-remote and --approve-job with that exact digest and still enforces the existing approval and usage ledger. export --job JOB --output NEW_FOLDER works offline. reading --job JOB --view bilingual --output NEW_READING_FILE creates a linked reading project. languages --profile PROFILE --allow-remote explicitly discovers provider-declared support.

## Reuse and verification boundary

The implementation composes the reviewed [PR #161](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/161), source `7a9cc943acd48b6fd7bb43d03c30630ef3c67800`, decoded-receipt/usage binding and prior exact-request reconstruction with [PR #162](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/162), source `57cbd5d6c825f42b5769de9fa7ec6faf779622cc`, saved-visible selection checks. The implementation also adapts [PR #165](https://github.com/Oleksii-debug/AudioTacticalFPS/pull/165), source `805d6cd8aeefa016e86b059cce4f691a63696a53`, deterministic source identity, original directory execution binding, storage/output budgets, live gateway source checks and completion-after-late-stop semantics. These mechanisms use the single batch-capable binding and existing translation/1 JobStore family here; its separate single-anchor schema and second menu are not installed. The existing shared HTTP transport, strict restore marker, single gateway, vault, job store and reader remain authoritative.

Portable tests cover actual SQLite originals, chapter/batch capture, preserved quotations, authorization/receipt tampering, stale scope, cancelled transfers, retained accepted chunks, source-bound CLI review, complete-backup restore and asynchronous native-handler composition. scripts/windows_translation_source_smoke.py exercises actual installed Windows controls and shared workers with synthetic provider replies. Local/native results must be tied to the final integrated source before release.

Synthetic protocol tests do not qualify live Google billing or translation quality, acoustic language coverage, Advanced v3, manual NVDA behavior or the full platform.
