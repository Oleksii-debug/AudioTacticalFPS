# Local speech and narration-copy implementation

## Implemented boundary

`audio/voices.py` invokes an explicitly selected local FFmpeg build with Flite,
or the installed Windows System.Speech API through a fixed packaged PowerShell
worker. It does not download engines/models, install voices, change execution
policy, invoke a shell, accept SSML, send text to a service or fall back to an
external provider. Windows native and SysWOW64 SAPI5 inventories use separate
`sapi5` and `sapi5-x86` provider IDs. No SAPI4 compatibility is claimed.

Both engines use the same plain-text → PCM16 mono WAV interface. Output is
checked for frame count, sample rate, channels and truncation, hashed and
published with an atomic no-clobber operation. Cancellation or a failed worker
leaves no published utterance. The per-segment character and worker-time budgets
are separate from total audiobook duration; the production director splits long
text and keeps completed segments. Speech-worker output is bounded, and errors
do not echo private utterances. Text containing command or filter syntax remains
text in the private request file.

Voice metadata includes provider, installed identity, languages and supported
controls. Flite rate is post-synthesis time stretching. SAPI's rate control is
mapped to its native −10…10 scale and is not an exact duration multiplier.
Neither adapter currently supplies word alignment, emotion, pitch, phonemes or
native-quality multilingual guarantees. Segment durations are measured from
actual PCM. A cached engine identity fingerprints the installed executable and
reported inventory/version; it is not a complete audit of proprietary voice
files or dynamic-library distribution rights.

`audio/reading.py` keeps the original Unicode source and its SHA256 immutable
through its public API. Literal pronunciation changes and manual edits create
versioned narration derivatives with undo/redo, explicit diffs and sparse origin
spans. Changed/inserted text has no source offset. Original-offset mapping does
not grant citation authority: quotations still require the existing research
source-verification workflow. No automatic punctuation/stress correction or
unbounded regular-expression engine is implemented.

## Reused APIs and rights

This change contains independently written adapters and calls upstream APIs; no
upstream voice, model, compiled binary or private project code is copied.

- [FFmpeg Flite filter documentation](https://ffmpeg.org/ffmpeg-filters.html#flite)
  documents runtime voice enumeration, local text-file input and compiled-in
  `libflite` support. The requirements' reviewed FFmpeg source candidate is
  `fa456433196deda8677258e695f6614d0f094c38`; this is not represented as the exact
  build provenance of the separately installed Debian executable used below.
- [Microsoft SpeechSynthesizer](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer)
  provides installed voice enumeration, selection and plain text synthesis.
  [PCM output](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.setoutputtowavefile)
  and [rate](https://learn.microsoft.com/en-us/dotnet/api/system.speech.synthesis.speechsynthesizer.rate)
  are configured through the documented APIs. Existing Windows voice licences
  remain separate; no permission to redistribute installed voices is implied.
- [Flite's upstream COPYING](https://github.com/festvox/flite/blob/master/COPYING)
  contains its component notices. A future distributable runtime must review
  and pin its exact Flite code, voice data and build notices together. This
  source-only change does not bundle them.
- Exact installed-repository search for `dordiai-voices` returned no matching
  repository in this run. The registry reports the engine unavailable until its
  real interface and installation are inspected; no invented protocol or copied
  private code is substituted for the project.

## Executed checks

Host: Linux, CPython 3.12.14, pytest 9.0.2, installed FFmpeg
`7.1.5-0+deb13u1`, built with `--enable-gpl --enable-libflite`. The installed build
is used locally only and is not included in a delivered package.

```
python -m pytest -q tests/audio/test_voices.py tests/audio/test_reading.py
```

Result: **57 passed, 1 skipped**. The skipped test is explicitly the actual
Windows SAPI5 integration test. Mock System.Speech plumbing checks are labelled
as mocks and are not native speech evidence.

Actual Flite inventory was `awb`, `kal`, `kal16`, `rms`, `slt`. Actual `awb`,
`rms` and `slt` outputs contained distinct nonempty PCM at 24 kHz. Separate
executed regressions cover Unicode filenames, hostile-looking text remaining
data, unknown/injected voice IDs, preservation of existing or concurrently
created output, cancellation, diagnostic/time budgets, no private text in
worker errors, immutable source spans, undo/reopen, malformed history and edit
resource failures. No human listening assessment was performed.

Actual Windows 11/SAPI5 x64+x86, device playback, SAPI4, dordiai-voices and NVDA
acceptance remain separate pending checks. The optional native SAPI5 test is in
`tests/audio/test_voices.py` and must be run on a real Windows host.

## Independent integrated production verification

The separate `tests/audio/test_director_independent.py` suite exercised actual
Flite speech and FFmpeg music/mixing through the production director, project
store and durable journal. It initially reproduced two failures: a checksumed
non-audio file could be accepted as a complete production, and a project beneath
an ancestor symlink was accepted. Both were corrected before the final run.

The corrected validator binds the exported plan and immutable script, requires
the complete planned artifact inventory, verifies source/role/line timing and
checks actual PCM duration, channels and sample rate for master, chapters and
stems. Project and backup paths reject linked ancestors and Windows network or
reparse paths before creation. Checkpoint backups are restricted to their exact
cache identities; restore verifies the journal against the copied inventory.
Directory publication uses atomic no-replace semantics on Linux and Windows,
including a regression for a destination created between preflight and rename.
Lock acquisition rejects existing hardlinks so the locking library cannot
truncate an unrelated source or draft.

```
python -m pytest -q tests/audio/test_reading.py tests/audio/test_voices.py tests/audio/test_director_independent.py
```

Final executed result at this handoff: **71 passed, 1 skipped**, 51.30 seconds.
The independent suite contributes 14 tests, including a real two-scene,
three-voice production with four stems and measured channel isolation. A saved
edit to one line caused one additional speech call and preserved the first
chapter checksum. Cancelling after two completed checkpoints, taking a backup
during the render, restoring and resuming reused the completed speech. An altered
published master was preserved while a new complete output was rebuilt from
verified cache. These are operational and signal-integrity checks, not human
listening, Windows, NVDA, or full V3 acceptance.
