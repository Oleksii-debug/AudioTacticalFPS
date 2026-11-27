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

The current native Studio format selector and `studio render --format m4b` use
the same ProductionService path. The lower-level `AudioRuntime.audiobook()` API
accepts explicit title and measured chapters. Artist/genre/year editing and
cover-art selection/embedding are still open parts of the finite workflow
inventory; this change does not claim them.

## Evidence

`tests/audio/test_audiobook.py` and `tests/studio/test_audiobook_publication.py`
passed 14 actual cases at local `e6477ce`: real AAC encoding/remux, ffprobe chapter
readback including Unicode/newline/FFmetadata-special-character titles, actual
PCM decoding, no-clobber, changed source after encoding, cancellation, invalid
metadata, measured LRC and real Flite/Studio completed-receipt reuse. The tests
are required in the Windows source and installed selections. Native and human
acceptance remains tied to the exact CI artifact and separate reviewer record.

Format reference: [FFmpeg metadata documentation](https://ffmpeg.org/ffmpeg-formats.html#Metadata),
reviewed 2026-11-27. Existing FFmpeg native-package rights and distribution
clearance are unchanged by adding metadata through its reviewed runtime.
