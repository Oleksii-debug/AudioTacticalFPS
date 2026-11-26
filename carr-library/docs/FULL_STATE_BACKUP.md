# Full library and production-state backups

Use the Library backup command or the existing `--backup` entrypoint to write a
new local backup directory. Keep the returned manifest SHA-256 separately. A
restore requires that exact digest and creates a new data directory; it never
merges into or replaces an existing library.

## What the snapshot preserves

The library database includes every workspace, catalog entry, historical source
revision, saved quotation, note, topic, tag, saved query, bibliography record and
stored metadata revision. Registered original files are copied and verified.

Manifest version 3 adds checksummed application state when it exists:

| State | Backup and restore behavior |
| --- | --- |
| Reading positions and bookmarks | Preserve exact workspace, edition, revision, location and source digests. Reading still revalidates these identities before displaying a page. |
| Audio projects and history | Preserve current project JSON, historical revisions, narration derivatives, voice settings, accepted cached chunks and imported audio assets. |
| Audio drafts | Preserve unsaved new scripts and existing-project drafts, including title, language, output format and base revision. Drafts may contain unfinished or invalid script syntax. |
| Production jobs | Preserve project snapshots, progress and accepted cache records. Restore pauses the job and requires a new output destination and a new render job. |
| Assistant workflows | Preserve typed plans and durable results. Restored plans require a new review and current folder selections before execution. |
| Provider profiles | Validate the strict nonsecret profile schema. Restore clears Credential Manager references and requires reconnecting remote accounts. |
| Provider usage history | Preserve reservations, request hashes and uncertain charge states. Restored ledgers are read-only; a backup cannot authorize another call against an older spending allowance. |
| Import requests, batches and review records | Preserve queue identities, original external source paths, expected source digests and review bindings. Rebind only the explicit local `data_dir` field. |
| Import checkpoint journal | Include a consistent SQLite snapshot when meaningful requests or batches exist. Preserve retained, unregistered originals referenced by its jobs or requests, allowing pending work to resume even after the external original disappears. |

Libraries with no such additional state retain manifest version 2. A routine
synthetic text import's otherwise unused import journal does not force a version
upgrade. Existing version 1 and 2 backups remain readable with their established
schema checks.

## Active jobs and resumable work

The backup takes the library writer lock and short studio/assistant state locks.
It snapshots accepted state between publications. If a state publication already
owns its lock, the backup reports that it is busy and can be retried. It does not
copy unfinished render work, temporary files, lock files or invalidated cache
diagnostics. Import metadata files must remain byte-identical while copied;
atomic replacement during the snapshot causes a visible retry error.

Original external source paths are descriptive history, not new restore targets.
An import that already retained its source resumes from those exact retained
bytes. A queued import that never retained its source still needs the originally
selected file. The restore does not silently substitute another document.

Completed external audio export folders are separate from private project state
and are not copied by this library backup. Restored production jobs retain their
previous output path as history, clear active output/result fields, and refuse to
resume into it. Start a new render in a new folder to reuse intact accepted
chunks. This protects existing exported audio from being overwritten.

Optional model weights, installed executables, voice packs and OS credentials
are not part of the library snapshot. Keep separately reviewed installations
available. A missing voice or model remains an explicit configuration error.

## Verification and limits

Every included sidecar has a relative allowlisted path, exact byte count and
SHA-256 digest. The implementation rejects traversal, symbolic links, reparse
points, multiple hard links, unexpected files, duplicate manifest paths and
unknown secret fields in provider profiles. It validates reading, project,
draft, workflow and connection schemas before publishing the backup or restore.

Copying streams in 1 MiB blocks. Explicit limits are 100,000 sidecar files,
64 GiB of sidecar bytes, 32 GiB per ordinary sidecar file, 32 MiB per JSON
document, a 512 MiB import journal, and a 64 MiB complete manifest. Existing
database and registered-original limits also apply. A request beyond a limit
fails with an explanation; it never produces a silently incomplete snapshot.

Restore verifies the complete staged file inventory and all digests before
creating the destination. Explicit root rebinding and restore guards are then
validated again. Publication retains `.restore-incomplete` until the entire new
root is in place. The application rejects a root with that marker after a crash
or an interrupted publication.

Regression coverage includes legacy schema compatibility, exact reading/project
recovery, drafts, nonsecret profiles, protected provider history, corrupt and
missing bytes, traversal and links, resource limits, a busy state writer, and a
real pending import resumed from restored retained bytes after its external
source file was deleted. These are application recovery checks, not a claim that
an external disk, a scheduled backup or an encrypted archive has been tested.
