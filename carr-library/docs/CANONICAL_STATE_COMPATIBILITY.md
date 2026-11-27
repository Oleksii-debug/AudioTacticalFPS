# Canonical backup and saved-state compatibility

## What is restored

The reviewed application accepts its existing typed library backup versions
1–6. Canonical backups use a separate version-3/version-4 grammar: they contain
a raw state inventory and generated_audio_included; version 4 also contains
pending. Those exact field sets distinguish the formats. Numeric version and
the common carr-library kind do not.

The catalog and registered originals are authenticated using the independently
retained manifest SHA256, actual SQLite signature and schema fingerprint,
source hashes and existing relational/source validators. Canonical catalog
schemas 3, 4 and 5 must match the reviewed implementation exactly. An unknown
SQLite layout with a familiar version number is rejected before writable
Storage access. Older accepted catalogs remain unchanged while staged;
opening the restored library performs its normal checked upgrade.

Canonical Studio projects, speech reading copies, provider profiles and
translation jobs use different state formats. Their saved engine paths,
permissions and receipts do not become current execution permission. They
are retained as inert data in:

- preserved-state/files/: original committed bytes and relative names;
- preserved-state/review.json: inventory and PRESERVED_REVIEW_REQUIRED status.

The receipt records that migration is incomplete and execution, provider and
quotation authority are false. Canonical state is not installed into active
assistant, translation, provider, Studio or speech runtime paths. No historical
reading copy becomes verified evidence.

Preserved bytes are not sanitized or claimed to be secret-free. They may
contain old credential references and private text already present in the
trusted local backup. They are not sent anywhere or interpreted as commands.
References to external executables, models, inputs or outputs are never opened.

## Subsequent backups and limits

A backup containing preserved state uses version 7. Its exact manifest contains
all version-5 fields, a typed transcription component (possibly empty), and the
nonempty preserved_state component. Version 6 retains its requirement for
nonempty typed ASR/resource-policy state. Existing version-5 and version-6
behavior remains when no preserved state exists.

The manifest, catalog, originals, typed sidecars, pending imports,
ASR/resource-policy data and preserved state share the existing complete-backup
byte and file ceilings. Optional limits only lower those ceilings. File
contents stream through bounded buffers; directory visits, path depth and
metadata allocation are bounded before retaining an inventory.

Subsequent restoration verifies actual preserved files against the review
receipt and enclosing manifest. Resigning a receipt with provider_authorized
or another authority flag enabled does not make it acceptable.

## Old state in a live library

Recognized canonical namespaces include state/studio, state/speech,
state/providers, state/audio, state/ui, and translation's nested jobs/provider
directories. Their presence selects explicit preservation for the committed
legacy tree, so a successful backup does not silently omit those files.

Assistant and translation state in that tree is preserved for review, not
restored as resumable current jobs. Current state/transcription and
state/operations retain their separate typed adapters, guards, resource-policy
checks and runtime/model exclusions. They are not duplicated through raw
preservation. Current provider profiles and the SQLite cost ledger remain
separately sanitized and typed.

A live canonical layout mixed with current flat translation checkpoints is
refused explicitly: independent cost ledgers cannot be merged implicitly.
An existing preserved component mixed with new active canonical state likewise
requires review. No backup is published in either case; source data remains.

Close the old producer before a live snapshot. The copier checks identity and
size while streaming, rereads source content against the copied SHA256 and
compares the complete inventory before publication. Detected mutations fail.
Untyped live SQLite/database files need their own consistent adapter or a
verified canonical backup; they are not copied as ordinary JSON.

Locks, temporary writes and .pending- entries are not committed state.
Committed generated files already inside a live legacy tree are included under
the same global bounds. Canonical archives retain exactly their authenticated
inventory, including the original generated-audio inclusion choice.

## Paths and publication

Regular local files are required. Symbolic links, junction/reparse points,
hard links, traversal, alternate streams and reserved Win32 names are rejected.
NFC-equivalent or case-equivalent namespace collisions are rejected without
renaming source files. File/directory aliases and undeclared payload directories
are rejected.

