# Resonance V3 implementation and owner-test delivery status

This document describes the implemented library, reading and audio preview of
Resonance by Sceneria Innovative Technologies. It does not close the complete
platform specification or the finalization addendum. An owner-test application
bundle and a signed distribution-cleared release have separate acceptance
requirements. Existing package, launch filenames and data locations remain
unchanged until a verified migration; visible application titles use Resonance.

## Implemented and exercised

| Area | Implemented behavior | Evidence and limits |
| --- | --- | --- |
| Library and OCR | Scoped originals, exact quotations, advanced Boolean/NEAR search, BibTeX and DOI/ISBN workflows, additional document formats, managed Fast/Best OCR | Actual SQLite/parser/import/recovery tests; externally downloaded PDFs bind import and resume to their exact verified receipt; native acceptance is recorded per build |
| Reading and voices | Source-preserving reading derivatives, reviewed whole-copy/selected-text pronunciation rules with dictionary save/import/export and undo, local voice discovery, playback, SAPI5 native COM worker and explicitly configured Dordiai adapter | Real Windows SAPI PCM with outbound network blocked; local Flite rendering; other engine assets require separate qualification |
| Audio studio | Roles/scenes, reviewed pronunciation derivatives for selected or whole spoken lines with undo, versioned named clips with trim/placement/gain/pan/fades, microphone capture, pauses, procedural score, DSP, aligned stems, measured M4B chapters/LRC, subtitles and verified resume | Actual Flite/FFmpeg rendering, ffprobe chapter/text checks, stop/cancel, clip/recording composition and destructive recovery; original script and non-spoken clips remain intact; physical microphone, listening, other players and native evidence remain per build |
| Documents and citations | Isolated conversion, local citeproc-js, retained citation DOCX regeneration, exact custom style, guarded bibliography exports and opt-in DOI previews | Actual Node/DOCX regeneration, immutable source snapshot, final content recheck and no-clobber tests; native regeneration is separately gated; no live Word add-in |
| Synchronized publishing and reading | EPUB3 chapters/navigation/MP3/line-level SMIL and DAISY 2.02 NCC/XHTML/SMIL file sets from an exact completed Studio job; retained EPUB overlays play through Reading Desk with segment navigation and source reauthorization | Actual FFmpeg/package/no-clobber/recovery/playback-boundary tests; three official EPUBCheck cases passed without errors/warnings, four DAISY documents passed pinned DTDs; exact-build native probes, third-party readers, word alignment, Braille, listening and WCAG/NVDA qualification remain separate |
| Recovery | Manifest-bound backup v4, conditional v5 for reading state, exact supported database schemas through v5, pending import bytes, bookmarks/resume, Studio inputs and transcription state; portable collision checks; transcription admission/run locks prevent snapshots from disrupting active workers | Real deletion/restore/resume and active/paused-job exclusion tests; archive format and database schema are distinct identities; external engine approvals require explicit rebind; external exports and OS credential secrets remain separate |
| Transcription | Supplied reviewed Whisper worker through one service, native dialog and CLI; explicit file/folder intake, retained source, four output formats, revision editing and resume; Windows live progress remains indeterminate until verified worker exit | Actual offline tiny.en CPU inference, two-file folder execution and destructive recovery with synthetic English speech; other languages/models, diarization, word alignment and human Windows/NVDA quality remain separate |
| Keyboard and progress | Configurable menu keys and searchable command palette with current command/scope validation, named controls, measured or indeterminate progress, Off/Beep/Speech/Beep+Speech, explicit local voice and playback suppression | Real private PCM synthesis and bounded feedback-worker tests; terminal notices wait for obsolete speech to stop; human NVDA listening remains required |
| Tasks and resources | One native task view and CLI over the existing Studio/transcription journals; shared cross-process CPU/RAM/GPU/disk/device admission for Studio, ASR and recording; reviewed resource-policy editing and attempt-bound ASR controls | Actual subprocess lock/crash and control tests, real rendering and offline Whisper with an observed lease; declared allowances do not measure or isolate physical resource usage; journal completion is verified by the originating workstation |
| Language inventory | Exact canonical 135 entries, source audit and searchable capability matrix | Resource inventory is separate from acoustic qualification; no claim of 135 working voices |
| Assistant and providers | Typed scoped tools, durable jobs, explicit local-model proposal through the existing gateway, protected credential references and consent/budget | Actual local HTTP protocol and CLI tests use explicitly synthetic responses; proposed plans cannot execute without separate review; installed LLM quality and paid providers remain separate |
| Translation | Optional Google Basic v2 and Advanced v3 selected-text derivatives, directional language inventory, explicit OAuth expiry/renewal, reviewed glossary/model binding, consent/budget, chunk receipts/resume and new exports | Actual gateway/ledger/jobs/UI handler/CLI with synthetic REST responses; live Google IAM/billing, translation quality, whole-document fidelity and local model inference remain separate |
| Delivery preparation | Exact-source wheel checks, pinned dependency/SBOM closure, parent-manifest binding, strict paths and sealed runtime files | Source/packaging tests; checksum integrity is not a publisher signature |
| Private integration boundary | Separate versioned entitlement/update protocols and preservation of user-data operations | 45 synthetic boundary tests; no verifier, authority, automatic renewal, production trust or updater is enabled |

