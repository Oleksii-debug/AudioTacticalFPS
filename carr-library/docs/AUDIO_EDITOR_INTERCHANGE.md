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
publication, checksum receipts and completion checks.

## Qualification

Source review covers the bounded serializer and narrow existing-renderer integration.
The focused gate executes actual FFmpeg production and source/installed regressions
on Windows and Linux. Its separate Linux probe builds and calls the primary
[libcue 2.3.0 source](https://github.com/lipnitsk/libcue/tree/cfb98a060fd79dbc3463d85f0f29c3c335dfa0ea)
and reads actual published CUE bytes, Unicode and more-than-99 chapter sheets.
It retains exact source, installed package, probe, fixture and parser-binary hashes.

The completed component [run 46121179440](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/46121179440)
qualified exact commit `3c56c6c4489d2a5da7c3a9ebd3f9b5803a8b817e`:

| System | Source tests | Isolated installed tests |
| --- | ---: | ---: |
| Windows 2022 | 342 PASS | 342 PASS |
| Ubuntu 24.04 | 342 PASS | 342 PASS |

Each context ran the same 17 nonempty suites with no failed, errored, skipped or
expected-failure cases. The gate checked 15 named module origins and all 408 package
Python files, with selected test/helper identities retained before and after execution.
The [complete component evidence](https://api.github.com/repos/Oleksii-debug/AudioTacticalFPS/git/blobs/f8befd32c9b070f11349dfa41faac7440ee5b681)
retains the exact counts, identities and raw logs.

In that run, Linux built the pinned libcue source and passed all six upstream
self-tests. The actual reader then parsed the published two-track sheet from a
real completed FFmpeg production and an authored 100-chapter boundary fixture
split into sheets of 99 and one track. Exact 75 Hz indices, Unicode title behavior,
source and parser identities, and unchanged production artifact bytes were checked.
The authored boundary fixture is distinct from the two-track rendered production.

These results qualify that exact component. Parsing with libcue does not establish
a GUI editor import, acoustic quality, physical output, NVDA acceptance or a later
combined application head. Those results are recorded separately.

libcue is a test-only independent reader and is not shipped, linked or loaded by
the application. Its original license remains with its pinned test source.
