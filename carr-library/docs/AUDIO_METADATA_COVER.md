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
size, temporary disk use, process time and captured output.

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

Existing Studio projects, original scripts, fingerprints, completed artifact
receipts and measured timelines remain unchanged. A tagged copy is a separate
explicit export; it does not mutate the completed Studio job.

## Evidence and limits

The required tests/audio/test_metadata.py suite contains 39 cases. Actual FFmpeg
tests exercise MP3, FLAC, M4A and M4B, both original synthetic PNG and JPEG covers,
existing-cover keep/removal, all eleven portable fields, source/cover mutation,
corruption of an encoded output, cancellation, destination replacement,
no-clobber and committed receipt warnings. An actual two-scene Studio M4B render
is tagged and then replayed from its original completed receipt without launch;
its chapter timing, source bytes, project fingerprint and artifacts are verified.
An actual two-frame APNG is refused: fixed png_pipe decoding alone can hide the
animation chunks, so bounded PNG chunk framing also verifies the still-image
contract before admission.

The required tests/ui/test_audio_metadata_dialog.py suite contains 17 cases. It
executes the actual named handlers with the existing worker contract and real
FFmpeg, checks that widgets are never read on the worker, rejects stale review/
library/destination authority, and checks close both before launch and after
publication.

The installed Windows script scripts/windows_audio_metadata_smoke.py uses a
normal MainLoop and owned foreground/focus/key-release checks to inspect a real
synthetic M4B, edit the native title field, select a local cover, review, change
the field again, re-review and explicitly save. It verifies exact installed
module and pinned FFmpeg/ffprobe identities, original source/cover/PCM bytes,
encoded audio and chapters, then independently reads the saved receipt/output.
Only three synthetic file-picker path results are substituted. A 120-second
watchdog, faulthandler trace and owned modal/worker cleanup bound failure. The
script's native result remains NOT_RUN until the exact Windows CI artifact is
executed; its existence is not a passing native result.

These portable source/handler results do not qualify human keyboard navigation,
NVDA, third-party tag players, artwork display quality or a native Windows
artifact. Those require their own exact artifact evidence.

Format references: [FFmpeg explicit stream mapping and attached pictures](https://ffmpeg.org/ffmpeg.html),
[FLAC muxer](https://ffmpeg.org/ffmpeg-formats.html#flac),
[streamhash](https://ffmpeg.org/ffmpeg-formats.html#streamhash).

