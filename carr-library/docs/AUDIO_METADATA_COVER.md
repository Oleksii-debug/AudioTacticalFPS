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

## Saved choices and backup

Each successful Review saves the latest portable tag choices, original name and
byte identity, intended output basename, and the exact reviewed replacement
artwork in one bounded passive library record. It contains no external absolute
paths, output approval, native ownership, worker command or replay permission.
The ordinary full/incremental backup validators check its exact schema, bounded
cover encoding and hash; unknown fields, changed artwork or an execution grant
are rejected.

**Load saved tag choices for a new review** restores editable fields only.
It does not select the original file, set a destination, inspect media or enable
Save. If the review includes artwork, **Export saved artwork to a new file**
makes an explicit no-clobber copy outside the private library. Select that copy
as a cover and inspect/review the desired audio again before saving. This also
works after a backup is restored and the earlier external inputs are absent.
Only the latest review is retained; this is a passive preference/evidence
record, not another job queue or a recovery permission for abandoned exports.

Book Audio users can reach the same action through Audio Studio → **Review
audio metadata and cover** after their MP3 or M4A/M4B completes. The Book dialog
also displays this route. Select the completed file and write a separately named
copy. The original Book/Studio receipt, chapter mapping and cached render remain
unchanged; the new metadata receipt identifies the derived file.

## Required qualification and limits

This component reuses the prepared metadata implementation at
`c8baf30c2cf85a6d0683ff485795509244bf76d5`. Its earlier results do not qualify this
composition. The current source, installed and native composition is **NOT_RUN**
until the exact gates execute.

The original actual-FFmpeg suites are preserved:

- `tests/audio/test_metadata.py`: MP3, FLAC, M4A and M4B, PNG/JPEG replacement,
  keep/removal, all eleven portable fields, source/cover mutation, malformed and
  animated covers, cancellation, no-clobber, packet hashes and the original
  Studio receipt/completed-job replay.
- `tests/audio/test_metadata_publication.py`: real no-clobber links followed by
  late writer errors, private staging cleanup failures, equal-byte foreign inodes,
  changed published bytes and substitutions during final cleanup.
- `tests/audio/test_metadata_stream_hash.py`: supported streamhash contracts,
  malformed/ambiguous output and both reviewed FFmpeg output generations.
- `tests/ui/test_audio_metadata_dialog.py` and
  `tests/ui/test_audio_metadata_lifetime.py`: actual handler/worker boundaries,
  frozen choices, exact source/destination review, real resource policy,
  late completion and destroyed-view cleanup.

New retained ownership is **opt-in for metadata only**. Other producers retain
their existing process contract, and microphone admission/Windows Job cleanup
is unchanged. `tests/audio/test_metadata_cleanup_scope.py` exercises a real
child holding a private file while stop acknowledgement, diagnostic-reader
startup or cleanup-thread construction is deliberately refused. The exact
private stage and actual broker reservation must survive until the owned child
and diagnostic readers have stopped; competing admission remains refused.

`tests/audio/test_metadata_review.py` covers actual reviewed-media retention,
full backup/restore with original inputs removed and engine calls forbidden,
explicit artwork export, unchanged no-clobber bytes, tampered schema/artwork
refusal, cancellation and byte bounds before decoding. Added native-handler
cases prove loaded choices restore neither input paths nor Save approval.

The installed Windows command is:

```text
python -I -B -X utf8 scripts/windows_audio_metadata_smoke.py
```

It holds the existing native bootstrap handle, requires exact installed module
bytes from the reviewed checkout, and uses the current configured AudioRuntime.
Inside a sealed bundle, each actual executable must match the bundle path and
manifest hash. An optional `--audio-evidence PATH` can instead bind an explicit
external CI tool preparation report; neither mode downloads artwork or engines.

The real wx MainLoop and foreground/key ownership exercise inspection, native
title edits, explicit cover selection, invalidation/re-review and Save using a
synthetic M4B. Independent FFmpeg readback checks unchanged original inputs,
encoded packets, chapter boundaries, compressed cover bytes and adjacent receipt.
The same gate delivers a real completed inspection after native owner deletion,
testing both direct and parent-driven destruction. Only selected file-picker
paths and the deliberate completion barrier are fixtures.

These gates do not qualify physical listening quality, NVDA, artwork quality,
third-party player compatibility or distribution rights.

Format references: [FFmpeg explicit stream mapping and attached pictures](https://ffmpeg.org/ffmpeg.html),
[FLAC muxer](https://ffmpeg.org/ffmpeg-formats.html#flac),
[streamhash](https://ffmpeg.org/ffmpeg-formats.html#streamhash).
