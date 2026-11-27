# Audio export and publication verification

Date: 2026-11-26. Base: PR99 commit
`7cf3ee625b0f30342ae23470f2248a0d1de32e40`.

## Changes checked

Encoded exports require a local `ffprobe`, supplied explicitly or found beside
the selected FFmpeg executable or on the executable search path. Missing
inspection support refuses the encoded export before encoding; WAV remains
available. Inspection checks the actual container, audio codec, channel count
and sample rate. A complete FFmpeg decode then counts PCM frames in bounded
memory without writing another decoded audio file. The verification process
uses file-only inputs and is cancelled with its owning operation.

FLAC and AIFF preserve the complete frame count. MP3 explicitly reduces a
96 kHz source to its supported 48 kHz rate; Opus uses 48 kHz. Rate conversion
allows one sample of rounding. AAC may retain at most 1023 trailing padding
samples. Shorter, excessive, malformed or undecodable output is refused before
the final export path is published. Completion keys use pipeline version 3 so
an older completion marker cannot bypass this new export gate; existing checked
PCM step caches remain reusable.

A published render is authoritative even when its final progress-journal write
or status callback fails. Ordinary notification failures return the completed
receipt with a warning. An interruption after publication carries the completed
receipt in `ProductionPublishedError.result`. Neither case rewrites a committed
render as failed or cancelled. Reopening a verified completed render retries
the final journal update. Invalid completion JSON shapes are quarantined and
regenerated using checked cached audio.

The help text, progress message and completed result state the storage limit's
actual scope: cached audio and current output estimates. It is **not** a quota
on total project-folder size. Older renders, persistent music work, assets,
models and quarantine occupy additional storage.

## Executed gate

Linux x86-64; CPython 3.12.14; pytest 9.0.2; installed FFmpeg and ffprobe
7.1.5-0+deb13u1. The strict runner disables Python network access and rejects
missing suites, skips, XFAIL and XPASS.

```sh
python scripts/ci_check.py \
  tests/audio/test_encoded_verification.py \
  tests/audio/test_production_commit.py \
  tests/audio/test_production.py \
  tests/audio/test_ffmpeg.py
```

| Suite | Passed | Failed | Skipped |
| --- | ---: | ---: | ---: |
| Encoded verification | 15 | 0 | 0 |
| Production commit and reopen | 11 | 0 | 0 |
| Existing production | 17 | 0 | 0 |
| Existing FFmpeg | 7 | 0 | 0 |
| **Total** | **50** | **0** | **0** |

The new cases use actual FFmpeg and ffprobe subprocesses on short synthetic
tones or silent PCM. They cover FLAC, MP3, Opus, M4A, M4B and AIFF; chapter
metadata; high-rate MP3 resampling; wrong codec, channels and duration;
truncated MP3; damage after a successful actual encoder; cancellation of an
actual decoder; and final notification/journal faults after publication.

## Boundaries

This gate does not establish speech quality, original multihour neural music,
multihour compressed-export performance, native Windows execution, NVDA
acceptance or an all-project disk quota. The earlier repeated-score RIFF/RF64
benchmark is separate evidence and was not rerun by this gate. Windows encoded
export acceptance requires an actual ffprobe executable in addition to FFmpeg;
an FFmpeg-only test bundle is insufficient for that acceptance.

## Native publication follow-up

The fix branch then merged upstream literal narration and Windows durability
commit `d59cc82e3be7320386eaaba6af3c83da83c27678` without conflicts.
`file_publication.publish_directory_no_replace` now supplies the shared native
exclusive directory rename. It uses Windows rename, Linux `renameat2` with
`RENAME_NOREPLACE`, or macOS `renamex_np` with `RENAME_EXCL`, and fails closed
where no supported primitive exists. The primitive performs no fsync after its
rename, so the caller can record the commit point first. Audio then syncs the
parent separately and reports any durability uncertainty with its completed
receipt.

The actual Linux syscall passed preservation tests for existing files, empty
directories and nonempty directories. A render regression creates a competing
empty directory after the preliminary check and confirms it survives unchanged.
A separate injected post-rename directory-sync failure retains the completed
receipt and complete journal.

The strict follow-up run passed 46 cases with no failures or skips: native
directory publication 4, production commit/reopen 13, upstream literal-narration
plans 11, and merged production tests 18. These overlap the earlier gate and
must not be added to it as a unique-test total. This execution establishes the
Linux path only; the platform-specific Windows and macOS paths were not simulated
as acceptance.
