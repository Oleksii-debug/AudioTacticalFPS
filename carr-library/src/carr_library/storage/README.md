# Versioned shared library storage

## Current application schema (V3)

`Storage(path, schema_version=3)` explicitly creates the shared catalog,
sealed-revision, workspace-profile, topic, evidence, saved-query and immutable
metadata-snapshot schema in one transaction. Existing V1/V2 formats retain
their exact signatures and can still be opened explicitly; opening one as V3
fails without changing it. No implicit live upgrade or inferred identity
mapping is performed. `Storage(path)` detects an existing V3 database.

`WorkspaceStore(db)` creates, renames, describes and archives research
workspaces. The application calls `require_active(id)` before scoped work.
`MetadataSnapshots(db, workspace)` records explicitly supplied attribution for
the exact sealed revision, independently of later mutable catalog edits.
Snapshots can be revoked, but their identity, payload and checksum cannot be
rewritten. They do not approve OCR text or printed labels. Strict TXT
attestation still checks the retained original bytes; PDF/OCR requires its
separate verification evidence.

All service operations use the caller's transaction and existing catalog,
topic, saved-evidence and query APIs. `check()` and `backup()` additionally
validate workspace profiles, topic reachability, evidence codecs/checksums,
exact sealed-page slices and revision-bound metadata. Revoked or archived
history is preserved in backups while runtime authorization prevents its use.
Database hashes are corruption detection, not protection against an attacker
who can rewrite the entire private database. Source-byte retention and complete
application bundle restore remain separate orchestration responsibilities.

The actual disk-backed V3 suite is `tests/storage/test_shared_v3.py`; it composes
the real services and TXT attestor, including reopen, rollback, isolation,
revocation, original-byte drift and checked backup. Legacy V1/V2, replacement,
recovery and schema-boundary suites remain applicable. Windows/NVDA acceptance
is separate from these storage tests.

## Earlier V1 candidate provenance

This is the storage-only adaptation of the SQLite external-content approach already used in PR #17, not another research UI or search engine. CARR-03 owns its MIT cairnsearch extraction integration; extracted pages enter this boundary through Page records. Catalog workspace/edition identifiers are supplied by the caller; CARR-02/01 must agree on their authoritative foreign keys before production wiring.

## Reuse and provenance
- Python standard sqlite3, SQLite FTS5, unicode61 and SQLite online backup API, no additional dependency. Official upstream https://github.com/sqlite/sqlite/tree/f3d536d37825302e31ed0eddd811c689f38f85a3 (version-3.46.1 tag resolved and license read via GitHub; public-domain disclaimer at LICENSE.md). The installed SQLite 3.46.1 reports sqlite_source_id() = 2024-08-13 09:16:08 c9c2ab54ba1f5f46360f1b4f35d849cd3f080e6fc2b6c60e91b16c63f69aalt1. This distribution build is not asserted byte-identical to upstream tag. No source vendoring or new download; direct network DNS unavailable. FTS5 native external-content triggers, integrity-check with rank=1, rebuild and backup API are exercised, not reimplemented. Reference https://sqlite.org/fts5.html.
- PR #17 reviewed: retains raw immutable page text and FTS5; this lane adds atomic active revision switch, sealed historical pages and checked backup. No files in PR #17 are modified. Do not maintain two production databases; integrate through coordinator.

## Boundary
Storage(path) opens a dedicated candidate database, rejects unknown versions and unversioned non-empty databases. Schema v1 creation and publication use BEGIN IMMEDIATE and rollback on any failure. SQLite default rollback journal is retained; WAL is not claimed or enabled. All pages of one revision must be supplied before publication. No deletion of sealed historical pages/revisions, including append; active_page is the only search-eligible projection. CARR-06 must bind workspace scope in every query and never derive citation offsets from token positions. Search ranking isolation is not established: global FTS corpus statistics may influence scores, even when rows are scoped. Raw pages are the citation authority; ordinal differs from physical page, which is nullable for logical text segments. Printed labels can be verified only for physical pages.
check() checks SQLite integrity, FK, raw page hashes, active content parity and FTS external-content consistency. rebuild() repairs the index from immutable active pages, never changes original text. backup() uses SQLite snapshot backup and refuses existing destinations; verifies copied database. Restore by opening a verified copy only; full workspace/catalog/asset manifest and crash-durable backup publication remain CARR-10 coordination work. This is not a security boundary against callers with direct database-file access.
No destructive down migration; future migrations must be versioned and backed up. Candidate does not yet implement catalog/asset registry, normalized passage spans, evidence foreign keys, resumable jobs, multi-process crash tests, workspace score isolation, Windows or complete integration.

## Reproduction
From carr-library: PYTHONPATH=src python -m pytest -q tests/storage/test_revisions.py (Linux); Python 3.13.5, SQLite 3.46.1. 5 passed in 0.11s, exit 0. Source Git blob d1ffd0829a6680d7af7a172186ce7db991de3dfe; test blob fb852dfba3f52e998e25d5f3c7b3dabcbe0e95a9.
One synthetic 10,000-page run, one workspace/edition, each page text "Synthetic page {i} historical river bridge archive", AMD EPYC 9V74 (5 visible CPUs), Linux x86_64: publication 0.1041s, check 0.0502s, rebuild 0.1238s, database 3,117,056 bytes. This is a single run, not a performance SLA. Windows and independent review NOT_RUN.
