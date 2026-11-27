# Scene audio clips

Open **Audio Studio → Edit scene audio clips**. Choose a saved production or
create an audio-only project. Choose its scene, then **Add audio file** or
**Add saved recording**. The latter uses a completed recording from
[Record microphone](MICROPHONE_RECORDING.md); it verifies that recording's
receipt before retaining the exact selected bytes.

The named native controls edit the clip name, start in the scene, start in its
original audio, selected duration, gain, pan, fade in, fade out and track role.
An empty duration retains the rest of the selected original. **Save clip
settings** validates and publishes one new project revision. **Move to another
scene** moves the same clip in one revision; **Remove clip** removes its placement
and keeps retained media and earlier revisions. Minimum scene duration can add
silence, but never truncates a longer placed clip.

**Preview saved clip** renders the selected range with saved gain, pan and
fades. It omits the leading scene silence. **Stop playback** also invalidates a
pending preview waiting for progress speech to become quiet. Closing during an
import or preview requests cancellation and waits for the owned operation to
finish before disposing its controls. No playback or microphone capture begins
when this window opens. All controls and buttons are keyboard reachable;
Escape closes or cancels and waits.

Render the complete project from **Audio Studio → Script and audio production**:
open the saved project, then Render. The existing master, chapters and speaker
stems are retained. Edited clip projects additionally publish whole-master
aligned `stems/clips/{sfx,ambience,music,microphone}.wav` for roles that occur in
the project. Music clip stems are before sidechain ducking. `timeline.json`
records measured selection duration, sample-aligned placement, source hash and
saved controls. A recorded clip does not create invented transcript text or
word timings.

## Preservation and recovery

The selected local original is copied into existing private Studio inputs under
its SHA-256 identity. Copying, decoder verification and recording-receipt binding
must complete before a project revision is published. Only that selected file
is admitted; neighbouring files are not authorized. Editing never rewrites its
audio samples. A concurrent project change requires reloading rather than
silently replacing the newer revision.

Importing project JSON records external input paths for explicit review without
opening or probing those paths. Render/Resume asks before reading them or adding
them to future backups. Existing explicit input approvals survive an ordinary
resubmission; importing another declaration requires review again.

Schema 1 project serialization and fingerprints remain byte compatible. New
clip controls use project schema 2. Saving an unchanged loaded script preserves
clips, source bindings, performance mode and sample rate. Changing the identity
of a scene with clips requires explicitly moving or removing them first.

The existing complete backup includes retained clip inputs, current projects,
historical revisions and production jobs. On restore into a new library, input
paths are rebound and output destinations require explicit selection. A real
regression deletes the old original, library and outputs before restoring and
resuming the same edited-clip job; its new PCM master matches the prior bytes.

## Console recovery

The console uses the same project schema and rendering service:

```powershell
research-library studio --data-dir "D:\Research" render --project "D:\Projects\audio.json" --output "D:\Exports" --approve-input "D:\Audio\selected.wav"
research-library studio --data-dir "D:\Research" resume JOB_ID --approve-input "D:\Audio\selected.wav" --output "D:\RestoredExports"
```

Repeat `--approve-input` only for exact files listed by the selected saved job.
`--output` on resume selects a fresh destination for a restored job; it cannot
redirect an ordinary active job. The original files and earlier output folders
remain intact. Projects containing only clips or silence do not discover voices.

## Execution evidence

The current Linux clip suite passed **26** strict cases, the clip-dialog suite
passed **11**, and the actual console import/review/relocation suite passed **2**,
all with zero skips or failures. These are separate from native Windows and
human listening acceptance.

The portable tests use actual local FFmpeg decoding and mixing, with known
synthetic PCM. They check audible source trimming, silence placement, left/right
routing, fade amplitudes, full-length stem alignment, cancellation, changed
source rejection, stale edits, exact original preservation and destructive
backup recovery. Controller tests cover queued selection binding, close/cleanup
and late completion. These are not microphone quality or human accessibility
tests.

Native Windows acceptance is **NOT_RUN** for this composed source until the
candidate workflow reports its result. The acceptance gate runs `scripts/windows_clip_editor_smoke.py` with the installed
interpreter. It requires real native focus, a wx main loop and Space key events,
then imports, edits, renders, rejects a stale revision and removes a clip. The
file-picker result is an explicit fixture; native file-picker and Windows 11
NVDA/manual listening qualification remain separate.
