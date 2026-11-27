# Measured M4B chapters and line LRC

New Studio productions selected as `m4b` embed the project's title and actual
scene titles and chapter boundaries in the M4B container. The existing local
FFmpeg adapter encodes the measured master once, then copies its audio stream
while adding FFmetadata. Titles remain data, with documented escaping; source
paths, titles and project text never become shell commands or raw filter options.

The application checks the resulting container using its selected ffprobe:
chapter count, every title, boundaries, audio sample rate/channels/duration and
output size must agree before atomic no-clobber publication. Container chapter
rounding is accepted only within one millisecond or one output sample. The
original audio is checked again by content hash. Cancellation, changed inputs,
changed output directories, invalid plans and existing destinations do not
publish a replacement. Temporary encoded and tagged files are removed after
success or failure. The existing runtime budget and local-input restrictions
remain in force.

Chapter plans must be ordered, contiguous and cover the measured master. Cache
identity includes the title and complete chapter plan, so changing a chapter
name cannot silently reuse an older tag set. Already completed jobs keep their
existing validated artifacts; create a new production to request the new output
instead of silently changing a completed receipt.

Every new Studio production also includes `script.lrc` beside the existing SRT
and VTT. LRC contains measured narration-line start times rounded to hundredths
of a second. Multiple visual lines within one spoken line share its start time;
no internal word timing is inferred. The retained original script, exact line
text and full measured intervals remain in `project.json` and `timeline.json`.
Player-specific LRC interpretation and third-party audiobook interoperability
require separate qualification.

When a visual lyric row's first non-whitespace character is `[`, the LRC export
adds one invisible U+2060 WORD JOINER before the complete, otherwise untouched
row, including any leading spaces or tabs. LRC permits repeated timestamp tags:
without this boundary, literal text such as `[10:00.00]` could create another
event at ten minutes and disappear from the displayed words. No backslash
escaping rule is assumed. Ordinary rows receive no added marker, and multiple
visual rows retain the same measured narration start.

The rendered `timeline.json` records this transformation in `lrc_notice`.
Consumers may remove the export-added marker **after** parsing LRC, using the
retained timeline text to distinguish it from any original WORD JOINER. Removing
it before parsing restores the ambiguous syntax. The original script, saved
spoken-line text and timeline text are unchanged. The actual FFmpeg LRC demuxer
is tested for one event per visual row at the measured time and exact UTF-8
packet text, including bracket literals, leading whitespace and multiple rows.
This qualifies that parser contract; display and parsing in other LRC players
still require separate testing.

The current native Studio format selector and `studio render --format m4b` use
the same ProductionService path. The lower-level `AudioRuntime.audiobook()` API
accepts explicit title and measured chapters. The separate reviewed
[metadata and cover workflow](AUDIO_METADATA_COVER.md) now edits eleven portable
tags and a selected local PNG/JPEG on a new MP3/FLAC/M4A/M4B copy. It verifies
the original encoded audio and chapters without changing the completed Studio
job, project fingerprint or original artifact receipts.

## Evidence

`tests/audio/test_audiobook.py` and `tests/studio/test_audiobook_publication.py`
passed 14 actual cases at local `e6477ce`: real AAC encoding/remux, ffprobe chapter
readback including Unicode/newline/FFmetadata-special-character titles, actual
PCM decoding, no-clobber, changed source after encoding, cancellation, invalid
metadata, measured LRC and real Flite/Studio completed-receipt reuse. The tests
are required in the Windows source and installed selections. Native and human
acceptance remains tied to the exact CI artifact and separate reviewer record.

The literal-text follow-up adds four real ffprobe cases. Both suites passed
18 cases with no skips on Linux using FFmpeg/ffprobe 7.1.5: bracket timestamps,
leading spaces/tabs, metadata-looking Unicode text and multiple visual rows
retain their expected packet counts, measured starts and exact UTF-8 payload
hashes. This source result does not substitute for the required Windows and
installed-bundle runs.

Format reference: [FFmpeg metadata documentation](https://ffmpeg.org/ffmpeg-formats.html#Metadata),
reviewed 2026-11-27. Existing FFmpeg native-package rights and distribution
clearance are unchanged by adding metadata through its reviewed runtime.

LRC grammar reference: [FFmpeg LRC demuxer source](https://ffmpeg.org/doxygen/6.1/lrcdec_8c_source.html),
reviewed 2026-11-27. The literal-text regression uses the selected installed
ffprobe rather than assuming all players implement this grammar identically.
