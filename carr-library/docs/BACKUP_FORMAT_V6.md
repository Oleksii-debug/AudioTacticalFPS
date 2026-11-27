# Complete backup format 6: retained Studio media

Version 6 extends the existing complete local-library backup with an explicit
typed Studio media envelope. It keeps the established database, original-source,
pending-import and personal-reading producers. A backup without typed Studio
media still uses the applicable earlier canonical format.

## Supported archive versions

| Archive | Additional component |
| --- | --- |
| 1 | Exact supported library database and registered originals |
| 2 | Exact database schema fingerprint |
| 3 | Private application state; the distinct historical reading-only shape remains accepted |
| 4 | Pending import journals and pinned unpublished sources |
| 5 | Exact personal bookmarks and reading positions, with nullable pending imports |
| 6 | Retained Studio input bindings, recording originals and publication receipts; reading and pending components may be absent |

The archive version and SQLite schema version are separate. The current SQLite
schema remains version 5. Its immutable import_publication rows are copied with
the database, alongside their sealed revisions. Restore neither fabricates new
publication receipts nor moves saved reading locations to another revision.

## Exact envelope

Version 6 retains the canonical top-level keys version, kind, schema_version,
schema_fingerprint, database, assets, state, generated_audio_included, pending
and reading_state, and adds studio_media. The two nullable component fields
remain present even when their value is null.

studio_media has exactly these fields:

| Field | Meaning |
| --- | --- |
| schema | Integer 1 |
| studio_input_receipt | Whether the state contains the authenticated Studio input receipt |
| studio_publications | Sorted relative paths of validated publication receipts |
| studio_recordings | Sorted relative paths of recording receipts bound to actual PCM originals |
| studio_unverified_recordings | Sorted relative paths of final WAV files without capture receipts |
| required_inputs | Sorted unique declarations of local voices, music engines and external publication outputs, each with included=false |

Unknown, missing, duplicate, mismatched or downgraded metadata is rejected.
Every listed path is also present in the independently hashed state inventory.
The full manifest remains bounded by the existing 8 MiB limit; state and retained
inputs share the existing 100,000-file and 32 GiB limits.

## Creation and recovery

Creation first takes the ordinary verified private-state snapshot. It then
copies each required, explicitly selected Studio cue input under its SHA-256
identity and binds that copy to the exact project, historical revision and job
bytes. Fresh content checks catch equal-size changes even if file metadata was
preserved. Pending legacy input declarations require explicit local review
before they can be read by a new backup.

Recording receipts are checked against retained byte size, SHA-256 and the actual
PCM header and frame count. A completed WAV without its capture receipt remains
an inert unverified original. Generated-audio exclusion omits rebuildable caches;
it keeps selected media and completed recording originals.

Restore verifies the database, registered originals and entire state inventory
before interpreting typed media. It repeats the exact media checks and compares
the observed result with studio_media. It reads only verified staged media;
recorded external output or engine paths are never probed or executed.

Only the private restored projection is changed. Project cue paths point to
retained inputs in the new state root, and historical project filenames follow
their rebound fingerprints. Jobs wait for an explicitly selected new output
directory. Their previous status and source provenance remain recorded. The
original backup, its project bytes and its independent manifest digest remain
unchanged. Repeating backup and restore retains that previous provenance.

Legacy archives without an input receipt remain readable. Their external cue
paths stay as declarations requiring fresh review; inherited approvals are
cleared, and old output destinations do not authorize new writes.

Publication receipts keep the exact original artifact and source hashes. The
external EPUB is declared rather than copied into this backup. After relocation,
the user selects an existing EPUB and verifies its actual bytes and graph before
its receipt can become current at the new root.

## Cancellation and evidence

Restore accepts the existing positional arguments and an optional cancelled
callback. Cancellation before publication leaves no destination. Once publication
has started, an interrupted destination retains the ordinary incomplete-restore
marker and cannot be opened as a complete library.

The focused tests retain the donor real-PCM round trip that deletes the original
library, selected external inputs and old outputs before rendering again. Added
cases restore media, exact bookmarks and immutable import receipts together and
reject resealed semantic corruption before creating a destination. These cases
must run on the exact composed source and installed wheel; their presence alone
is not a runtime pass. Physical microphones, human listening, external EPUB
readers and manual NVDA acceptance remain separate checks.
