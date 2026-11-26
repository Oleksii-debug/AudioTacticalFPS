# Guarded revision snapshot integration

Existing components suffice for this subset: acquire exclusive_writer(root) before opening writable Storage; create_revision_bundle(storage, destination, include_all_workspaces=True) under the guard; close Storage before releasing the guard. For restore, acquire the same guard on the stable private destination root, call restore_revision_bundle with independently retained manifest SHA256 and a new destination, then open the restored Storage and check it. No replacement of existing databases. All cooperating writers must use the same root. Do not use network or hostile filesystem paths. The bundle covers revision storage only, not catalog, topics, journal, original assets, or user preferences; do not represent it as a full library backup.

## Reproduction, 2026-11-26

Pinned assembly (copy these exact files into scratch tree preserving paths relative to carr-library/; verify Git blob hashes):
- PR31 head 04ffaf99b2eff886e4ad7a57afcbc65ef677f563: src/carr_library/ingest/writer_guard.py 2445a40f97cdb45bd590e64da415a18fae31ce54; bundle.py d664107cebe9704c568220a6752c159e95c70f60; restore.py 7c752b0203eb3d85bf3dfdeb2dea9a8c76013f37.
- PR21 head 8a7ed6bbdc84a4c626af2f7a9d54dfe3431a8d0f: src/carr_library/storage/revisions.py 32e66ef36e3c565b2696dfb82c05e3af465b2afc.
- PR17 head 4c2ee5ba43a778a2b160325cfe48678eeab213d3: bootstrap/vendor/cairn_extractors/hasher.py b872612f8470191b53af83e41af02f637cbaee69, matches upstream MIT https://github.com/smwaqas89/cairnsearch/tree/eb5bf13605343e75e3aaf6ce0d8d2ecef8748d93. License blob 7f60bd126b932ca626c4f19ae6d4086690f56825.
- Installed filelock 3.29.0, MIT, 13/13 upstream Python source blobs match installed runtime at https://github.com/tox-dev/filelock/tree/469b47f192b0a9f8c8b795d9b9f57212c716959b (additional generated version.py in installed wheel). No new lock implementation.
- tests/ingest/test_guarded_roundtrip.py d8b190ee49e042a65f9cfb5258af2a3bf8ba9ae3.

Linux Python 3.13.5, SQLite 3.46.1. From scratch root: PYTHONPATH=src:. python -m pytest -q tests/ingest/test_guarded_roundtrip.py -> 3 passed in 3.10s, exit 0. Actual SQLite WAL, FTS5, subprocess locking and upstream hash_file; no test doubles. Historical page digest and printed label, unknown printed label, workspace isolation, independent post-backup changes, rejected existing restore target, explicit scope consent and release of lock on exceptions tested.

This is owner-side integration evidence, not independent acceptance. Full Ingest (catalog/extractor/job journal), cross-store backup consistency, transaction CAS, Windows/NVDA and CI NOT_RUN. No production code changed in this checkpoint. Next: agree a single workspace snapshot contract with CARR-05/08, then exercise actual Ingest pipeline under the same guard. No further parallel backup engine.
