# Canonical reader-session recovery in the current reader

Status: implemented; source review and actual Windows qualification are separate gates.
No runtime PASS is claimed by this document.

## Preserved format and explicit workflow

The existing **Tools → Import earlier reading history or reader session** action
accepts the exact canonical `carr.reader-session.v1` envelope. Its fields are
`schema`, `document`, `voice_id`, `rate`, `volume`, `cursor` and
`audio_seconds`. The embedded document must pass the existing complete
`carr.reading-copy.v1` or `carr.reading-copy.v2` decoder, including its original
text checksum, every compact undo/redo edit and exact source-position mapping.

Canonical source: `e3cf74c243d4ef930b526b5648ca9003c0900057`,
`carr-library/src/carr_library/speech/session.py` (`ReaderResume` and
`ReaderDraftStore`). This component adapts that retained format to the current
`reader.legacy_reading.LegacyReadingStore`; it does not introduce another speech
engine, session store, editor or player.

Select the old local JSON file, choose an original/undo/redo revision, review its
text and historical settings, then explicitly create a new editable reading
copy. The existing current editor opens only after the current scope guards and
immutable archive publication succeed. Cancel at file selection, historical
revision selection or preview creates no new archive or project.

The complete **outer envelope bytes**, including original JSON whitespace, are
retained under the existing `narration-imports/<SHA256>.json` namespace. The
current derivative's `legacy_origin.archive_sha256` points to that envelope.
The inner document's checksum does not replace the envelope's identity.
Open retained reading-copy history can reopen it after the external old file is
removed. Existing bare v1/v2 imports retain their previous byte and schema
contracts.

## Meaning of the historical settings

The preview displays the saved voice identifier, rate, volume, character cursor
and audio position as historical observations. It does not resolve an old voice,
grant a runtime permission, apply old settings to the current reader, open a
device or start playback. The envelope does not contain the corresponding PCM.

The character cursor is validated against the **saved document revision**, even
when a different revision is selected for review. It is not remapped to the
selected revision. Rate remains the old integer interval -10 to 10, volume 0 to
100, and finite audio seconds 0 to 31,536,000. Booleans are not accepted as
numeric values. Duplicate fields, extra executable/runtime fields, recursively
nested session envelopes, invalid documents and unknown schema variants fail
before retention.

## Current source, backup and recovery boundaries

Source anchors remain historical locators. The existing
`authorize_snapshot` checks the selected workspace, sealed edition/revision,
original retained asset and exact page text before activation. Archive retention
alone does not grant quotation authority.

No additional unified-backup producer or namespace was added. The existing
typed history inventory, `validate_archive_payload`, `origin_archive`,
pending narration drafts and managed Studio handoff all use the same updated
decoder. Full backup/verify/restore/rebackup retains the exact original envelope
and its current project references. Restoring this history never creates
`state/reading-speech.json` or resumes a speech operation.

The component changes only two product modules:
`reader/legacy_reading.py` and `ui/reading_import.py`. There are no
`library_backup.py`, live-reader, player, engine or desktop action-owner changes.

## Reproducible qualification

`scripts/legacy_reader_session_oracles.json` declares **111 exact tests in each
source and installed mode**: all 60 existing complete history/backup/UI cases and
51 new envelope cases. Function multiplicities include both stacked
parameterizations and imported unparameterized fixtures.

`scripts/windows_legacy_reader_session_smoke.py` declares **33 exact native
checks**. It drives actual Windows revision, Cancel and accept controls with
public synthetic Unicode text; verifies inert saved observations; creates a
current derivative only after acceptance; and runs full backup/restore/rebackup.
It records installed module origins and hashes, exact candidate identity, outer
archive hash and observed historical cursor/revision fields.

The focused `carr-reader-session.yml` gate builds the exact source-bound wheel,
installs the current 56-pin runtime, checks complete declared installed Python
against the wheel, and uses the existing isolated batch acceptance harness for
strict offline tests. Each test phase has the unchanged 1,200-second child bound;
native has a 180-second watchdog. Raw textual receipts are uploaded on every
outcome. Failure, timeout, a missing case, an extra case or a skipped phase cannot
become PASS.

Human NVDA acceptance, physical playback and speech-engine execution are
explicitly NOT_RUN by this recovery gate.
