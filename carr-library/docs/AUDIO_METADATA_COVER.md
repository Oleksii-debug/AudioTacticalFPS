# Reviewed audio metadata and local cover

Audio Studio → **Review audio metadata and cover** edits a new copy of a selected
MP3, FLAC or M4A/M4B file. It uses the configured, verified local AudioRuntime and
the Desktop's existing background worker. There is no new scheduler, project
schema, media database or automatic playback.

## Explicit review and save

1. Choose a local supported audio file and **Inspect source tags and chapters**.
2. Edit title, artist/author, album/book, album artist/book author, composer,
   genre, date, track, disc, comment or copyright. An empty field removes that
   portable tag. Track and disc accept positive numbers, optionally /total.
3. Keep the existing cover, remove it, or select one local PNG/JPEG you may use.
4. Choose a **new output filename** outside the private library and select
   **Review metadata and cover changes**.
5. Read the resulting tags, source and cover SHA-256 values, chapter count and
   destination. **Save reviewed audio copy** publishes those exact choices.
   Any form change requires another review.

Every control has a native accessible name. Inspection, image decoding, hashing,
remuxing and readback run off the UI thread. Cancel and Close signal owned work;
late callbacks do not access a destroyed dialog. A late cancellation does not
turn an already saved file into a failed save.

## Format and resource boundaries

This profile stream-copies one encoded audio stream within its current container
family: MP3 → MP3, FLAC → FLAC, or M4A/M4B → M4A/M4B. It does not transcode or
claim tagging support for WAV, Ogg, raw AAC, AIFF or WMA. Use the existing
conversion workflow when a different format is required.

A source may contain one audio stream, at most one attached PNG/JPEG cover and,
for M4A/M4B with chapters, the existing chapter data track. Video, additional
audio tracks and unknown data streams are rejected rather than silently removed.
Other source tags must survive readback exactly; unsupported container/tag
combinations fail before publication.

A replacement cover is an explicit regular local PNG/JPEG, no more than 8 MiB,
8192 pixels on either side and 16 million pixels. Its fixed image demuxer admits
one still image; an actual bounded decoder rejects incomplete image payloads.
Animated formats, URLs and playlists are not admitted. The cover's compressed
bytes are copied unchanged. The existing AudioBudget applies to input/output
size, temporary disk use, process time and captured output. The current shared
per-library resource policy admits inspection, review and save with the actual
thread and memory budgets; saving also reserves the measured source/cover
staging space. An unconfirmed native shutdown retains its staging directory and
resource reservation with the existing native cleanup owner until that owner
confirms quiescence.

The metadata fields accept bounded Unicode text. They are passed as separate
FFmpeg arguments, never as shell/filter instructions. No cover license or
third-party distribution right is inferred from selecting an image.

## Integrity and publication

Inspection binds the original source and replacement cover by size and SHA-256.
Save copies those reviewed bytes into a private staging directory, rechecks the
originals, and keeps the selected destination directory's identity throughout
work. A changed source, cover, library or destination requires a fresh review.

Before publication, real ffprobe/FFmpeg readback verifies:

- every retained/edited tag and the attached cover's exact compressed-byte hash;
- audio codec, sample rate, channels and duration;
- chapter titles, count and measured boundaries, with at most 1 ms container
  timestamp rounding;
- the SHA-256 of the encoded audio packets using streamhash with stream copy.

The new audio and its adjacent .metadata.json receipt use no-clobber
publication. The receipt records source/output names and hashes, image identity,
chapters and the encoded audio hash, without private absolute input paths.
If audio publication has committed but the adjacent receipt cannot be saved,
the UI reports the saved audio path and a receipt warning. It does not overwrite
a competing receipt or falsely report that the audio disappeared.

Late writer errors are accepted as a saved result only when the exact staged
inode, selected parent directory, length and full verified bytes prove that the
owned no-clobber link committed. A foreign inode with equal bytes is insufficient.
Cleanup and resource-release failures are reported separately from verified
saved audio. After cleanup the output is checked again; a replaced or changed
output is never granted completion. Receipt verification has its own warning
and does not revoke a still-verified audio output.

The adjacent receipt is evidence for this explicit export, not a restart job
or permission to adopt an abandoned output. A process crash may leave an audio
copy or staging files without a completed receipt; this feature does not
automatically recover or overwrite them.

Existing Studio projects, original scripts, fingerprints, completed artifact
receipts and measured timelines remain unchanged. A tagged copy is a separate
explicit export; it does not mutate the completed Studio job.

## Required qualification and limits

The donor source and handler suites were executed before the later integration:
56 cases passed using actual local FFmpeg. Eight initial publication-boundary
cases then passed against an earlier core correction; their baseline had six
failures and two passing negative controls. These are separate historical
component results, not a passing result for the final reconstructed candidate.
The final shared-resource, descriptor, lifecycle and cleanup composition below
is **NOT_RUN** until its exact source and installed gates execute.

The existing tests/audio/test_metadata.py suite covers MP3, FLAC, M4A and M4B,
both original synthetic PNG and JPEG covers, keep/removal, all eleven portable
fields, changed inputs, corruption, cancellation, no-clobber and the original
Studio receipt. Its actual two-scene M4B remains replayable from the original
completed job without another render. The actual two-frame APNG negative
fixture remains: the bounded PNG framing check rejects animation before admission.

The existing tests/ui/test_audio_metadata_dialog.py cases retain the actual
handlers and worker contract. They check that widgets are never read on the
worker, require a current review and unchanged library/destination authority,
and preserve saved results if Close arrives after publication.

Two additional required suites cover the corrected boundaries:

- tests/audio/test_metadata_publication.py: real no-clobber links followed by
  OSError or KeyboardInterrupt, staging cleanup failures, equal-byte foreign
  inodes, changed linked output, and substitutions during final cleanup.
- tests/ui/test_audio_metadata_lifetime.py: the actual shared policy during
  inspection/review/save, denied admission, retained native-cleanup ownership,
  truthful resource-release notices, and actual handler closures after direct
  or parent-driven dialog destruction, including nested file-pickers.
  The injected cleanup-uncertainty case does not claim a native process-kill test.

The installed Windows command is:

```text
python -I -B -X utf8 scripts/windows_audio_metadata_smoke.py --audio-evidence PATH
```

It uses the existing reviewed external FFmpeg/ffprobe evidence, exact installed
module bytes and a held native bootstrap handle. It never adds test audio tools
to the sealed application or transfers them with an artifact. A normal MainLoop
and owned foreground/focus/key-release checks inspect a real synthetic M4B,
edit the native title field, select a cover, review, change the field again,
re-review and explicitly save. Independent readback verifies original inputs,
encoded audio, chapters, cover and the adjacent receipt. Only three synthetic
file-picker path results are substituted.

The same native gate also completes a real inspection in the existing Desktop
worker and holds its result until after direct or native-parent window deletion.
It requires exactly one cached-ID destruction event, a deleted native wrapper
and delivery of the real completion without a late view update. The held
completion is an explicit scheduling fixture; no native destruction event or
operation result is fabricated. A watchdog, faulthandler trace and owned cleanup
bound failure. This native result remains **NOT_RUN** until the exact Windows
artifact executes.

These checks do not qualify manual keyboard navigation, NVDA, third-party tag
players, artwork quality, acoustic playback or dictionary/model assets.

Format references: [FFmpeg explicit stream mapping and attached pictures](https://ffmpeg.org/ffmpeg.html),
[FLAC muxer](https://ffmpeg.org/ffmpeg-formats.html#flac),
[streamhash](https://ffmpeg.org/ffmpeg-formats.html#streamhash).
