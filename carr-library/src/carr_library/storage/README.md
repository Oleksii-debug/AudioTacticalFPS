# CARR-05 atomic revision storage (candidate)
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

## Import publication receipts (schema V5)

The earlier candidate notes above are historical. The current application keeps
the existing catalog, source revisions and import journal. Schema V5 adds one
immutable receipt in `library.sqlite`, committed in the same transaction as the
source revision. It binds that publication to its import request, execution
attempt and validated source fingerprint. Identical imports retain their
deterministic revision ID; a second request cannot make a cancelled first
request claim its commit. A sidecar candidate is only a hint and requires this
receipt plus fresh authorization and retained-source verification.

Exact V3 and V4 databases upgrade transactionally after schema and data checks.
The released V1–V4 layouts and fingerprints remain unchanged. Existing revisions
receive no invented receipts. Full backups include receipts in the normal
SQLite snapshot; old backups restore at their original version before the host
performs its checked upgrade. Restoring pending requests clears process-bound
attempts and candidate hints, while immutable historical receipts remain.
These records report completion only and grant no quotation approval.

## Current host: later import registrations (schema V6)

Schema V6 leaves every V1–V5 table, trigger and schema identity unchanged. It
adds `import_registration` to the same catalog transaction for a request that
registers an already sealed revision, including a later request adding collection
membership. Its immutable key is the pair `(request_id, attempt)`, so several
independent requests can register one revision without replacing the V5
`import_publication` receipt that owns the original revision publication.

Each registration stores the exact registration fingerprint and a `changed`
flag: `1` records a membership change committed by that attempt; `0` records
an observed no-op and does not claim ownership of an earlier commit. The worker
must still validate its own intent and current publication state before using a
receipt. Neither receipt grants source or quotation approval.

Exact V3, V4 and V5 catalogs upgrade in one checked transaction. The V5-to-V6
step only creates an empty table and its immutability triggers; it does not
infer registrations for old revisions or rewrite earlier publication receipts.
Invalid layouts or data reject before upgrade, and interruption rolls back the
whole uncommitted migration. Full backup and restore preserve these records in
the existing SQLite snapshot.

The host also checks complete receipt strings while streaming stored rows,
including V5 receipts whose frozen SQL constraints predate explicit NUL checks.
Malformed identity types or suffixes reject migration and backup without
normalizing or replacing historical records.
