# Local audio batches and saved-book delivery

## Available workflow

Open Audio Studio, then **Production → Audio batches and audiobooks**. The
dialog uses the existing Studio worker, cancellation owner and task observer.
The same durable service is available through the main application CLI.

Select ordinary local audio files, ordinary local video files, or saved Studio
book/narration projects. Each item has an explicit title, preset label, output
format, sample rate, channel count, bitrate, duration allowance and worker
timeout. A media item also has a numeric audio-stream index: 0 means the first
audio stream, 1 means the second. The service never selects another stream if
the requested one is missing.

For a saved book, choose one whole programme or one file per existing Studio
chapter. Chapter boundaries come from the existing producer's verified sample
positions. Arbitrary media is not automatically divided into invented chapters.
Different selected books can use different delivery presets and split choices.

Choose **Create retained plan**, review the saved exact names and source
identities, then choose **Run**. Creation captures inputs and saves the plan; it
does not start conversion or speech synthesis. Saved settings are immutable.
Choose **New plan** to change them; the previous queue and completed outputs
remain available. Unsaved Studio draft edits are not part of a book selection.

The native controls are named and keyboard reachable, with a read-only exact
preview and visible status. The automated Windows probe exercises actual native
focus, preset editing, plan creation, pause/resume, verified FLAC conversion and
completed replay. It does not claim a manual NVDA, file-picker or physical audio
device session.

## Formats and explicit local runtime

Input extensions are deliberately limited to reviewed demuxers:

| Kind | Local containers |
| --- | --- |
| Audio | WAV, FLAC, MP3, M4A, M4B, AAC, AIF, AIFF, OGG, OPUS, WMA |
| Video | MP4, M4V, MOV, MKV, WEBM, AVI |
| Saved book | An existing managed Studio project in this library |

Output formats reuse the current FFmpeg formats: WAV, FLAC, MP3, Opus, M4A,
M4B, AIFF, Ogg Vorbis, AAC and WMA. WAV/FLAC/AIFF use their fixed current
lossless codecs. Other formats use their fixed current codecs and the explicit
bitrate. Opus and WMA presets require an explicit 48,000 Hz rate. Channel count
is one or two. Other sample rates must belong to the existing Studio rate list.

Select the local FFmpeg executable explicitly. This queue does not download a
runtime, search PATH for helpers, invoke ffprobe, or add another process owner.
It records the executable SHA-256, actual version line and version-output
SHA-256. Run/resume requires those same bytes. To change the selected runtime,
create another reviewed plan. The focused Windows gate provisions only the
project's existing pinned FFmpeg 9.1 archive and checks the installed executable
against its existing byte pin; the original publisher license and source
provenance remain documented in [FFMPEG_RUNTIME.md](FFMPEG_RUNTIME.md).

