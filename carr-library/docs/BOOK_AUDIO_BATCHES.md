# Reviewed source books and ordered audio batches

This adapts PR152 `196a430f4a4139727021efb7236489a75293fbf2` through Resonance's current reader, managed Studio, local voice registry, production engine and complete backup. It does not install a second speech/Studio framework.

## Native workflow

Open **Audio → Review multiple books for audio**. Select a retained source in **Books and Collections** and use **Add selected library book**. Refresh local voices and explicitly choose a voice and language. Rate, pitch, volume and pan use the current typed Role bounds. The settings apply to the next captured review; existing rows remain immutable until explicitly replaced by a new review.

Each book has its own captured source range, split, voice, format and sample rate. The ten split modes reuse canonical semantics: whole document, retained EPUB chapters, physical pages, paragraphs, sentences, Unicode character count, blank lines, isolated regular expression, retained EPUB heading offsets, or explicitly selected current reading bookmarks. Logical text segments are never labelled physical pages. Heading and bookmark metadata are rechecked before execution. Regex uses the existing bounded pronunciation child, with its 1,000-character pattern bound and owned native process cleanup.

Move up/down sets exact execution order. Choose an external output parent for each book, or apply one parent to every row. **Preview exact batch** lists every part, exact source interval, voice/settings and output name. **Prepare managed parts** saves ordinary managed Studio projects and performs no synthesis. **Run or resume** is a separate action. It verifies existing production receipts and files before cache reuse. Completed parts are independently copied to the reviewed external parent; existing modified outputs are refused.

Pause and Cancel withdraw the current invocation and wait for current worker cleanup. The current renderer checkpoints remain the recovery mechanism. A later explicit Run checks them again. No persistent renderer pause controller, second job queue or audio-quality qualification is implied.

The limits are 32 books, 256 total parts and eight million selected characters; one book is bounded to two million characters. One split scans at most 10,000 boundaries. Names accept only `index`, `book` and `part` template fields, with optional one-to-four digit index formatting. Exact sanitized names, including deterministic managed IDs, appear before approval. Duration estimates are not text-split boundaries.

## Current data and authority

- Shared source capture: `reader/book_source.py`, using current `ReadingDesk` and exact retained originals. Book source text is always literal, including text that resembles Studio directives.
- Immutable plan: `state/audio/books/plans/<32hex>.json`, `carr-book-audio/2`. It binds exact source text, historical source anchor and metadata, selected boundaries, current Role and production settings.
- Ordered batch: `state/audio/books/batches/<32hex>.json`, the donor `carr-book-batch/1` shape with exact ordered plan IDs and SHA-256 values.
- Execution: ordinary `audio-projects/<deterministic32hex>/studio/project.json` and the current `audio/render-journal.json`, cache and `renders/<run_key>/complete.json`.
- Complete source and rendering lineage remain separate from quotation approval. None of these receipts authorizes an academic quotation.
- Output paths, approved native parent identities and review approval are transient. A reopened or restored batch requires explicit output selection and a fresh review. Machine-local profiles, executable paths and provider credentials are not imported from a book plan.

Current source authorization is rechecked during review, before production, before completed-cache reuse and at the final publication callback after file/directory flushing. A saved source-bound Studio part uses the same guard when opened directly in the existing Studio. Changed performance drafts are retained by complete backup, but they cannot masquerade as the originally reviewed book part; review a new plan or use an independent reading copy.

The source-level `source_guard` parameter added to the current renderer defaults to no additional guard for existing standalone callers. It does not change engine identities, synthesis cache keys, music model pins, plan SHA, DSP or completed-receipt format.

## Copying and recovery

The external copy verifies the current managed completion, exact plan/source hashes, expected inventory and every output hash. It streams independent copies through the existing publisher; output files are never writable hard links to the managed cache. A final callback rechecks the reviewed parent, owned stage and original source immediately before the atomic no-replace move. A post-move durability error retains and names the visible output. A later explicit review verifies matching existing receipts and retries directory synchronization; it never overwrites a different file.

Only current complete-library backup can preserve a source-bound book batch with its library originals and metadata. Standalone Studio ZIP backup explicitly refuses a book-part reference because it cannot carry the original library and all review records. Ordinary unrelated Studio project ZIP backups keep their existing behavior.

Complete backup includes only the typed immutable book plan/batch namespace, under the application writer and the shared native book metadata lock. Metadata is bounded to 4,096 files and 128 MiB total; individual plans to 32 MiB and batch selections to 128 KiB. Snapshot verification does not create locks or start engines. Historical sealed source rows, bounded raw page bytes, original metadata, batch membership and managed references are checked against the snapshotted database. Archived workspaces and old sealed revisions remain valid historical data; active permission is required again for Run. Runtime invocation locks and output approvals are excluded.

Canonical donor schema-1 plans belong to the previous Studio/Role contract and are not automatically activated as schema 2. Existing inert historical retention remains authoritative for those records. This implementation has not fabricated a compatibility claim for another executable producer.

## Acceptance status and gates

This source was prepared during executor unavailability. **New portable, installed and native results are NOT_RUN until their exact candidate CI executes.** Earlier donor passes are not evidence for this adaptation.

Required zero-skip suites:

- `tests/test_open_existing_library.py`
- `tests/audio/test_book_plans.py`
- `tests/audio/test_book_batch.py` — real configured FFmpeg with explicitly synthetic tone speech; no skip if the required runtime is absent.
- `tests/storage/test_book_audio_backup.py`
- `tests/ui/test_book_batch_dialog.py`

The native installed gate is `scripts/windows_book_batch_smoke.py --native`. It drives real named controls and current desktop worker lifecycle, exact order, Preview, Prepare, real FFmpeg render/reuse and completion-aware Close. Fixture books and PCM are public synthetic data; voice quality and manual NVDA remain unqualified.

Desktop composition hooks: import `install_book_batch_actions` from `ui.book_batch_dialog` and call it after the existing Audio menu is installed. The command ID is `audio.book-batch`. All added modules live in already packaged `audio`, `reader` and `ui`; there is no new runtime dependency or package list change.

## Offline accepted completion reuse

After complete-library restore, a completed managed part can be exported without loading a voice engine, model or FFmpeg. This is a read-only reuse of accepted audio: under the existing native project lock, the service requires a recorded complete journal, the exact stored typed plan, matching plan/source receipts and every current output checksum, plus current source authorization. The original absolute output path in the journal is never followed. Verification uses the existing resource broker and bounded file readers.

The result and native report explicitly count `retained_completion` reuse. This is distinct from a fresh synthesis or current engine qualification. Missing or incomplete parts still use the current renderer and configured local runtime. A legacy completion without its typed-plan receipt must pass that live renderer's existing cache/engine validation; the offline path never upgrades it. Changed completed bytes or a different stored plan fail closed. A restored batch still requires a new explicit output-parent review.
