# Historical backup format 6 and the current typed media inventory

The current writer emits one typed version-4 archive. It preserves the media
behavior introduced by the former version-6 writer through registered sidecars
and the same explicit studio_media envelope. Version 6 remains an exact
historical read format; it is not a second current writer.

## Supported manifest families

| Archive family | Complete shape selected by the reader |
| --- | --- |
| 1–2 | Historical database and registered-original envelopes; version 2 also pins the database schema fingerprint. |
| Historical 3 | Explicit reading-only, sidecar, or opaque-state envelope, each distinguished by its complete field set. |
| Historical 4 | Opaque state plus pending imports and pinned unpublished sources. |
| Historical 5 | Opaque state plus exact personal reading positions/bookmarks, with nullable pending imports. |
| Historical 6 | The historical state envelope plus typed retained Studio media, recording originals and publication receipts. |
| Current typed 4 | Common typed sidecars, with optional pending-import and Studio-media components. Current reader/settings/draft producers use this inventory. |

A malformed typed archive never becomes a legacy archive just because a validator
failed. Missing, extra or mixed discriminator fields are rejected. The archive
version and SQLite schema version are separate; database schema fingerprints and
immutable import_publication receipts keep their existing validation.

## Typed media envelope

Historical version 6 has the exact top-level keys version, kind, schema_version,
schema_fingerprint, database, assets, state, generated_audio_included, pending,
reading_state and studio_media. Its nullable component keys remain present.
Current typed version 4 lists the same owned media bytes in sidecars instead of
state; personal reading data also use the common typed inventory.

In either family, studio_media has exactly these fields:

| Field | Meaning |
| --- | --- |
| schema | Integer 1. |
| studio_input_receipt | Whether the authenticated retained-input receipt exists. |
| studio_publications | Sorted relative paths of validated publication receipts. |
| studio_recordings | Sorted recording receipts bound to the actual PCM originals. |
| studio_unverified_recordings | Sorted final WAV files without capture receipts; they remain unverified originals. |
| required_inputs | Sorted unique declarations of voices, music engines and external publication outputs, each with included=false. |

The envelope must equal inspection of the independently hashed staged inventory.
Unknown, missing, duplicate or mismatched declarations are rejected. No external
path in a declaration is opened or executed by validation.

The manifest remains bounded by the existing 8 MiB limit. Current typed metadata,
retained media and optional generated media retain their distinct aggregate
budgets and the 100,000-entry bound described in
[Primary desktop state](PRIMARY_STATE_BACKUP.md). Exact historical opaque state
keeps its original 32 GiB total limit.

## Creation and recovery

Creation snapshots verified private state under the existing library and producer
locks. It copies explicitly selected Studio cue inputs under their SHA-256
identity and binds each copy to exact project, historical revision and job bytes.
Fresh content checks catch equal-size changes with preserved metadata. Pending
legacy input declarations require explicit local review before a new backup may
open those external paths.

Recording receipts are checked against retained size, SHA-256, PCM header and
frame count. Excluding generated audio removes rebuildable caches; it retains
selected cue media and completed recording originals. Container checks do not
claim recording or acoustic quality.

Restore verifies the database, originals and complete inventory before interpreting
media. It repeats the exact media checks and compares the envelope. Only the
private restored projection changes: project cue paths point to retained inputs
at the new root, revision filenames follow rebound fingerprints, and jobs wait
for a newly chosen output directory. Previous status, source provenance and
artifact identities remain recorded. Repeating backup and restore preserves that
provenance while the original archive and its digest stay unchanged.

Historical archives without an input receipt remain readable. Their external
cue paths stay as declarations requiring fresh review; inherited input approvals
are cleared, and old destinations grant no write permission.

Publication receipts retain original artifact and source hashes. External EPUB
outputs are declared, not copied into the private library archive. After
relocation, a user selects an existing EPUB and verifies its actual bytes and
graph before its receipt becomes current at the new root.

## Cancellation and qualification

Restore keeps its positional API and optional cancelled callback. Cancellation
before publication leaves no completed destination. An interruption after
publication begins retains the incomplete-restore marker.

Focused tests retain the real-PCM round trip that removes the source library,
selected external media and old outputs before restoring and rendering again.
They exercise both the current typed writer and explicit historical version-6
fixtures, combine media with exact bookmarks and immutable import receipts, and
reject resealed semantic corruption before publication. Test source alone is not
a runtime pass: the exact composed source and installed wheel need their own
recorded results. Physical microphones, human listening, external EPUB readers
and manual NVDA acceptance remain separate checks.
