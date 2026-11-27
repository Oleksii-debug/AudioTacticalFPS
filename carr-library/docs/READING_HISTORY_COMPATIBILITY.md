# Earlier reading-copy histories

The existing reading editor can explicitly import canonical
`carr.reading-copy.v1` and `carr.reading-copy.v2` files. Tools contains
**Import earlier reading-copy history** and **Open retained reading-copy history**.
Both are semantic commands with no additional default shortcut. The original
file is kept unchanged.

## Review and create a current copy

1. Open the correct library and choose an earlier reading-copy JSON file.
2. Select the exact historical revision. The saved cursor and every redo edit
   remain visible; an importer never assumes that the last edit is the saved one.
3. Review the full text, source positions and mapping. Choose a title for the
   new current copy, then explicitly create it.
4. Continue in the existing reading editor, with its save, recovery, comparison,
   pronunciation and Audio Studio handoff. Return to the retained history to
   choose another old revision at any time.

The original compact JSON is stored once under
`narration-imports/<raw-file-sha256>.json`. Multiple current copies reference
that same immutable file. Up to 500 compact edits are validated without
constructing 500 complete text snapshots. The new copy uses the current
editor's bounded history; it contains the original text and the chosen
historical derivative, rather than silently truncating the older undo history.
The full original title remains in the archive; the current title is explicitly
reviewed within the existing editor's 200-character limit.

## Source identity and compatibility

The importer preserves all positions in a canonical multi-position
`source_anchor`: workspace, edition, exact sealed revision, retained asset
hash, page/segment order, text-slice hashes and historical verification flags.
The two released pre-canonical identity dialects are also read explicitly:
`source_origin` with `text_verified`, and the earlier anchored
`carr.narration-source.v1` form with `text_verified_at_read`.
Mixed dialects, unknown fields or invented separator mappings are rejected.

Activation verifies current access, the exact historical sealed pages, their
actual raw text and the retained original bytes. It never substitutes the
currently active source revision. Archived libraries can be backed up, but
opening their history still requires explicitly restoring access.

A new project with imported provenance uses current reading-project version 4.
Versions 1–3 remain readable, and ordinary projects continue writing versions 2
or 3 with their existing field sets. The imported origin is immutable for a
project identity and accompanies its saved Audio Studio source reference.
Captured verification flags and source maps are historical data; none creates
quotation authority. Canonical edits that reproduce the same words remain
derived. The detailed old exact/derived map remains available in the retained
history; the newly forked editor uses a conservative derived map for subsequent
editing rather than guessing a recovered legacy edit lineage.

## Complete backup

The typed reader producer includes each compact history once. Before backup,
verify and restore, it checks the raw filename digest, all edits, every source
anchor against the database, and each current reading/Studio reference against
its selected archived revision. Missing histories and foreign or changed
bindings fail closed. Restore preserves exact bytes and source identities.
No executable configuration, provider credentials or quotation approval is
created by this migration.

Limits: 32 MiB per retained history, 1,000 histories and 64 MiB total archive
bytes, 2,000,000 Unicode characters per historical revision, 500 edits and
10,000 source positions. These are separate from the existing current-editor
history and backup budgets.

## Validation

Focused suites:
- `tests/reader/test_legacy_reading_import.py`
- `tests/storage/test_legacy_reading_backup.py`
- `tests/ui/test_reading_import.py`

Installed Windows control probe:
`python -I scripts/windows_legacy_reading_smoke.py --native`.

The probe uses only synthetic Unicode text and real named wx controls. Its
result covers revision selection, read-only preview, explicit title and Cancel;
it reports NVDA as NOT_RUN. Source/API preparation alone is not execution
evidence.

## Canonical recovery sessions

Released `state/speech/<sha256(slot)>.reader.json` files use the typed
`carr.reader-session.v1` envelope. The same history adapter now reads the nested
reading-copy v1/v2 document, retains the complete original envelope bytes by
their SHA256, and exposes all saved undo/redo revisions through the existing
history chooser. The chooser also lists valid original recovery files already
present in the selected library, deduplicating copies with identical bytes.
The original path remains protected when a new current project is created.

Saved voice identity, rate, volume, text cursor and audio position remain inert
historical metadata. They are validated and shown as recovery information;
they neither select a current engine nor seek or resume audio. The saved text
cursor is validated against its saved revision even when a different historical
revision is selected. Current playback still requires an explicit current voice.

Complete backup includes these original typed session files before any import,
under their original paths and exact bytes. It acquires each released session's
native `.<slot-sha256>.lock` after the application writer, rejects active writers,
rechecks the file inventory before completion, and verifies every historical
source anchor against the snapshot database. Lock and known private staging
files are excluded; unknown files or foreign source bindings prevent a complete
backup. Session limits are 1,000 files, 32 MiB per file and 64 MiB in total.

The three focused source suites passed 85 cases after this extension, including
actual file-lock exclusion, a new concurrent session during copying, byte-exact
restore/rebackup and explicit recovery into the current project store. Native
Windows dispatch and voice playback are separate acceptance gates.
