# Portable audio editor interchange

New completed Studio renders publish standard PCM/WAVE audio, requested stems,
the original project, measured timeline, subtitles and chapter CUE sheets through
the same checked artifact publisher. No external editor, plugin or model is
started or installed by rendering.

## Open the output

1. In Studio, render the project to a new output directory. The completion message
   gives that directory. Reopen a saved completed job to inspect its destination.
2. Import master.wav into the chosen editor. To work with separate tracks, import
   the requested stems at time zero. They use the full master timeline; the
   existing project and timeline record track processing and pre-ducking music.
3. Where the selected editor or reader supports audio CUE files, open
   chapters-0001.cue with master.wav beside it. Productions with more than 99
   chapters have additional numbered sheets, each referring to the same master.
   Sheet track numbers restart at 01; absolute positions never restart.
4. Read editor_interchange in timeline.json for exact file/track-to-scene bindings,
   availability and title fallback notices. Keep project.json and timeline.json
   with the audio. They retain original titles, source text and measured times.

CUE uses 75 index frames per second. Markers are rounded down, moving by less
than 1/75 second; they are navigation markers, not sample-exact edit decisions.
Adjacent chapter starts closer than that timebase can be unrepresentable. In that
case audio completion remains valid, no partial CUE set is emitted, and the
timeline gives an explicit unavailable reason. The exact measured timeline remains
available. CUE export has a 10,000-chapter inspection budget.

Titles use UTF-8 with a bounded safe single-line representation. A title containing
a quote, backslash, control character or more than 240 UTF-8 bytes gets an explicit
numbered CUE label. Original titles are never rewritten. No path from a title or
source text becomes a CUE FILE command: sheets reference only master.wav.

## Compatibility and retained work

A standard WAV/stem/CUE set is an interchange package, not a proprietary editor
session. It does not claim to preserve editor plugins, automation, native project
files or editor-specific behavior. Some editors do not import CUE; import WAV
and use the measured timeline there. Do not rename CUE files to an editor project
extension. Completed jobs from earlier versions remain valid and are not silently
rewritten to add new artifacts.

The serializer reuses the synchronized-publishing validator for the exact
project/revision, original source hash, scene/line identities and measured coverage.
New files use ProductionService's existing cancellation, output ownership, atomic
publication, checksum receipts and completion checks. Completion replay recomputes
the expected CUE bytes and requires each corresponding public receipt; a changed
file is rejected even if its receipt was rehashed.

Private CUE generation uses the existing reserved pending-file namespace and
removes its own staging files after successful publication. The retained project
and measured timeline carry the declaration. Both the native composite backup and
accepted legacy flat backup retain and validate that passive metadata against its
exact project checkpoint. Restore requires a new output selection before a job
can run. Backup and restore do not read public audio, start a renderer or claim
that CUE parsing has been qualified. The existing private-state inventory rules
are unchanged; arbitrary unknown CUE files do not gain an exclusion rule.

Resource admission includes bounded CUE payloads and declaration copies alongside
the existing audio estimate. Each sheet is limited to 64 KiB; the complete report
is bounded by the 10,000-chapter limit.

## Qualification

Source review covers the bounded serializer and narrow existing-renderer integration.
The focused gate executes actual FFmpeg production and source/installed regressions
on Windows and Linux. Its separate Linux probe builds and calls the primary
[libcue 2.3.0 source](https://github.com/lipnitsk/libcue/tree/cfb98a060fd79dbc3463d85f0f29c3c335dfa0ea)
and reads actual published CUE bytes, Unicode and more-than-99 chapter sheets.
It retains exact source, installed package, probe, fixture and parser-binary hashes.

Current runtime state must be taken from the exact completed gate, not this
implementation description. Until that gate completes, runtime qualification is
NOT_RUN. Parsing with libcue does not establish a GUI editor import, acoustic
quality, physical output or NVDA acceptance. Those results are recorded separately.

libcue is a test-only independent reader and is not shipped, linked or loaded by
the application. Its original license remains with its pinned test source.
