# Canonical playback queue preservation and explicit migration

This integration retains the canonical carr-playback-queue/1 data under
state/speech/playback-queue.json and its declared
state/studio/inputs/<sha256>.audio originals. It uses the existing managed
PlaylistStore and the existing Studio player. It adds no playback runtime.

## Historical backup contract

A common schema-4 backup may contain at most 256 queue items, 4 GiB per original
and 32 GiB across the selected items. The larger allowance belongs only to
actual queue references that pass the exact JSON schema, matching outer
inventory, complete checksum and bounded PCM dimension checks. An unrelated
.audio file does not obtain the larger allowance. Unreferenced historical
metadata remains subject to its original 512 MiB budget.

Verification accepts the donor's ordinary PCM WAV widths of 8, 16, 24 and 32
bits and preserves the full original bytes. Device declarations, old controls,
sleep settings and source paths remain inert. The raw manifest and queue
survive restore and rebackup under the existing hashed historical namespace.

## User action

In Audio Studio's Playback page, **Import previous playback queue** offers
preserved queues and an explicit canonical-library folder picker. The existing
Studio worker verifies the selected originals. A named, keyboard reachable
review dialog states the byte count, order and saved coordinate before
**Import as new project** is available.

The importer creates a new managed project in a private directory and uses
current resource admission. It retains independent checksum-named originals,
exact raw queue provenance and labels. A saved coordinate is marked
historical_cursor. Current playback controls use their defaults; the old
output declaration cannot select a device. The user chooses **Restore saved
queue**, a current output and explicit Play or Resume.

Current playback supports PCM16 up to 384 kHz. A canonical 8/24/32-bit or higher
rate original is preserved in history but is refused by this importer with an
explicit conversion explanation. It is not silently converted or relabelled.
A separately reviewed conversion can produce a new current input; it does not
replace the preserved original.

## Publication and recovery

The managed project appears only through one no-replace directory publication
after its draft/render locks have closed. The final boundary checks the exact
source selection, full original hashes established at review/copy, native
ChangeTime pins, directory identities, metadata digests and finite staged
inventory. The last external cancellation callback precedes every final source
and payload check; no later observer callback can change an already checked
payload before the rename.

A post-publication cleanup or durability error carries
QueueImportCommittedError.result, including the existing project ID. It must
be reopened and verified, not automatically imported again. The UI retains
that identity even if a closed or changed-library window suppresses its normal
completion callback.

Both project ZIP and complete-library backup retain
audio/legacy-playback-queue.json and its original media, including after the
current queue is cleared. Empty queues retain their provenance without needing
an assets directory.

## Required acceptance

Portable source and isolated installed-product contracts:

- tests/storage/test_legacy_playlist_backup.py
- tests/audio/test_legacy_playlist.py
- tests/ui/test_legacy_playlist_controls.py
- tests/storage/test_legacy_playlist_acceptance.py

These are mandatory zero-skip suites through the existing strict runner.
The existing Windows main, full-bundle and audio gates additionally run:

    python -I -B scripts/windows_legacy_playlist_smoke.py --native --output native-legacy-queue.json

The native probe uses actual owned wx controls, separate focus/event turns and
real retained files. It checks decline, explicit import and restored queue
state without starting an audio device. It does not qualify physical output or
NVDA. Existing windows_managed_queue_smoke.py calls now also pass their
required --native switch.

The separate large-source workflow runs an actual 513 MiB PCM fixture on Linux
and Windows using the installed product:

    python -I -B scripts/legacy_playlist_acceptance.py --workdir NEW_PRIVATE_DIRECTORY --expected-package INSTALLED_CARR_LIBRARY_INIT --output actual-513-mib.json --mib 513 --seconds 3600

The producer writes real nonempty blocks, hashes all bytes, restores the old
backup into inert history, deletes the old fixture, explicitly migrates the
retained source, performs a complete backup and restores it under another
root. It then checks the complete PCM hash, exact provenance and typed queue.
Its preflight reserves eight simultaneous file equivalents plus 2 GiB and
refuses before producing data when unavailable. Only reports are uploaded;
the PCM and library copies stay ephemeral runner fixtures.

**Evidence status at source preparation:** all new suites, native events and
the actual >512 MiB lane are NOT_RUN while the executor is unavailable.
A source adapter and a configured workflow are not format/capacity
qualification. Any later PASS must name the exact tested commit and runner.
The 4 GiB per-file schema bound is not a measured 4 GiB acceptance result.