The native V3 acceptance script performs installed SAPI synthesis, scene
production, output verification, restart, CSL/DOCX, isolated text conversion and
named native dialogs while the selected runtime executables have outbound
firewall rules. Its evidence explicitly labels manual NVDA as NOT_RUN.

## Requirements still open

The following cannot be labelled DONE by passing the current core tests:

- A signed ResearchLibrary.exe and standard signed installer/uninstall/repair,
  owner-configured publisher trust and a complete private release review.
- Compatible private entitlement authority, account-optional perpetual policies,
  signed unattended refresh and offline renewal, and signed update A-to-B,
  interruption/recovery and key-rotation acceptance.
- Actual owner-supplied cloning runtime, authorized reference recordings,
  protected voice profiles and independent multilingual inference/listening.
- Actual installed dordiai/SAPI4 compatibility, MusicGen/ACE-Step model inference,
  the remaining approved Balabolka-equivalent workflow checklist, third-party
  synchronized reader and full DAISY semantic/interoperability acceptance, and
  Braille workflows. The implemented DAISY exporter has a separate
  [bounded publication profile](STUDIO_DAISY_PUBLISHING.md).
- Independent qualification of every required capability for the canonical 135
  entries; registry/resource counts never establish language quality.
- Human Windows 11/NVDA and listening review. Automated Windows Server native
  execution is useful evidence, but does not substitute for that review.

The supplied Whisper Transcribe V4.3 implementation is corrected and integrated.
The exact private worker `5379b889…` has executed through the service and CLI with
the pinned tiny.en model, including genuine interruption, verified reuse and
same-job recovery after deletion of the original library. This establishes a
bounded English CPU compatibility result, not complete language or audio-quality
qualification. The worker/runtime/model remain explicit external selections and
are not published or automatically bundled. See the
[integration and execution review](TRANSCRIPTION_INTEGRATION_REVIEW.md).

## Exact Windows failure evidence and subsequent corrections

Public source `8bf411718548809793748ed01f458c79f3692e98`
[run 45929429163](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45929429163)
completed with failure. Its actual fresh EXE installation, first native launch,
normal close, second launch with unchanged installation marker, retained
source/search state and installed self-test passed. The full-bundle gate later
failed at an obsolete translation-control label; the recording probe expected
an obsolete live progress-gauge name. These successful substeps are not a full
bundle PASS, and the installed EPUBCheck step was not reached.