Restoration creates a new directory, never merging or overwriting. Cancellation
before publication leaves no successful manifest. A publication failure retains
the existing incomplete-restore marker. Archive validation is read-only and
creates no locks inside the source.

## Source identity and evidence

Tests use unmodified public canonical producer fixtures:

| Source commit | Catalog | Writer blob | State-inventory blob |
| --- | --- | --- | --- |
| 184dd83d43f285ce27b6f351e82fee0bbaf19323 | V4 | 01b4945c5f51ec90012b1ec895cca929dbdaff13 | 0224cf450d7d3790876bee9b03ad0573f70ba173 |
| 1eff701fe848a3bd9eb088c85e5372a7c0a73861 | V5 | 8adc760d1ea99f87ecb7c577e2a9b264e1a3d812 | a6b01f762dc362dfd23544c6f5f374fddd210787 |

Tests verify those blob identities, then run the actual producer over real
synthetic SQLite/catalog/original files. Both backup versions and catalog
generations, empty and empty-nested state, retained authored bytes, corruption,
authority tampering, aliases, aggregate limits and subsequent version-7
roundtrips are covered. A portable test models the Windows DirEntry/Path
link-count difference; production consistently uses checked Path metadata.
Path-to-handle binding compares stable device/inode/size/link identity. Full
path and handle change stamps are compared separately within each API; a
cross-API timestamp difference is not treated as a content change. Source
content reattestation, link/reparse refusal and within-API mutation checks
remain required. A real atomically written JSON roundtrip plus portable API
clock-divergence, handle-change and foreign-inode cases cover this boundary.
The isolated workflow includes a separate Windows restore/file-identity job.

Root and current canonical storage/revisions.py share exact blob
0e326329b65b519a7743503ee2967211fd19897b. Their schema_identity.py shares
2c72c6132969cb1076035a47ee87c4573ebd93be. This is evidence for the accepted SQL
layout, not permission to accept unknown layouts.

The isolated source workflow includes the existing library, pending-import,
provider, translation, assistant, reading-state and ASR backup suites. It
supplements installed Windows acceptance. The exact ea85d19d checkpoint ran on
Linux in Actions 45941173243: all 71 new compatibility cases passed. The complete
gate did not pass: two existing translation budget cases exposed an ordinary
call-signature compatibility issue, and one real OCR case lacked Tesseract.
The successor retains the ordinary sidecar call signature and installs that
existing OCR prerequisite. Its result must be recorded separately.

## Interrupted canonical publication

The current canonical worker includes source/data-directory paths in its
publication digest; the reviewed worker binds retained source bytes instead.
The SQLite receipt itself remains immutable historical evidence. Pending
restore already validates candidate shape, attempt, retained source and journal
relations, then clears the old process attempt and candidate before explicit
resume. It does not reinterpret the historical digest or transfer execution
authority. No alternate digest validator or weaker ownership path is added.

The interruption regression uses additional unchanged 1eff producer sources:

| Module | Exact public blob |
| --- | --- |
| application.py | 747c692692f191161b1ca6b5c212abdf0e17345a |
| ingest/worker.py | e219ff740c54b6280fc0dc6a09e53da1671a4dc7 |
| pending_backup.py | c039d5d7000ac5a4adafdc631820933c420dba84 |

It launches the real canonical supervisor and child with public synthetic text.
Scheduling instrumentation stops the child after its SQLite publication, or
stops the supervisor after the real worker returns, before request finalization.
The former leaves a pending extraction journal and the latter a complete one.
In both cases the request remains running with the actual committed candidate.
The original source/library are removed before restore. The test then requires
passive restore, cancellation without child launch, explicit real-worker resume,
unchanged original bytes, one sealed revision and the identical old SQLite
receipt. Separate source, attempt and journal tampering cases retain the
existing rejection boundary. The new interruption cases are NOT_RUN until their
exact workflow result is recorded. Native Windows remains separate acceptance.

## Desktop integration

Call carr_library.preserved_state.review_notice(restored_path) after successful
restore and before offering to open the restored library. It returns an empty
string when absent, otherwise a plain English review-needed notice and the
local inventory location. It never reconstructs a provider, job or engine.
