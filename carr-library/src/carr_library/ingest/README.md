# CARR-10 — resumable ingestion integration candidate

## Supervisor publication boundary — QA-011, 2026-11-27

The native import supervisor uses the existing request's result field for a
fresh attempt token and a bounded publication intent. An intent is written
before the catalog transaction and is never sufficient evidence of completion.
After a cancelled, timed-out or crashed child is reaped, the supervisor checks
the exact request/attempt, authorized edition/work and collection membership,
sealed active revision, extraction and page hashes, search rows, and retained
original bytes. A verified fresh commit returns complete; a rolled-back
transaction remains cancelled/failed. A cancelled replay cannot adopt an older
publication or add membership. No new journal, schema or parent-side parser is
introduced. The existing `published_here` correction is also retained.

Recovery has a five-second cooperative deadline, SQLite progress interruption,
and limits of 10,000 segments and 64 MiB of text/source bytes. If verification
cannot finish or identities differ, the result explicitly remains unverified
and resumable; committed source data is never deleted. Ordinary successful
imports retain the existing extraction limits. Interrupted request intents
remain compatible with the existing pending-state backup and restore producer.

`tests/e2e/test_import_commit_supervisor.py` runs actual isolated LibraryApp
imports with real PDF/DOCX parsing and SQLite publication barriers. It covers
cancel/timeout/crash after commit, precommit rollback, replay, tampering,
quarantine, recovery budgets and pre/postcommit backup restoration. The exact
canonical negative control at `33dcc42` failed all six fresh postcommit cases
and passed the three rollback/replay controls. The corrected local affected
selection passed 88 tests with no failures/skips on Linux Python 3.12.14.
Windows/installed/NVDA acceptance remains separate.

The sections below preserve historical prototype evidence at base
`a450bd9eb97b11764f0fe2d70d72efbf9788aa5d`; they are not current release status.

## Actual reuse, no duplicate production adapters
- PR #17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3: import original MIT cairnsearch hash_file at bootstrap/vendor/cairn_extractors/hasher.py; upstream https://github.com/smwaqas89/cairnsearch/tree/eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93. Verified remote upstream, vendored and executed local blob b872612f8470191b53af83e41af02f637cbaee69; LICENSE upstream blob 7f60bd126b932ca626c4f19ae6d4086690f56825. TXT extractor blob ed96b96beef0538567532fb5156c8b88f139268a inspected and verified, but not suitable for exact quotes (errors=replace and universal newline translation); strict UTF-8 decoding preserves CRLF and refuses invalid data.
- PR #20 head 4694b0d31cce3635b80d22ae7c13eb67b76ed7a1: catalog intake plus original MIT filetype.py matchers. No new MIME parser. Local blobs match remote intake 87d3bb0af3051058961b4674057f2724c0bf5e06, archive df7e8a66cf10b3b2b1adf4629b0f26f5aa7b8f1b, base 7c0c0d26d9344907eb6d5cba107dd927c8477d5d.
- PR #18 head c118f8e7ba848f8f3c2286d1d0df7f87be788e70: page OCR adapter 530ac42da7e255b2e0f5262f5bd5f6a8e36b203a; pypdf/PDFium/Tesseract path reused, no new OCR.
- PR #21 head 0657a5e0d4353b6473251b8d39d6153c6ad8156f: storage revisions d1ffd0829a6680d7af7a172186ce7db991de3dfe; native SQLite transactions and FTS5 reused, no new search index.
- External queue triage: https://huey.readthedocs.io/en/stable/index.html has SQLite task storage and retries, but no verified source-anchored page transaction contract; https://github.com/SammyRyuga/sqlite-workqueue documents at-least-once delivery and no cancellation. Neither copied, installed or adopted. Current serial journal has no third-party queue dependency. Consider mature queue only when multi-worker requirement warrants it.

## Use and behavior
Open a private local journal with Ingest(journal_path, storage) and call dry_run(root, relative), start(workspace, edition, root, relative), run(workspace, job, root, relative, max_steps=...), inspect(workspace, job), retry(workspace, job). Job identity is deterministic from workspace, edition, asset digest and extraction config. Journal stores private raw text/provenance: never commit or sync live journal to public/shared storage. Source snapshots are bounded and staged in a temporary directory; verify using upstream hash_file. All writers must share one job journal; no direct concurrent Storage.publish_revision calls. Sequential per-page checkpoints survive reopening; source/config mismatch rejects resume. Unverified OCR stays quarantined. TXT segments have physical=NULL. Storage commits all pages and index together; an interrupt after that commit is reconciled by comparing sealed revision/pages and integrity before marking the job complete. A stale job refuses to overwrite a newer active revision under single-journal ownership.

## Executed evidence
Local Linux Python 3.13.5, exact dependency blobs above, synthetic files only.
- PYTHONPATH=src:. python -m pytest -q tests/ingest: 9 PASS, 0.71s, 32 pre-existing catalog pathlib deprecation warnings.
- PYTHONPATH=src:. python -m pytest -q tests/ingest bootstrap/test_external_reuse.py bootstrap/test_universal_library.py: 13 PASS, 1.09s.
- Test source blob 11e68408f08dd08ad3cc961487354040155b10f5; implementation d932ee0c6982b05ddef9082b9410d3101b6eb501; local git hash-object matches uploaded blobs.
- Tested actual synthetic PDF native text and blank page, scoped FTS retrieval, Unicode/CRLF and logical segments, resume after reopening, dedup, cancellation, input replacement, simulated disk-full before publication, simulated KeyboardInterrupt after storage commit, simulated OCR review requirement, checkpoint corruption, stale job rejection and invalid paths. Failure injections are not a real power-cut/disk exhaustion test.

## Open blockers / handoff
Candidate depends on unmerged PRs #17/#18/#20/#21, not standalone on base. Integrator must assemble dependency heads and package vendor import in supported runtime. CARR-05 should expose a transactionally checked publish API: current integrity check runs immediately after atomic storage publication, not inside its commit. Cross-journal/direct writer races are unsupported. Parser isolation, external hostile inputs, hard resource ceilings across entire checkpoint journal, job cancellation during an individual OCR call, cleanup after OS kill, OCR review acceptance, persistent immutable original asset store, catalog membership/authorship registration, verified citation service integration (#26), CLI and non-destructive restore remain open. No Windows/NVDA, real corpus, real disk-full, or independently reviewed CI evidence. G2/U4/U5 not DONE. 