The source step completed 175 of 183 reports before its 20-minute deadline:
169 successful suites, six failing suites, 4,126 passed assertions, nine failures
and two skips. The interrupted reading-backup suite is not counted. Subsequent
corrections address Windows live-status replacement, equal-metadata changed
export bytes, owned-voice cleanup fixtures and the Windows absolute-path
fixture. POSIX process/filesystem cases have explicit Linux selection; strict
failure/skip checks remain. The source deadline reflects the measured workload.

This history identifies actual exercised paths and failures. It is not
transferred to later source. New recorded-EPUB/DAISY native probes, installed
EPUBCheck, source suites and final sealed-bundle verification are required for
each composed candidate. See [DAISY evidence](STUDIO_DAISY_PUBLISHING.md),
[recorded EPUB reading](EPUB_RECORDED_READING.md) and the finite
[reading workflow inventory](BALABOLKA_WORKFLOW_ACCEPTANCE.md).

The current FFmpeg packs remain available for reviewed local installation and
execution. Their missing distribution clearance is enforced separately by the
existing transfer-archive helper; runtime tests do not authorize an archive
containing uncleared native payloads.

The private build retains local operation and access to user-owned data. Private delivery does not waive dependency rights, notices or source obligations.
This work does not authorize external distribution, pricing, billing, a public site,
automatic model acquisition or publishing private voice profiles.

## Reviewed reader and command additions

The composed source at local `646a15e` passed 235 focused cases with zero
failures/skips across pronunciation review/handlers/reader hooks, command palette,
keyboard settings, progress ownership, publication handlers and bounded portable
backup paths. The pronunciation workflow independently passed all 82 cases in six
strict offline suites including existing reading, source and recovery tests.
The palette independently passed 269 composed cases; these sets overlap and are
not added together. The earlier import/music/delivery/transcription/backup
composition passed 367 cases on local `8cceedd`. These are source test results,
not actual native keyboard, installed Windows, model quality or human acceptance.

The new Windows probes exercise actual keys through the normal wx event loop,
verify foreground window/process ownership and release paired keys before the
next action. Both source-installation and sealed-bundle jobs require recorded EPUB
reading, DAISY publication, command palette and pronunciation review. Their result
remains NOT_RUN until a corresponding exact-build Windows run completes. See
[command palette](COMMAND_PALETTE.md) and [pronunciation review](PRONUNCIATION_REVIEW.md).

## Composed recovery, resources and spoken-text evidence

After the canonical schema-five/import-publication merge, local `c59556b`
passed 522 distinct cases across 24 selected suites with zero failures/skips.
These cover immutable import publication, existing and previous-format reading
state, pronunciation, exact database/backup identities, portable restore paths,
federated search, resource admission, actual measured M4B rendering, clips and
production cancellation. The three non-overlapping reports contain 193, 259 and
70 cases respectively; earlier overlapping runs are not added to this total.

The final composed application source `504ea2f` passed a further 221-case
focused run with zero failures/errors/skips. This run combines the task facade,
resource broker and policy UI, ASR and microphone admission, Studio pronunciation,
actual audiobook output, retained-input cancellation, worker ownership and Studio
backup. It overlaps earlier runs and is reported separately.

The exact application source `01f893b8d63cba86153f3f90658f313b2f813ec3`
also passed 117 actual private-worker assertions with the reviewed worker,
all 31 pinned runtime distributions and five pinned tiny.en model components.
Both two-file inference runs executed in a Linux network namespace with no
external connectivity. The replay observed a live resource reservation, proved
that a completed job opens while the resource pool is occupied, and restored
the same completed job after deleting the original library and sources.
The restored archive retained the exact resource policy and database schema five;
external runtime access remained blocked until explicit rebind. The second run
exercised the no-live-progress policy branch on Linux, not native Windows.
See the [sanitized exact-source report](evidence/transcription-resource-recovery-01f893b-20261127.json).

