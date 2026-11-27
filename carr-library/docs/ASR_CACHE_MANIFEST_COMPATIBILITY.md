# Historical ASR cache inclusion

Canonical PR196 at `a44fac04072389caa40e0d6e799b13797712f797` adds a manifest admission rule: optional recognition cache must not contradict `generated_audio_included`.

The current implementation applies that rule in `backup_historical.validate_manifest`, using the reused `legacy_transcription_schema.is_cache_file` recognizer. State-v3, state-v4, common root-v4 and state/reader-v5 names are normalized before checking current worker and worker-history cache layouts. A false flag rejects cache entries; an absent flag still fails the existing exact historical-envelope requirement. This does not introduce another backup format or transcription service.

Retained source inputs, accepted transcript receipts, edits and non-cache worker results remain recoverable when optional cache is excluded. Historical settings, runtime declarations and output paths stay inert. Current managed transcription checkpoints retain their existing typed producer and completion authority; the historical flag does not silently change their retention policy.

The adapted existing recovery suite adds 128 pure envelope cases and two real restore/rebackup/tampered-manifest scenarios. These new sources are **NOT_RUN** at preparation time. They do not establish native inference or throughput qualification.

| Canonical path | Current contract owner |
| --- | --- |
| `src/carr_library/backup_transcription.py` | `src/carr_library/legacy_transcription_schema.py` |
| `src/carr_library/backup_sidecars.py` | `src/carr_library/backup_historical.py` |
| `tests/storage/test_transcription_backup.py` | `tests/storage/test_legacy_transcription_recovery.py` |
| `docs/PRIMARY_STATE_BACKUP.md` | This note plus the current `docs/COMPLETE_BACKUP.md` recovery contract |
