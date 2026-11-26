# Full-library backup formats

`create_backup` keeps the database, registered originals, exact source-bound
reading places, supported private sidecars and durable pending imports. When
import journals, requests or batches are present, new backups use **version 5**.
With only sidecars/private state they retain version 4; with neither component
they retain version 2. The manifest binds
each included file by its relative filename, size and SHA256. Keep the returned
manifest digest separately from the backup directory.

Version 5 combines `sidecars`, `state`, `generated_audio_included` and `pending`.
A null
`state` means the directory was absent; an empty list means it was present but
contained no included files. The pending component uses its existing version-1
schema; the enclosing version distinguishes historical layouts unambiguously.

| Existing format | Accepted extension fields |
| --- | --- |
| Version 1 | None; exact supported database schema is checked |
| Version 2 | `schema_fingerprint` |
| Version 3, reading places | `reading_state` |
| Version 3, sidecar registry | `sidecars` |
| Version 3, private application state | `state`, `generated_audio_included` |
| Version 4, sidecar union | `sidecars`, `state`, `generated_audio_included` |
| Version 4, pending imports | `pending`, `state`, `generated_audio_included` |
| Version 5 | `sidecars`, `pending`, `state`, `generated_audio_included` |

Each historical version-3/4 form must match its exact field set. The historical
version-4 pending form requires its state list/directory, even if empty. Mixed
forms and missing version-5 fields are rejected;
the restore does not guess which feature owns an ambiguous manifest.

## Source identity and optional features

Reading positions and bookmarks must match the saved workspace, edition,
sealed revision, original asset, physical page or logical segment, and exact
text digest in the database snapshot. Archived libraries and historical
revisions remain unchanged. Restoration never moves an older bookmark to the
active revision and never approves an unverified quotation. The reader applies
its normal access and active-revision checks when opening a restored location.

The shared sidecar allowlist also recognizes narration copies and drafts,
keyboard preferences, OCR-language presets and evidence recovery drafts.
Optional features supply their own existing byte validators. If a recognized
file is present but its owning feature is unavailable, backup/restore fails
explicitly; the file is neither dropped nor trusted without validation.
Evidence drafts remain recovery data and require fresh evidence authorization
when the editor offers recovery.

## Pending imports

The existing pending-import adapter snapshots the import journal through
SQLite's backup API, plus committed request/batch/review sidecars and every
referenced original needed to resume. It preserves job IDs, checkpoints,
expected source hashes, workspaces, editions and review attestations. On restore
only the saved data-directory and source-location fields are rebound to the
new local root. A missing or replaced planned original makes a complete backup
fail explicitly; unrelated retained bytes are excluded. The outer manifest
binds this component alongside reading state and production receipts.

Pending validation retains its 512 MiB journal, 25,000-file and 8 GiB total
limits. Reads, hashes and copies share the portable before/after handle checks
used by private state, without equating Windows path and descriptor inode APIs.
Cancellation and corruption fail before destination publication; an interrupted
publication retains the library's incomplete marker. Restored imports use their
normal resume path and cannot turn an old checkpoint into a different source.

## Private production state

Private state includes saved production plans, jobs, receipts and settings.
`include_generated=True` additionally retains the generated audio cache. An
accepted original cue in the production service's `cue-<hash>-<id>` namespace
is always retained, even when generated audio is excluded; its durable binding
stays in the saved job. The backup does not mistake these irreplaceable inputs
for regenerable speech or music. An
active production job holds the same `.production.lock` used by the snapshot,
so the backup reports that the production is busy instead of mixing stages of
a render. Atomic metadata writers using other locks are checked by repeated
inventories and streamed checksums; a concurrent edit causes a retryable failure.

State defaults to at most 32 GiB and 100,000 filesystem entries; the snapshot
API accepts smaller explicit byte/entry budgets. Directory entries and omitted
temporary/cache entries also count toward the inventory limit. Copies and
verification use 1 MiB blocks. Links, reparse points, path traversal, device
names, duplicate portable paths and active database sidecars are rejected.
Restore supports cancellation during streamed private-state copying and checks
the source again before a destination can become complete.

Exports outside the private state directory are not silently discovered or
copied. Retain them separately, or use the production service's explicit
re-export action with its verified cache after restoration. Installed voices,
executables, model weights and operating-system credential-store secrets are
not bundled automatically.

Restore always creates a new directory. It verifies the manifest, database,
originals, sidecars, pending imports and state before publication; it never merges into an
existing library. An interrupted publication retains `.restore-incomplete`,
and the application refuses to open that directory as a completed library.
Every new file is flushed. POSIX additionally flushes published directories;
the Windows durability and native runtime gate remain separate acceptance.