URLs, network shares, playlists, concat descriptions and unreviewed extensions
are refused. FFmpeg receives a fixed argument vector, one exact audio-stream
map, a fixed demuxer, a file-only protocol whitelist and disabled video,
subtitle, data, metadata and chapter copying. MOV/MP4 references are checked
without following them; external data references and reference/compressed
movies are refused. The fixed MOV options disable external tracks and absolute
data-reference paths. Their upstream meaning is documented in the
[FFmpeg MOV demuxer documentation](https://ffmpeg.org/ffmpeg-formats.html#mov_002fmp4_002f3gp).

## Originals, outputs and interruption

A queue contains independent copies of its selected media. Moving or deleting
an external original after capture does not invalidate the retained input.
Neither selection nor conversion edits the external original.

For books, the queue retains the exact saved Studio document, original draft
and source reference, plus the existing retained clip WAV assets. It creates a
managed child project only when the book is run. The child preserves the
original source/draft and clears the old external output destination. Existing
Studio planning, voice registry, effects, master render, source identity and
audio.render admission remain in use.

Names contain the explicit item position, a portable form of its title, and
the full item identity. Chapter outputs also contain the chapter number.
The preview is deterministic within the saved plan. Two items with the same
title do not silently replace each other.

Every fresh output is encoded and then actually decoded before completion.
The receipt records the exact source, preset/item, plan and runtime identities,
output bytes and SHA-256, and measured decoded sample rate, channel count and
frame count. Lossy output is not claimed to be sample-identical to the source.
The selected duration is an upper bound: overlong input is refused, rather than
accepted as an unnoticed truncated result. Decode verification allows one
second of codec padding.

A complete item directory and receipt are published with the existing atomic
no-replace operation. An occupied or malformed final directory is an error;
it is never silently overwritten. Cancellation received after that atomic move
does not undo a committed verified item.

| Action | Durable behavior |
| --- | --- |
| Pause | Records an intent; the worker acknowledges it after owned native work settles. |
| Cancel | Stops remaining work and retains completed items and source captures. |
| Resume | Explicitly continues paused, cancelled or interrupted items; completed receipts are verified and reused. |
| Retry | Retries failed items, optionally limited to selected failed IDs. Other completed items retain their bytes and timestamps. |
| Export | Copies one verified output to an explicitly selected new filename. Existing files are refused. |

The shared Tasks view reads typed metadata and can request pause/cancel.
Its displayed checkpoint is not a new verification of every output file.
Resume, retry and per-item settings stay in the queue workflow.

Each fresh item captures the existing audio.render permission lease. A lease
already admitted for one item may settle after expiry; the next fresh item
requires a new permission decision. Reusing verified completed data and
exporting it remain available. A nested queue cannot replace the host's selected
TrustController.

Processing uses the existing resource broker and owned FFmpeg process runner.
The reservation stays live until native cleanup confirms quiescence. Per-worker
time, output, diagnostic, allocation and file limits remain active, and a
cumulative staging check bounds all chapter files against the reserved item
disk allowance. Interrupted typed derived scratch can be removed on explicit
resume; unknown committed state and final outputs are never treated as scratch.

## Headless use

Use an existing explicitly selected library directory and an explicitly
selected FFmpeg path for creation or new work. A selection file has this shape:

~~~json
{
  "schema": "carr-audio-batch-selection/1",
  "items": [
    {
      "kind": "audio",
      "source": "C:\\Media\\original.wav",
      "title": "Lecture delivery",
      "audio_stream": 0,
      "preset": {
        "label": "Speech delivery",
        "output_format": "mp3",
        "sample_rate": 48000,
        "channels": 1,
        "bitrate_kbps": 192,
        "split": "whole",
        "max_seconds": 7200,
        "timeout_seconds": 3600
      }
    }
  ]
}
~~~

For video, use kind video and the explicit selected audio stream. For a saved
book, use kind book, the existing 32-character managed project ID as source,
stream 0, and split whole or chapters. A queue allows at most 128 items.
Input files are bounded at 8 GiB; metadata, inventory, stage and native process
allowances are enforced separately. The per-item duration and worker timeout
are positive numbers no larger than 86,400 seconds. These are acceptance
limits, not throughput or resource-performance promises.

~~~powershell
research-library --data-dir C:\Library --audio-batch-plan C:\Plans\selected.json --ffmpeg C:\Tools\FFmpeg\ffmpeg.exe
research-library --data-dir C:\Library --audio-batches
research-library --data-dir C:\Library --audio-batch QUEUE_ID --audio-preview
research-library --data-dir C:\Library --audio-batch QUEUE_ID --audio-batch-action run --ffmpeg C:\Tools\FFmpeg\ffmpeg.exe
research-library --data-dir C:\Library --audio-batch QUEUE_ID --audio-batch-action pause
research-library --data-dir C:\Library --audio-batch QUEUE_ID --audio-batch-action resume --ffmpeg C:\Tools\FFmpeg\ffmpeg.exe
research-library --data-dir C:\Library --audio-batch QUEUE_ID --audio-batch-action retry --audio-batch-item FAILED_ITEM_ID --ffmpeg C:\Tools\FFmpeg\ffmpeg.exe
research-library --data-dir C:\Library --audio-batch QUEUE_ID --audio-batch-action export --audio-batch-item ITEM_ID --audio-batch-file EXACT_OUTPUT_BASENAME --audio-batch-export C:\Exports\new-output.mp3
~~~

Replace the placeholder IDs and exact output basename with the saved preview.
The CLI returns 0 for successful reads, saved plans, controls and complete work;
1 for a failed queue; 130 for an acknowledged paused/cancelled run. A pause or
cancel command records a request and reports acknowledged=false until the
producer has checkpointed it. Ctrl+C cooperatively stops owned work and retains
completed items.

## Complete backup and recovery

audio_batch_backup is a typed adapter for the existing complete-library backup.
The common wiring is delivered separately in component
d31d88a0563f58fc9b117933c569225dbee9fadd and must be composed with this feature.
The adapter alone does not make another backup implementation.

The existing library writer and the queue namespace lock prevent a backup from
taking a live mixed snapshot. The manifest retains the exact source captures,
typed plan/control files, verified completed outputs and the managed Studio
children. Pending book source references are checked against the captured
catalog even before a child has been created. Unknown state is refused.

A file-only backup may omit an empty outputs directory. That represents no
completed outputs; explicit resume reconstructs the owned empty directory.
A completed item with missing output bytes or receipt still fails validation.
Restore never automatically starts speech, media conversion or an executable.
Select the original runtime bytes and resume explicitly.

## Qualification contract

The feature checkpoint d1277d92744a01865ad51b58ab511d03b446f8d8 is a clean
descendant of 947754a6b1b73baa6e1c8d95cf39fee81c96321a. It contains 142 static
cases; this qualification supplement adds the cumulative reserved-disk
regression, bringing the exact declared population to **143**.

The focused workflow is .github/workflows/carr-audio-batches.yml. It runs on
the carr/v3-acceptance-* and carr/audio-batches-* branches, or by explicit
workflow dispatch. It keeps frozen R6/R7 snapshots unchanged.

| Required lane | Exact declared population |
| --- | ---: |
| Source tests, using the exact checked-out application | 143 |
| Installed tests, isolated Python and a tests-only staging directory | 143 |
| Installed native Windows controls plus actual FFmpeg conversion | 26 labeled checks |

scripts/audio_batch_oracles.json contains the complete function-to-multiplicity
and native-label Counters. The suite includes all ten actual encoded output
formats, actual two-stream local-video extraction, pause/cancel/retry/replay,
runtime/source changes, late publication and cleanup ownership, signed
admission, CLI behavior, shared task controls, and common backup/restore/rebackup.

The gate builds and verifies the application wheel against the exact Git
commit, installs the existing 53-pin qualification runtime without compiling
application bytecode, and compares every installed Python file with the wheel.
It keeps the current 56-pin full-platform workflow unchanged. Test subprocesses
reuse the existing offline pytest policy and case watchdog. Their application
origins and complete Python inventories are checked before and after execution.
The installed lane adds only copied test files to its import path.

Actual JUnit function Counters must match the oracle exactly, with zero
failures, errors, skips, XFAIL or XPASS. The native report must match all 26
labels, eight exact installed module origins and the selected FFmpeg identity.
Timeouts, missing reports, partial runs and changed byte identities are FAIL.

The job is bounded at 75 minutes; each source/installed test process has a
1,200-second deadline and a 22-minute step allowance. The native probe has a
180-second watchdog and a five-minute step allowance. Raw textual reports are
uploaded on every outcome; synthetic media, private inputs and executable/model
payloads are not uploaded.

**Authoring status: NOT_RUN.** This component was prepared and independently
source-reviewed while the local executor was unavailable. Compilation,
collection, actual source/installed tests and native execution must be recorded
by the composed candidate's CI run. Earlier R6/R7 or owner results are not
transferred to this component. No manual NVDA, physical microphone/playback,
speech-quality or general external-player conformance claim is made.
