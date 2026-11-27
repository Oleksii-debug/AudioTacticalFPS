# Prepared audio conversion and video soundtracks

The Audio and video conversion workspace uses the existing local FFmpeg runner,
native process-family ownership and shared resource coordinator. It supports
audio-to-audio batches and an explicitly selected audio stream from a local video.
It does not invoke speech synthesis, transcription, a provider or a second player.

## Review and run

1. Add explicit local files, or one folder's direct media filenames. Folder selection
   is not recursive. At most 1,000 inputs belong to one prepared job.
2. Inspect streams. The exact original bytes and installed FFmpeg/FFprobe identities
   are captured. Playlists, network protocols, external MOV data references and
   arbitrary filter/command text are not admitted.
3. Select an audio track and output profile for each input. The batch action applies
   a profile only to single-track inputs; multiple audio tracks always need a choice.
4. Review the disk, per-item time and CPU-thread allowances, then Prepare. Preparation
   makes independent verified source copies and freezes the ordered definition.
5. Start or resume is a separate action. Pause/Stop retain already completed outputs.
   Resume validates the retained source bytes, exact saved profile and processor,
   and reuses the verified completed prefix.
6. Verify outputs without requiring the optional runtime. Export writes a new
   directory of independent audio files and bounded provenance; existing destinations
   are refused, and the native chooser carries its selected parent identity.

WAV, AIFF and FLAC support explicit 16- or 24-bit integer PCM. The existing MP3,
OGG/Vorbis, Opus, M4A, M4B, AAC and WMAv2 codecs are available when the selected local
FFmpeg provides them. Opus and WMA require an explicit 48,000 Hz profile. Compressed
audio can have codec delay or padding; the receipt records actual decoded frames,
without claiming lossless equivalence. Output sample rate, mono/stereo, bitrate and
title/artist/album tags are typed settings. No arbitrary codec option or shell is exposed.

The installed-runtime resolver and existing FULL runtime pin remain authoritative.
Opening this workspace never downloads an engine. A user-selected local FFmpeg must
have its sibling FFprobe. Both executable byte identities must match the preparation
on every new item. Removing an optional runtime does not make completed audio unreadable.

## Durable state and backup

The typed namespace is media-conversion-jobs. Each UUID job contains its immutable
definition.json, mutable job.json, optional run-bound control.json, retained
inputs/000001.media files and an ordered results/000001 directory per completed item.
Each result directory contains exactly its audio and receipt.json. Receipts bind the
whole definition, exact source hash, selected stream, typed profile, processor hashes,
encoded file hash and actual complete-decode sample properties.

Full backup retains queued inputs, partial completed prefixes, complete outputs and
named presets. It verifies bytes before publication and again on restore. It never
follows the inert original-source paths or launches an engine. Native lock files
are validated but not restored. Exact unacknowledged work stages are excluded only
after bounded regular-file/link checks; a stage is not accepted as a completed result.

Lock order is the existing application writer, namespace lock, then sorted actual
job and control locks. An active conversion refuses a full snapshot. A restored
job is data until the user explicitly selects Start or resume. Resource policy remains
shared with Studio, transcription and other operations; the Tasks window shows
media checkpoints and can pause/stop the exact currently running job.

## Bounds and evidence

Each item has one explicit inclusive time allowance across hashing, encoding,
probing and full decode. Watched staging bytes include verification PCM; declared
disk allowances include retained inputs and bounded metadata. Process diagnostics,
stream inventory and allocations have separate limits. CPU/RAM reservations are
declared capacity; RAM and VRAM usage are not measured. There is no silent duration
truncation or newly imposed project-length ceiling.

The existing process owner confirms child-family quiescence before temporary files,
input leases or shared reservations are released. Matching result bytes after an
interrupted directory flush are resynced before a resumed job acknowledges success.

Before the executor outage, the initial core passed 35 actual portable FFmpeg cases,
including 13 codec/depth combinations, a selected second video soundtrack, partial
resume, cancellation, tamper refusal and cleanup ownership. The subsequent source
recovery, UI and complete-backup tests are NOT_RUN until their exact tree is executed.
This document does not convert those earlier results into acceptance of later bytes.

Required exact-tree gates:

- tests/audio/test_media_conversion.py: real FFmpeg encode/decode and retained-source
  behavior; run after the existing FFmpeg preparation, with zero skips.
- tests/storage/test_media_conversion_backup.py: actual complete backup, relocation,
  offline verification/rebackup and partial resume.
- tests/ui/test_media_conversion.py: typed native handlers and current operation lifecycle.
- scripts/windows_media_conversion_smoke.py --native --output ...: actual installed
  Windows wx controls and synthetic PCM-to-FLAC conversion. Physical audio and NVDA
  remain explicitly NOT_RUN.

The desktop hook is install_media_actions(frame), after the existing Tools menu is
created. It registers semantic command conversion.audio through the existing keyboard
registry, without a default shortcut or duplicate installer. The new modules are in
already packaged carr_library.audio and carr_library.ui; no runtime dependency is added.