Actual FFmpeg/ffprobe tests also verify that literal source text beginning with
brackets cannot create an extra LRC timestamp; source and timeline text remain
unchanged. A separate retained-input cancellation review passed 52 cases,
including four real WAVE files larger than 3 MiB that stop after one 1 MiB read
with no runtime launch or leaked reservation. See [audiobook chapters](AUDIOBOOK_CHAPTERS.md)
and [shared resource behavior](runtime_resources.md).

The preceding composed Windows workflow selected 210 unique source suites and 117 unique
installed suites, including the new Studio pronunciation, Tasks/resources,
ASR admission and microphone admission cases. Real-key Studio pronunciation and
Tasks/resources probes are required for both the installed desktop and complete
bundle. Selection and source checks alone are not Windows execution evidence.

## Package and cursor corrections after the first composed candidate

The explicit setuptools package list omitted the new operations package.
An actual wheel built from `504ea2f` reproduced the omission; the corrected
`37a0dc3` wheel contains all six operations modules. Exact Git/source verification
passed for 313 payload files and 244 required runtime files, and ten relevant
modules imported from an isolated installation of that wheel. A production-package
inventory regression now prevents another declared-package omission. This local
wheel result does not substitute for the composed Windows installer gate.

The native lock preflight also had a release race: the previous owner could
legitimately remove an unowned lock between existence and metadata checks.
The original ASR mutation regression reproduced the failure four times in 100
runs. The corrected preflight passed 100 repetitions and 50 distinct strict
resource/admission/fork cases. Owned descriptor, replacement-link and native
backend checks remain required.

Reader cursor save, restoration and read-from-cursor now convert exact native
text ranges to character offsets, preserving the existing session format.
The explicit clipboard command opens a separate reading draft; it never watches
the clipboard or starts speech automatically. The composed source `c4b548b`
passed 252 cases with zero failures/errors/skips across package closure, resource
admission, cursor/clipboard/session recovery, reader pronunciation and all six
Dordiai fixture suites using importlib isolation. This run overlaps earlier
tests and is reported separately. The preceding 116-suite installed selection
collected all 2,946 cases successfully; collection is not execution of those
cases. The later clipboard selector adds its 13 cases to that installed gate.

Public source `5ba8389a193f1907810b8f00aba2e386993fd7a3` completed the
[academic reconciliation run](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45942462279)
successfully. Its Windows/V3 jobs were queued when these corrections were
prepared, and no Windows pass is inferred from that queue state.

## Reader, playback, conversion and keymap composition

The application source `270ec95` passed 407 distinct cases in 18 focused suites
with zero failures/errors/skips. This composition covers the new Find/Replace
worker and real reader closures, playback navigation and reader lifetime,
actual POSIX ffplay process lifecycle, measured audio conversion, reviewed keymap
interchange and existing keyboard/palette behavior, plus cursor, clipboard,
reading-source and session recovery. Actual Studio M4B rendering remains in the
same run. Earlier overlapping feature runs are not added to this count.

The implemented additions are:

- [Find/Replace](READING_FIND_REPLACE.md): bounded literal/regex next/previous,
  fixed selected-text or whole-copy scope, preview and explicit undoable apply,
  native Unicode position conversion and cancellation of the owned subprocess.
- [Playback navigation](PLAYBACK_NAVIGATION.md): generation-bound skips,
  seconds/percentage seek, explicit A–B repeat and sleep through the existing
  player, with obsolete commands revoked on Stop, edit or source replacement.
- [Audio conversion settings](AUDIO_CONVERSION_CONTROLS.md): explicit sample
  rate, channels, supported PCM depth and compressed bitrate, checked against
  actual output. A post-publication observation failure reports the already
  committed result accurately.
- [Keymap interchange](KEYMAP_INTERCHANGE.md): previewed schema-one merge,
  exact file/current-command checks, no-clobber export and full-map undo through
  existing shortcut preferences. Previously accepted Unicode JSON encodings
  remain readable.

