# CARR-10 — revision snapshot restore, candidate v1

REVIEW_REQUIRED. This is a continuation of PR #31, not a second backup writer. Base a450bd9eb97b11764f0fe2d70d72efbf9788aa5d.

## Reuse and boundary

- Reuses actual MIT cairnsearch hash_file from PR #17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3, path bootstrap/vendor/cairn_extractors/hasher.py, upstream https://github.com/smwaqas89/cairnsearch/tree/eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93 (src/cairnsearch/indexer/hasher.py). Upstream and local blob b872612f8470191b53af83e41af02f637cbaee69; upstream MIT LICENSE blob 7f60bd126b932ca626c4f19ae6d4086690f56825 fetched again.
- Reuses CARR-05 Storage.backup and Storage.check, #21 head 0657a5e0d4353b6473251b8d39d6153c6ad8156f, source blob d1ffd0829a6680d7af7a172186ce7db991de3dfe, and Python sqlite3 (SQLite 3.46.1). Native online backup API: https://www.sqlite.org/c3ref/backup_finish.html . No new SQLite parser, backup engine or index.
- Public alternatives surveyed: https://github.com/latheiere/statecrate advertises signed archives but POSIX support, not our Windows target; https://github.com/KanadeK/sqlite-backup-probe and https://github.com/KanadeK/sqlite-restore-drill are scratch verification tools rather than this application's restore publication/immutable revision contract. These candidates were not downloaded, installed, or adopted; no license or runtime claim beyond the verified adopted component above. Existing Storage.check supplies the application-specific validation already.
- CARR-05 owns backup creation/crash-safe publication; this lane owns only restore orchestration. No edits to storage.

## API

restore_revision_snapshot(source, destination, manifest, max_bytes=536870912) accepts a trusted closed standalone revision-storage snapshot and independently trusted manifest with exact keys version=1, kind="revision-storage", schema_version=1, size_bytes (integer), sha256 (lowercase hex). Caller supplies local source and new destination paths; no paths taken from manifest. No overwrite. Bounded copy into private staging, upstream hash verification, read-only exact schema comparison against a fresh Storage, then existing Storage.check and post-check digest. Final same-filesystem hard-link publication is atomic no-clobber. Unsupported filesystem fails rather than falling back to a partial copy. No network.

Use only in private stable parent directories with no concurrent writer to destination or sidecars. Manifest checksums are integrity, not authenticity. Not a hostile SQLite parser sandbox, not a complete data validation guarantee, no arbitrary Internet databases. No claim of power-loss directory durability or Windows behavior. Exact schema comparison deliberately rejects incompatible schema/SQLite schema text; no silent migration. No backup manifest writer in this package; caller must obtain manifest from trusted backup process. Catalog, original assets, research topics/evidence/notes, job journal, merge/restore of full workspace, manifest authentication, and Windows/NVDA remain unimplemented or untested.

## Executed evidence

Linux Python 3.13.5, SQLite 3.46.1. Exact dependencies reconstructed from GitHub text and verified with git hash-object against the blobs above. Command: PYTHONPATH=src:. python -m pytest -q tests/ingest/test_restore.py ; exit 0, 11 passed in 0.19s. Actual CARR-05 backup of synthetic SQLite data restored byte-for-byte; old verified Roman page label and Unicode retained; active revision and workspace filtering tested. Negative cases: wrong manifest versions/digest/size, limit, existing target, source/destination sidecars, wrong schema, raw-text tamper with updated manifest, race injected at publication (existing target preserved). No original private material used.
Implementation blob 7c752b0203eb3d85bf3dfdeb2dea9a8c76013f37; test blob 0e950bfd2c746d1bd6974bf026ac2c3decc00bbd. Both match remote readback. Earlier pipeline suite not rerun in this run; independent review and CI NOT_RUN. U5 remains open.
