# Checked library backups: format 4

The library backup contains every workspace, the exact checked SQLite snapshot,
registered original documents, and the explicitly supported sidecars listed
below. Restore always creates a new data folder. It does not merge into an open
library, overwrite an existing destination, apply a recovered note, or start an
audio job.

Keep the returned manifest SHA256 separately from the backup. It authenticates
the manifest that in turn binds every database, original and sidecar byte.

## API

```python
digest = app.backup(destination, max_bytes=None, include_generated=False,
                    cancelled=lambda: False)

manifest = verify_backup(source, digest, cancelled=lambda: False)
restored_root = restore_backup(source, fresh_destination, digest,
                               cancelled=lambda: False)
```

The functions are in `carr_library.library_backup`; `LibraryApp.backup` delegates
to `create_backup`. Existing calls that pass only a destination remain valid.
`max_bytes` is an optional positive integer covering the complete published
database, originals, sidecars and manifest. It is checked before publication.
The staging process may require additional temporary disk space.

`verify_backup` and `restore_backup` share the same complete validation path.
Verification opens only a private temporary database copy and stages checked
sidecar bytes outside the source. The original backup remains unchanged.

## Compatible manifest shapes

The writer emits version 4. Readers discriminate the exact supported field set;
a mixture of formats is rejected. Reading an old backup never rewrites it.

| Version | Additional fields beyond database and asset descriptors | Meaning |
| --- | --- | --- |
| 1 | None | Database and registered original documents; an exact supported database schema is still required. |
| 2 | `schema_fingerprint` | Version 1 plus the exact schema identity. |
| 3, reading-state variant | `schema_fingerprint`, `reading_state` | One optional explicit reading-state descriptor. |
| 3, sidecar variant | `schema_fingerprint`, `sidecars` | Explicit file descriptors for supported sidecars. |
| 3, state variant | `schema_fingerprint`, `state`, `generated_audio_included` | Legacy names relative to `state/`, normalized only in memory and accepted only through installed typed validators. |
| 4 | `schema_fingerprint`, `sidecars`, `generated_audio_included` | Canonical explicit sidecar descriptors with portable paths relative to the backup root. |

Database schema versions 3 and 4 must match their exact known fingerprints and
pass the existing database integrity checks. A restored version 3 database is
preserved as stored; opening that library performs the normal checked upgrade.

Every sidecar descriptor has exactly `filename`, `size_bytes` and `sha256`.
Paths must be portable, bounded and unique, including on case-insensitive
filesystems. Symlinks, reparse points, additional hard links, unexpected files,
missing declared files and unsupported producer formats are rejected.

## Explicit sidecar registry

`backup_sidecars.py` delegates parsing and format limits to the actual producer.
It does not copy arbitrary JSON, settings directories or credentials.

| Stored path | Authoritative producer validation | Additional checks |
| --- | --- | --- |
| `reading-state.json` | `reader.reading_state` | Every location binds the exact workspace, edition, sealed revision, asset, page kind/index and page-text digest. |
| `keyboard-shortcuts.json` | `keyboard_preferences.validate_keymap`, 64 KiB | Command availability and context rules are checked again when the UI activates the profile. |
| `evidence-drafts.json` | `evidence_drafts.validate_evidence_drafts`, 8 MiB and 128 drafts | Each workspace/topic/evidence tuple must exist in the copied database. Exact source and saved-note baseline hashes remain unchanged. |
| `keybindings.json` | Optional legacy `keybindings.decode_preferences` | The real producer must be installed. |
| `ocr-language-presets.json` | Optional `extract.ocr_languages.validate_presets_bytes` | The real producer must be installed. |
| `narration-projects/<id>.json` and `<id>.draft.json` | Optional `reader.narration.validate_narration_payload` | Source-bound original text must match the exact checked page; producer count and aggregate byte limits also apply. |
| `reading-copies/<id>.json` | Optional `reader.reading_copy.validate_project_bytes` | Filename and project identity, workspace, original text and source location must agree. |
| `state/studio/projects/<id>/project.json` and declared revision files | Optional `studio.project.Project.from_dict` | Exact project ID, revision and fingerprint; no generic job-state copier. |

An optional adapter is not permission to invent its data format. If its files
exist while its real validator is unavailable, backup or restore reports that
problem and preserves existing data. Unknown files within registered state
directories are rejected. Private files outside this explicit inventory are
not included.

Archived workspaces and sealed historical source locations are preserved.
Backup validation does not retarget an old bookmark to the current revision or
grant access to an archived scope. The reader authorizes the restored location
again before opening it.

Draft restoration likewise preserves personal recovery data without applying an
edit. A changed saved-note baseline or source hash remains a conflict for the
normal editor to review. Fresh scope authorization, source comparison and the
user's recovery decision are still required after restore.

## Publication and interruption

The existing cooperative library writer guard covers the database snapshot and
sidecar inventory. Sidecars that use independent writer locks are captured with
file-identity checks and a second inventory comparison; a concurrent change
prevents publication.

Files are written and flushed into a private staging folder first. The backup
destination is created exclusively, and `manifest.json` is published last as
the completion marker. Restore uses `.restore-incomplete` until every checked
component has been published. Interrupted destinations are never silently
overwritten or reported as successful.

These guarantees assume stable private local directories and cooperating
writers. They do not claim protection against an unrelated process replacing a
directory entry at the exact publication system call.

## Coverage boundaries

Generated audio is not included by this library adapter. Requesting
`include_generated=True` fails explicitly until an appropriate producer adapter
exists. The selected audio studio stores its projects outside the library;
those projects use their own explicit backup/export operation.

The library assistant's `library-workflows` journals, arbitrary running studio
jobs, model installations, caches and credentials are not restored by this
format. This implementation does not claim complete agent-job backup. Pending
work needs its producer's typed checkpoint adapter before such a claim is valid.

## Reused source and validation

The format 4 writer, legacy discrimination and explicit registry reuse
[AF5 commit 3431ac56c68847eadd61a70fca41bc4a601d4cd2](https://github.com/Oleksii-debug/AudioTacticalFPS/commit/3431ac56c68847eadd61a70fca41bc4a601d4cd2).
Its additions and removals were checked against the owner's reviewed local
`8728c8d0d65d18ae9b5b1ad4aa60c3f0db069c77` implementation. Current application
methods and the common read-only verification API were retained during
composition.

The selected keyboard/draft adapter and genuine producer tests reuse the
[F749 handoff](https://github.com/Oleksii-debug/AudioTacticalFPS/issues/1#issuecomment-7204903264).
The source patch SHA256 is
`c0f63e7d01d9987ad7d22ce1bd2ec04f8911fcf70993c321acf787b2922f4ea7`;
the unchanged imported producer-test file SHA256 is
`098d544e833a67e5ee5c961c1e2aa4e1da1b54814921c5e143b475c2aa70a96b`.

Required focused suites include `test_backup_v4_sidecars.py`,
`test_backup_keyboard_drafts.py`, `test_library_backup_reading_state.py`,
`test_library_backup.py`, `test_backup_schema_identity.py`, and the library
assistant recovery suites. They exercise real SQLite and original files,
historical and archived bindings, byte-identical personal-state recovery,
post-restore authorization, budgets, disk failures, cancellation and real child
process exits. `probe_backup_v4_producer_models.py` is an additional explicit
probe for an installation containing the listed optional producer models;
their absence is an error, not a skipped acceptance claim.

These source tests do not establish Windows/NVDA behavior, physical power-loss
durability, generated-audio restoration or recovery of unsupported running jobs.