At `270ec95`, the Windows workflow selected 218 unique source suites and 125 unique
installed suites. Required named cases, exact installed module origins and
zero-skip checks remain enforced. Exact `270ec95` collection found all 3,123
installed cases, all 129 required named cases and exactly 58 metadata cases;
collection is not execution of those cases. Existing native reading, audio, publication,
pronunciation and palette gates remain present. New conversion acceptance uses
the actual pinned FFmpeg/ffprobe through the real dialog and worker; its output
must be 48 kHz mono 24-bit PCM with measured frames and unchanged source bytes.
The separate playback-navigation native probe explicitly uses a synthetic
clocked transport and does not claim MCI or physical-device acceptance.

## Failures observed on public candidate b0fbc43

Exact public source `b0fbc43a627cf3e90805ea06fdc94fa986dcf344`
[V3 run 45943636356](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45943636356)
completed with failure. The installed integrity step recorded 307 passes and two
failures without skips: both timeout/PDF publication tests expired before their
required child-process barrier was reached. A three-second fixture deadline
incorrectly depended on cold native parser startup time. The corrected fixture
keeps a real bounded startup deadline, advances only the supervisor's clock
after the actual before/after-commit marker, and retains real child termination,
durable receipt and rollback assertions. Instrumentation is local to the worker
module instead of replacing the shared subprocess class. Source `4f6d9b5`
passed all 58 publication/replay cases with zero failures/errors/skips, including
deadline-arming checks when a child reaches its barrier unusually quickly.

That V3 run's broader source step recorded 1,379 passes, one failure and 22
skips; this is not a strict acceptance pass. The failed actual M4B fixture
assumed a Flite filter absent from the pinned Windows FFmpeg. It now uses the
existing actual native SAPI provider on Windows and actual Flite on POSIX.
Missing voices still fail, and all original output, chapter, LRC, hash and reuse
assertions remain. The ten separately strict progress suites passed. The native
V3 acceptance step was skipped after the preceding integrity failure.

The same source's
[Windows run 45943636357](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45943636357)
reached actual installed native execution. Recorded EPUB reading and
Tasks/resources exceeded their five-minute step deadlines, and the palette and
full-bundle steps also failed. Reader and Studio pronunciation passed. These
failures remain open until their exact diagnostics and corrected candidate
results are available. Periodic thread traces and flushed phase records now
cover the two blocked probes without changing their deadlines or acceptance
assertions. Independent source replay also proved that the old palette fixture
targeted a note on an unselected notebook page; it now explicitly reveals and
asserts the visible Evidence page before the original key sequence. That source
defect alone is not asserted to explain every native failure.

New source results do not establish final installed Windows, human NVDA,
physical-device, listening, model/language, signing or distribution acceptance.

## Native-driver repairs and bounded inventory rechecks

The completed `b0fbc43` job log identifies concrete native test-driver defects.
The Tasks probe changed the reviewed CPU field before its focus helper was ready,
then repeated the prior assertion on its next timer tick. Preparation and the
native key action now have separate phases, and reentrant/late timer callbacks
cannot repeat a phase. The palette called the pinned wxPython simulator's
byte-string API with a Python string; its actual filter-typing call now supplies
ASCII bytes and retains the resulting native-text assertion.

Both failures then remained inside the outer event loop after attempting to exit
an active modal loop. The recorded-EPUB probe had the same abort boundary. Each
probe now retains and schedules the owning main loop to exit, unwinds only its
owned dialogs, preserves the original error and continues cleanup after a key,
timer or dialog cleanup error. The EPUB Unicode/MCI/source assertions are
retained, with immediate original-error and selection diagnostics. Source
`4fbf59c` passed all 46 cases across the three driver/controller suites with zero
failures/errors/skips. These callbacks run against explicit native-control
fixtures locally; actual Windows replay remains required.

