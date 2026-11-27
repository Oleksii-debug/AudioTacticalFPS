# Canonical general test disposition — 2026-11-27

This bounded source review compares canonical `e5c64a5245583f8f8b404a0f042f6ca438c24d7f` with integrated `078dde33e65767e14b3bd50de74974144236e820`. It covers five test paths left outside the separate assistant/provider/progress and speech/ASR ledgers. No old test PASS or parameterized case total is transferred.

## extract/test_language_manager.py

Discovery and selection use the single ocr_languages catalogue and the current native selector. Explicit script IDs, auxiliary-model exclusion, safe selection, offline pack hashes, exclusive publication and cancellation remain covered. The old LanguageModel/installed_languages helper API is replaced; an unchanged API alias is not claimed.

Current reviewed test files:

- `carr-library/tests/extract/test_ocr_languages.py` — blob `42afbd950d5ba03fd5807c86f857e697a725bc51`; full source selector true, installed selector true.
- `carr-library/tests/extract/test_ocr_packs.py` — blob `b4264d87cefc74f8ef06ee78814157b01ba622c7`; full source selector true, installed selector true.
- `carr-library/tests/extract/test_ocr_model_store.py` — blob `d1db430a5798cf869feafb8fc517e01367f56424`; full source selector true, installed selector true.

## extract/test_model_pack_manifest_identity.py

Four canonical integrity cases are explicitly restored in 0203c825: replacement of real manifest/model bytes after the approved snapshot, duplicate JSON, non-finite JSON and an oversized manifest presented with the empty digest. Existing current pack tests retain exact valid language/script bytes, wrong checksum and changed-model refusal. The current installer refuses changed source snapshots; it does not promise the older test's successful installation from a concurrently changed source directory.

Current reviewed test files:

- `carr-library/tests/extract/test_model_pack_manifest_identity.py` — blob `8b0e6495fd066ce23262365753d7ac529cd9b376`; full source selector true, installed selector true.
- `carr-library/tests/extract/test_ocr_packs.py` — blob `b4264d87cefc74f8ef06ee78814157b01ba622c7`; full source selector true, installed selector true.

## operations/test_worker_start_cleanup.py

The active process owner tests both actual stdout/stderr thread-start failures, closes the exact owned child and pipes, and tests delayed cleanup without early ownership release. Recording service tests independently retain shared media/resource reservations through completion and pending cleanup. The retired audio.process module is not an import alias; no one-to-one parametrized test equivalence is claimed.

Current reviewed test files:

- `carr-library/tests/audio/test_recording_process.py` — blob `f0b5711ea14db77cd066d78afa2a77e2319f426b`; full source selector true, installed selector true.
- `carr-library/tests/audio/test_recording.py` — blob `375720b56cd235b20060c1473690c782f30c247f`; full source selector true, installed selector true.

## ui/test_export_confirmation.py

The current shared approval captures the selected destination before the owned native question, defaults to No, accepts only explicit affirmative consent, retains foreign changes, rejects private destinations and destroys its own modal. Text/binary publisher and real saved-research handler tests preserve immutable originals, no-clobber and post-commit completion semantics. The current helper is approve_export_destination; the old confirm_export_destination spelling is not exported.

Current reviewed test files:

- `carr-library/tests/ui/test_export_destination_approval.py` — blob `0e047df3557f0374797b97bdde843f865923a1ed`; full source selector true, installed selector true.
- `carr-library/tests/e2e/test_export_destination.py` — blob `8e4b1cb7d914d491d440dee5f3340439f13dc1c6`; full source selector true, installed selector false.
- `carr-library/tests/reader/test_binary_file_export.py` — blob `86007a1658fe98c6a9161be4a3e092fce38ba73d`; full source selector true, installed selector false.

## e2e/test_transcription_cli.py

Current CLI tests execute the actual owned synthetic protocol process, preserve retained originals, verify required receipts/exports and separate edits, reject zero-exit-only success, reject ambiguous scope before mutation and require explicit runtime selection before Start. Canonical history is retained passively and imported only through the explicit current history workflow. The top-level command is transcribe with explicit workspace/profile selection; old transcription/register-runtime/submit spellings and old receipt schemas are not treated as aliases.

Current reviewed test files:

- `carr-library/tests/transcription/test_profile_cli.py` — blob `632120dea2a81a137a36b0a80fb1a462ace24bb5`; full source selector true, installed selector true.
- `carr-library/tests/transcription/test_supplied_worker_bridge.py` — blob `c2536d290c531bbddaad9766d445f1869f97fbfa`; full source selector true, installed selector true.

## Qualification boundary

The four restored OCR cases require execution on the combined source. The referenced actual-child tests use explicit synthetic protocol workers. They do not establish acoustic inference, physical device behavior, translation quality, NVDA usability or distribution rights. The JSON companion records the exact old/new blobs and function-definition inventories; it does not call those inventories collected case totals.