The full-bundle run passed fresh EXE installation and two GUI lifecycles before
its document-format probe rejected a duplicated script pathname in `sys.argv`.
All seven existing `runpy` wrappers now remove their bootstrap target argument
before executing it. An actual subprocess/argparse regression verifies Unicode
script paths, retained `--native` arguments and rejection of unknown options.
Native activation, exact-source checks and the original bundle gates remain.

That Windows source run also exposed a real transcription folder-selection gap:
a late file could be missed while directory timestamps remained unchanged.
Source `8aecafb` rechecks the exact bounded entry names after file inspection.
It rejects additions, removals and renames without adding unseen files to the
review. The three unchanged-metadata cases reproduced failure before the fix;
all 48 CLI/native-handler cases then passed with zero failures/errors/skips.
The ordinary submit step still captures and verifies the accepted input bytes.

Reviewed narrow changes from PR166 and PR168 are also composed without removing
the existing transcription snapshot locks or retained Studio-input handling.
Unreadable state subtrees now fail backup/restore; restore stops at the first
unlisted file. Music model/SDK inventories propagate enumeration errors, bound
every entry and observe cancellation, including empty directories and
case-variant Python files. Source `fcf2b43` passed 114 backup/music cases in five
suites with zero failures/errors/skips. Synthetic inventory tests do not establish
actual neural inference or model distribution rights.

Exact public source `4d9804f03159fbdb532839abdef0ca3b1910d685`
[Windows run 45946977181](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45946977181)
passed the actual numeric-conversion native step, the explicitly synthetic
playback-controls step, reader pronunciation and Studio pronunciation. Recorded
EPUB reading failed again and is not accepted. The matching
[V3 run 45946977206](https://github.com/Oleksii-debug/AudioTacticalFPS/actions/runs/45946977206)
completed successfully: 311 installed backup/import cases passed without skips,
the actual native SAPI/Studio/restart probe passed, and ten strict progress suites
passed 233 cases without skips. Its broad optional service suite passed 1,435
cases with 24 disclosed skips and no failures; that suite is separate from
zero-skip acceptance. The complete desktop workflow still has the three known
native failures. These results belong to `4d9804f`, not the later repaired source.

The repaired workflow selects 220 unique source suites and 127 unique installed
suites. Final collection on the composed application source found all 3,164
installed cases, all 135 required named cases and exactly 58 metadata cases.
Both native-driver regression files are installed/source gates; the existing
music and wrapper suites remain in their existing source/V3 gates. Strict skip,
failure, origin and source-identity checks are retained. Collection and the
local callback tests are not native Windows execution.

The canonical branch then advanced to `4b6f8169986c4a58b43e4a23bc081fbefadbbdcb`.
Its bounded, fail-closed OCR inventory change and exact required cases are now
merged alongside the already-composed music correction. The complete OCR runtime
suite passed 44 cases without skips, including actual local Tesseract fixtures.

The reviewed PR163 reader/writer and native autosave-timer corrections are also
selectively composed at `05ad32f`. Windows state readers use bounded coherent
opened snapshots; replacement publication remains bound to the flushed staging
identity and preserves old open readers through the reviewed native primitive.
The existing create-only publication and transcription backup/control policies
remain intact. Autosave now latches inactive on destruction of its captured
native owner, without querying a dying frame wrapper. A composed seven-suite run
passed 226 cases without failures/errors/skips, including all 116 transcription
cases, keymap interchange and reading-session recovery. The dedicated Windows
25-case replacement diagnostic and real 3-second timer/Close/Destroy probe are
mandatory in the installed-wheel, full-bundle and V3 paths. Their native result
is pending on this new composition; earlier donor execution is not transferred.

The final workflow now has 222 unique source selectors and 132 installed
selectors spanning 130 test files. Importlib collection succeeds for all 3,193
cases, all 142 required named cases, the three additional exact OCR cases and
exactly 58 metadata cases. The previous 220/127 selection above remains a
historical collection record. All native probes and strict source/origin/skip
checks are retained.
